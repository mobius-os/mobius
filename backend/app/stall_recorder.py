"""Opt-in log of slow requests, event-loop stalls and long database holds.

Off unless ``<data_dir>/tracing.json`` has ``"stall_log": true`` (read once at
startup, like tracing). Unlike tracing it needs no collector or extra
packages, and it answers a question span durations cannot: what the server
was doing *while* something was slow. Slowness is bursty, so a profile
started by hand (``/api/debug/profile``) usually misses it.

While on, a watchdog thread notices, as it happens:

- a request that has not started its response after ``request_seconds``
  (the database's long-hold threshold: work no healthy request should take).
  Streams start their response at once, so they never count;
- an event loop whose heartbeat is late by asyncio's own
  ``slow_callback_duration``, the threshold its debug mode reports slow
  callbacks at;
- a database connection held past the long-hold threshold (reported by
  ``database``; no stacks).

Everything late at one watchdog tick shares one snapshot of busy threads and
the app frames of parked ones. Records are JSON lines in
``logs/stalls.jsonl`` with one rotation, each file bounded like the chat log;
event lines reference their snapshot line by ``snapshot``. Records hold the
shape of work, never its content: route templates (no concrete paths, ids,
file names or query strings), status codes, durations, thread names and code
locations.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import tracing
from app.activity import route_template
from app.chat_logging import LOG_FILE_MAX_BYTES
from app.stack_sampler import thread_snapshot

_log = logging.getLogger("mobius.stalls")
_log.propagate = False
_log.setLevel(logging.INFO)

_recorder: StallRecorder | None = None


def _io_pressure_avg10() -> float | None:
  try:
    first = Path("/proc/pressure/io").read_text().splitlines()[0]
    return float(first.split("avg10=")[1].split()[0])
  except (OSError, IndexError, ValueError):
    return None


class StallRecorder:
  def __init__(self, *, request_seconds: float, snapshot_threads=thread_snapshot):
    self.request_seconds = request_seconds
    self._snapshot_threads = snapshot_threads
    self._lock = threading.Lock()
    self._inflight: dict[int, dict] = {}
    self._tokens = itertools.count(1)
    self._snapshots = itertools.count(1)
    # The log outlives the process: prefix snapshot ids with this run so a
    # reference never resolves to a snapshot from an earlier boot.
    self._run_id = secrets.token_hex(4)
    self._last_tick = time.monotonic()
    self._stall_snapshot: str | None = None

  async def run(self, log_path: Path) -> None:
    """Watch for stalls and write records until cancelled."""
    threshold = asyncio.get_running_loop().slow_callback_duration
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
      log_path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=1, encoding="utf-8",
    )
    _log.addHandler(handler)
    stop = threading.Event()
    self._last_tick = time.monotonic()
    watchdog = threading.Thread(
      target=self._watch, args=(stop, threshold), name="stall-recorder",
      daemon=True,
    )
    watchdog.start()
    try:
      while True:
        before = self._last_tick = time.monotonic()
        await asyncio.sleep(threshold)
        late = time.monotonic() - before - threshold
        # Taken every tick, so a snapshot can only belong to this stall.
        with self._lock:
          snapshot, self._stall_snapshot = self._stall_snapshot, None
        if late >= threshold:
          self.emit({
            "kind": "loop_stall", "ms": round(late * 1000), "snapshot": snapshot,
          })
    finally:
      stop.set()
      watchdog.join(timeout=1)
      _log.removeHandler(handler)
      handler.close()

  # -- inputs ------------------------------------------------------------

  def request_started(self, scope) -> int:
    token = next(self._tokens)
    with self._lock:
      self._inflight[token] = {
        "scope": scope, "start": time.monotonic(), "checked": False,
        "snapshot": None,
      }
    return token

  def request_responded(self, token: int, status: int | None) -> None:
    """The response started (or the request ended without one)."""
    with self._lock:
      entry = self._inflight.pop(token, None)
    if entry is None:
      return
    elapsed = time.monotonic() - entry["start"]
    if elapsed >= self.request_seconds:
      scope = entry["scope"]
      self.emit({
        "kind": "slow_request", "method": scope.get("method", "?"),
        "route": route_template(scope), "status": status,
        "ms": round(elapsed * 1000), "snapshot": entry["snapshot"],
        # Checked while late and nothing in the server was busy: it was
        # waiting on something external (an upstream, a long poll).
        "server_idle": entry["checked"] and entry["snapshot"] is None,
      })

  def emit(self, event: dict) -> None:
    event = {"at": datetime.now(UTC).isoformat(timespec="milliseconds"), **event}
    _log.info(json.dumps(event, separators=(",", ":")))

  # -- watchdog ----------------------------------------------------------

  def _watch(self, stop: threading.Event, threshold: float) -> None:
    own = threading.get_ident()
    while not stop.wait(threshold):
      now = time.monotonic()
      with self._lock:
        late = [
          entry for entry in self._inflight.values()
          if not entry["checked"] and now - entry["start"] >= self.request_seconds
        ]
        # One heartbeat sleep plus the stall threshold without a tick.
        stalled = (
          self._stall_snapshot is None
          and now - self._last_tick >= 2 * threshold
        )
      if late or stalled:
        snapshot_id = self._snapshot(own, now, keep_idle=stalled)
        with self._lock:
          for entry in late:
            entry["checked"] = True
            entry["snapshot"] = snapshot_id
          if stalled:
            self._stall_snapshot = snapshot_id

  def _snapshot(self, own: int, now: float, *, keep_idle: bool) -> str | None:
    """Record what the server is doing; nothing busy is no evidence.

    A request can stay late only because it waits on something outside the
    server, with every thread idle; such snapshots are dropped. A stalled
    event loop is busy by definition, so its snapshots are always kept.
    """
    threads = self._snapshot_threads(exclude=frozenset({own}))
    if not threads["busy"] and not keep_idle:
      return None
    snapshot_id = f"{self._run_id}-{next(self._snapshots)}"
    with self._lock:
      inflight = len(self._inflight)
    self.emit({
      "kind": "snapshot", "snapshot_id": snapshot_id,
      "loop_behind_ms": round((now - self._last_tick) * 1000),
      "inflight": inflight,
      "load": os.getloadavg()[0],
      "io_pressure_avg10": _io_pressure_avg10(),
      **threads,
    })
    return snapshot_id


class SlowRequestMiddleware:
  """Time each HTTP request to its response start for the stall log."""

  def __init__(self, app, recorder: StallRecorder):
    self.app = app
    self.recorder = recorder

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    token = self.recorder.request_started(scope)
    responded = False

    async def _send(message):
      nonlocal responded
      if not responded and message["type"] == "http.response.start":
        responded = True
        self.recorder.request_responded(token, int(message.get("status", 0)))
      await send(message)

    try:
      return await self.app(scope, receive, _send)
    finally:
      if not responded:
        self.recorder.request_responded(token, None)


def install(app, *, request_seconds: float) -> bool:
  """Add the request timer when tracing.json asks for the stall log."""
  global _recorder
  if not tracing.stall_log_requested():
    return False
  _recorder = StallRecorder(request_seconds=request_seconds)
  app.add_middleware(SlowRequestMiddleware, recorder=_recorder)
  return True


async def run(log_dir: Path) -> None:
  """Run the installed recorder until cancelled; returns at once when off."""
  if _recorder is not None:
    await _recorder.run(log_dir / "stalls.jsonl")


def note_db_hold(label: str, ms: int) -> None:
  if _recorder is not None:
    _recorder.emit({"kind": "db_hold", "request": label, "ms": ms})
