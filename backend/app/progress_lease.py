"""Progress lease — the single durable authority for a running turn's liveness.

Historically "is this turn alive?" was answered by three in-memory-ish signals
that had to agree: the runner-registry handle, ``broadcast.running``, and the
durable ``ChatRun.status``. A process that was *alive but making no progress*
(a stalled model stream) satisfied all three, so every recovery path skipped it
and the chat span forever. The fix drops the wrong question ("is the process
alive?") for the right one ("is the turn still making progress?").

A running turn renews a lease — ``ChatRun.progress_expires_at`` — to ``now +
regime TTL`` as it emits progress. Recovery reclaims any ``running`` row whose
lease has lapsed, on expiry alone, regardless of whether a handle or broadcast
still exists (those stay for operational routing — who to stream to / interrupt
— not liveness truth). Crash and hang collapse into one predicate: expired.

The regime picks the TTL so legitimate silence never trips:

- **awaiting the model** — with ``include_partial_messages`` the stream emits
  partials continuously, so a gap here is a dead stream: short TTL.
- **a CLI-internal tool running** — silent but bounded (the Bash tool caps at
  600s), so a cap safely above that never trips a real tool.
- **a platform-mediated long tool outstanding** (e.g. a blocking ``TaskOutput``
  join) — legitimately unbounded and silent, so the lease is suspended until it
  returns.

Renewal writes are throttled and touch only this one column on the already-
running row (guarded by ``status='running'``), so they never race the writer's
status transitions and are outside the transcript-ownership contract, which
covers ``Chat.messages``/``pending_messages`` only.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

# Regime TTLs, in seconds.
MODEL_IDLE_TTL = 120.0
# The Bash tool caps at 600s; a running CLI tool is silent but never longer.
TOOL_TTL = 720.0
# A platform-mediated long tool is legitimately unbounded: effectively no trip.
SUSPEND_TTL = 24 * 60 * 60.0
# Set at turn start, before the first token, while we await the model.
INITIAL_TTL = MODEL_IDLE_TTL

# Streaming emits many messages per second; the 30s sweep only needs coarse
# freshness, so collapse the renewal write rate to at most once per interval.
RENEW_MIN_INTERVAL = 15.0

# Root-level tools that can block silently for longer than ``TOOL_TTL`` because
# they wait on external/child work Möbius owns. While one is outstanding the
# lease is suspended rather than guessed. Kept minimal on purpose: a name that
# is missed here merely falls back to ``TOOL_TTL`` and, on a genuine over-run,
# a *resumable* interrupt — never a hard failure or livelock.
LONG_TOOL_NAMES = frozenset({"TaskOutput"})


def _now_naive() -> datetime:
  return datetime.now(UTC).replace(tzinfo=None)


def _write_lease(chat_id: str, deadline: datetime) -> None:
  """Renew the lease on the chat's running row(s) via its own short session.

  Keyed on ``chat_id`` + ``status='running'`` (a chat has one running row at a
  time; a transient superseded pair is harmless to renew together). The update
  only moves the deadline forward, so worker-thread completion order cannot
  shorten a newer lease. The status guard makes a terminal transition committed
  by the writer a no-op instead of resurrecting a finished run.
  """
  from sqlalchemy import update

  from app import models
  from app.database import SessionLocal

  with SessionLocal() as db:
    db.execute(
      update(models.ChatRun)
      .where(
        models.ChatRun.chat_id == chat_id,
        models.ChatRun.status == "running",
        (models.ChatRun.progress_expires_at.is_(None))
        | (models.ChatRun.progress_expires_at < deadline),
      )
      .values(progress_expires_at=deadline)
    )
    db.commit()


class ProgressLease:
  """Per-turn progress tracker that renews the durable lease as work advances.

  The runner calls :meth:`note_message` for every SDK message (progress) and
  :meth:`start` once before the receive loop. Tool bookkeeping is counted only
  at the root conversation level, because subagent sidechain messages already
  flow as progress and never need a regime of their own.
  """

  def __init__(self, chat_id: str, *, floor_ttl: float = 0.0) -> None:
    self.chat_id = chat_id
    # A conservative lower bound on the TTL for a provider whose tool
    # boundaries this tracker cannot parse (Codex): renewing on any message at
    # >= floor keeps a real silent tool from false-tripping while still
    # reclaiming a truly dead stream, without provider-specific block parsing.
    self._floor_ttl = floor_ttl
    self._outstanding_tools: set[str] = set()
    self._outstanding_long: set[str] = set()
    self._last_write_monotonic = 0.0
    self._pending_write = None

  def current_ttl(self) -> float:
    if self._outstanding_long:
      return SUSPEND_TTL
    if self._outstanding_tools:
      return max(TOOL_TTL, self._floor_ttl)
    return max(MODEL_IDLE_TTL, self._floor_ttl)

  def _track_tool_boundaries(self, sdk_msg) -> None:
    """Update outstanding root-tool sets from tool_use / tool_result blocks."""
    content = getattr(sdk_msg, "content", None)
    if not isinstance(content, list):
      return
    for block in content:
      tool_use_id = getattr(block, "id", None)
      name = getattr(block, "name", None)
      if tool_use_id and name is not None:
        # An AssistantMessage tool_use block: a tool is now outstanding.
        if name in LONG_TOOL_NAMES:
          self._outstanding_long.add(tool_use_id)
        else:
          self._outstanding_tools.add(tool_use_id)
        continue
      result_id = getattr(block, "tool_use_id", None)
      if result_id:
        # A UserMessage tool_result block: the matching tool has returned.
        self._outstanding_long.discard(result_id)
        self._outstanding_tools.discard(result_id)

  def start(self) -> None:
    """Arm the initial model-idle lease before the first token."""
    self._renew(force=True)

  def note_message(self, sdk_msg, *, is_root: bool) -> None:
    """Record progress from one SDK message and renew (throttled)."""
    if is_root:
      self._track_tool_boundaries(sdk_msg)
    self._renew(force=False)

  def _renew(self, *, force: bool) -> None:
    now_m = time.monotonic()
    if not force and now_m - self._last_write_monotonic < RENEW_MIN_INTERVAL:
      return
    self._last_write_monotonic = now_m
    deadline = _now_naive() + timedelta(seconds=self.current_ttl())
    self._dispatch_write(deadline)

  def _dispatch_write(self, deadline: datetime) -> None:
    """Persist the lease without ever blocking the event loop.

    The write is a tiny single-column UPDATE, but it can wait on SQLite's writer
    lock (the chat writer may hold it for seconds), so it runs on a worker
    thread. Off the loop it runs inline. Failures are swallowed — a missed
    renewal only shortens the lease and the sweep retries every 30s.
    """
    import asyncio

    try:
      loop = asyncio.get_running_loop()
    except RuntimeError:
      loop = None
    if loop is None:
      self._write_safe(deadline)
      return
    task = loop.create_task(asyncio.to_thread(self._write_safe, deadline))
    # Consume the result so a failed write never surfaces as an unretrieved
    # task exception, and keep a reference so it isn't GC'd mid-flight.
    self._pending_write = task
    task.add_done_callback(lambda _t: _t.exception())

  def _write_safe(self, deadline: datetime) -> None:
    try:
      _write_lease(self.chat_id, deadline)
    except Exception:
      import logging
      logging.getLogger("mobius.progress_lease").debug(
        "progress lease renew failed chat_id=%s", self.chat_id, exc_info=True,
      )


def lease_expired(run, now: datetime | None = None) -> bool:
  """Whether a running row's lease has lapsed.

  NULL means no active lease (a non-running row, or a pre-migration/in-flight
  run) — the caller keeps its legacy dead-process fallback for that case.
  """
  deadline = getattr(run, "progress_expires_at", None)
  if deadline is None:
    return False
  return (now or _now_naive()) > deadline
