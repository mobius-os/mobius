"""Server-owned execution of exact, owner-reviewed app updates.

The Store reviews; this owner durably accepts the selected commits and runs
each one through the same path a single reviewed update takes
(``install.install_requested_app``), independently of any browser. One JSON
receipt per batch records every app's progress and result.

At most ``_RUNNING_ENTRIES`` entries run at once across all batches, so their
Git transfers overlap without taking every shared transfer worker from
interactive installs; the rest wait as ``queued``. Each local install then
takes the app lifecycle lock in turn, like any other install.

One rule says whether a reviewed update landed (``_settled_outcomes``), for
restart, cancellation and later refreshes alike. Recovery never replays an
approval.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import time
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import activity, app_git, models, pending_update, settled
from app.broadcast import get_system_broadcast
from app.database import SessionLocal
from app.resource_access import live_app_or_404
from app.storage_io import atomic_write

log = logging.getLogger(__name__)
_ACTIVE = frozenset({"queued", "applying"})
# Entries running at once across all batches. Each may hold a Git transfer on
# the shared transfer workers; the rest stay free for interactive installs.
_RUNNING_ENTRIES = 2
# Shutdown waits this long for running installs to settle. A process killed
# sooner is covered too: boot settles every unfinished entry the same way.
_STOP_GRACE_SECONDS = 60
# SSE transport heartbeat, not an update deadline. A long Python check can run
# without a status change; keep proxies from mistaking that for a dead link.
_STREAM_KEEPALIVE_SECONDS = 15
# Finished receipts are audit and retry history, not state; bound their growth.
_RETAIN_COMPLETE = timedelta(days=30)
_NOT_APPLIED = (
  "This update was not applied. Check the app's update again before retrying."
)
_UNKNOWN_OUTCOME = (
  "Whether this update landed could not be read yet; it is checked again "
  "when this progress is opened."
)
_UNREADABLE = (
  "Saved update progress could not be read, so batch updates are paused; "
  "this is retried on the next request. Apps can still be updated one at a "
  "time. The saved progress is kept in app-update-batches/ for inspection."
)
_UNSAVED = (
  "Update progress could not be saved, so batch updates are paused. Free disk "
  "space and restart the server; apps can still be updated one at a time."
)


class ReviewedAppUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  app_id: int = Field(gt=0)
  manifest_url: str = Field(min_length=1)
  upstream_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
  source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
  capability_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedUpdateBatch(BaseModel):
  model_config = ConfigDict(extra="forbid")
  id: UUID
  updates: list[ReviewedAppUpdate] = Field(min_length=1)

  @model_validator(mode="after")
  def unique_apps(self):
    if len({item.app_id for item in self.updates}) != len(self.updates):
      raise ValueError("Review each installed app only once per batch.")
    return self


def _now() -> str:
  return datetime.now(UTC).isoformat()


async def install_reviewed_update(update: dict) -> dict:
  """Run one entry through the single reviewed-update path."""
  from app import install

  with SessionLocal() as db:
    result = await install.install_requested_app(
      db,
      manifest_url=update["manifest_url"],
      reviewed_capability_digest=update["capability_digest"],
      reviewed_source_digest=update["source_digest"],
      update_app_id=update["app_id"],
      reviewed_upstream_commit=update["upstream_commit"],
    )
    return {
      "status": "conflict" if result.mode == "conflict" else "updated",
      "version": result.app.version,
      "warnings": result.warnings,
    }


class SettlementUnreadable(Exception):
  """One app's durable state could not be read, so whether its update landed
  is unknown. Never a verdict: that entry keeps its state and is read again."""


def _pending_receipt(source_dir: str, app_id: int, commit: str) -> dict | None:
  """The pending-update receipt naming ``commit``; none only once proven."""
  try:
    return pending_update.checked_pending_conflict_update_receipt(
      source_dir, app_id=app_id, upstream_commit=commit,
    )
  except pending_update.PendingReceiptUnreadable as exc:
    raise SettlementUnreadable(f"pending update receipt of app {app_id}") from exc


def _contains(source_dir: str, reviewed_commit: str, source_commit: str) -> bool:
  """Whether the served revision contains the reviewed commit.

  Ancestry is read from commit objects, never the commit-graph cache, whose
  stale or corrupt parents could answer "no" falsely. A positive answer is
  proof on its own. A negative one is trusted only on complete history (not
  shallow, every commit readable): across a shallow boundary Git can answer
  "no" without seeing the reviewed commit.
  On complete history, a reviewed commit Git does not have (an update never
  fetched) is not contained either. Anything else is unknown.
  """
  contained = app_git.object_ancestry(source_dir, reviewed_commit, source_commit)
  if contained is True:
    return True
  if app_git.history_is_complete(source_dir, source_commit):
    if contained is False:
      return False
    if app_git.commit_object_absent(source_dir, reviewed_commit):
      return False
  raise SettlementUnreadable(f"history of {source_dir}")


def _settled_outcome(
  app_id: int, commit: str, row: tuple,
) -> dict:
  """One app's verdict, or ``SettlementUnreadable`` rather than a guess.

  The pending receipt is read first: one naming this commit means
  ``conflict`` whatever the history says. Only once it is proven absent does
  ancestry decide between ``updated`` and not applied. ``main`` can hold
  unapplied edits, and a conflicted update records the reviewed commit as its
  upstream without serving it; only the accepted ``source_commit`` is
  evidence. A leftover pending directory or checkout is harmless, and a
  receipt for a later update does not unseat an earlier one that landed.
  """
  source_dir, source_commit, version, resolver, resolver_commit = row
  if _pending_receipt(source_dir, app_id, commit) is not None:
    return {
      "status": "conflict", "version": version, "error": "",
      "error_code": None, "retryable": False,
      "resolver_chat_id": resolver if resolver_commit == commit else None,
    }
  if source_commit and _contains(source_dir, commit, source_commit):
    return {
      "status": "updated", "version": version, "error": "",
      "error_code": None, "retryable": False, "resolver_chat_id": None,
    }
  return {
    "status": "failed", "error": _NOT_APPLIED,
    "error_code": "update_not_applied", "retryable": False, "resolver_chat_id": None,
  }


def _settled_outcomes(reviewed: dict[int, str]) -> dict[int, dict]:
  """The one "did it land" rule, from durable app state. Read-only, blocking.

  ``reviewed`` maps app id to its reviewed upstream commit. Each app settles
  on its own (``_settled_outcome``). An app whose state cannot be read gets
  only an explanatory note, no status: its entry stays unfinished and is
  settled again later, while every other app still settles. Callers apply
  the returned patches to their record on the loop.
  """
  with SessionLocal() as db:
    rows = {
      row.id: (
        row.source_dir, row.source_commit, row.version,
        row.conflict_resolver_chat_id, row.conflict_resolver_upstream_commit,
      )
      for row in db.query(models.App).filter(
        models.App.id.in_(list(reviewed)),
        models.App.deleted_at.is_(None),
      )
    }
  outcomes = {}
  for app_id, commit in reviewed.items():
    row = rows.get(app_id)
    if row is None:
      outcomes[app_id] = {
        "status": "failed", "error": "This app is no longer installed.",
        "error_code": "app_not_found", "retryable": False, "resolver_chat_id": None,
      }
      continue
    try:
      outcomes[app_id] = _settled_outcome(app_id, commit, row)
    except SettlementUnreadable as exc:
      # One line, no traceback: an app that stays unknown is re-read on
      # every refresh, and the cause names what to repair.
      cause = exc
      while cause.__cause__ is not None:
        cause = cause.__cause__
      log.warning(
        "app %s update to %s is unknown: %s could not be read (%s)",
        app_id, commit, exc, cause if cause is not exc else "no answer",
      )
      outcomes[app_id] = {"error": _UNKNOWN_OUTCOME}
  return outcomes


def _unfinished(record: dict, entries: list[dict], statuses=_ACTIVE) -> dict[int, str]:
  commits = {u["app_id"]: u["upstream_commit"] for u in record["updates"]}
  return {e["app_id"]: commits[e["app_id"]] for e in entries if e["status"] in statuses}


def _apply(record: dict, outcomes: dict[int, dict]) -> bool:
  changed = False
  for entry in record["entries"]:
    patch = outcomes.get(entry["app_id"])
    if patch and "status" not in patch and entry["status"] not in _ACTIVE:
      continue  # An unknown note never replaces a settled result.
    if patch and any(entry.get(k) != v for k, v in patch.items()):
      entry.update(patch)
      changed = True
  return changed


def _failure(exc: BaseException) -> dict:
  """Keep the installer's code and retry verdict, as the single route does."""
  from app import install

  if isinstance(exc, HTTPException):
    detail = exc.detail
    message = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
    code = detail.get("code") if isinstance(detail, dict) else None
  else:
    message = "This update could not finish. Check it again before retrying."
    code = None
  return {
    "status": "failed", "error": message, "error_code": code,
    "retryable": install.transient_failure(exc)[0],
  }


class AppUpdateBatches:
  """One runtime owner; atomic receipts, no polling or browser-owned writes."""

  def __init__(self, data_dir: str, *, execute=install_reviewed_update):
    self.root = Path(data_dir) / "app-update-batches"
    self.execute = execute
    self.records: dict[str, dict] = {}
    self.tasks: dict[str, asyncio.Task] = {}
    self.listeners: dict[str, set[asyncio.Event]] = {}
    self.accepting = False
    self.failure: str | None = None
    self._read_failed = False
    self._slots: asyncio.Semaphore | None = None
    self._starting = asyncio.Lock()
    self._save_locks: dict[str, asyncio.Lock] = {}

  def _encode(self, record: dict) -> str:
    record["updated_at"] = _now()
    record["phase"] = (
      "applying" if any(e["status"] in _ACTIVE for e in record["entries"])
      else "complete"
    )
    return json.dumps(record, ensure_ascii=False) + "\n"

  def _write(self, key: str, text: str) -> None:
    atomic_write(self.root / f"{key}.json", text, mode=0o600)

  def _write_failed(self) -> None:
    # Progress that cannot be recorded must not keep admitting work.
    self.accepting = False
    self.failure = _UNSAVED
    for listeners in self.listeners.values():
      for wake in listeners:
        wake.set()

  def _saved(self, record: dict) -> None:
    self.records[record["id"]] = record
    for wake in self.listeners.get(record["id"], ()):
      wake.set()

  def _save(self, record: dict) -> None:
    """Save on the calling thread, for admission: no await may come before
    the receipt exists."""
    try:
      self._write(record["id"], self._encode(record))
    except OSError:
      self._write_failed()
      raise
    self._saved(record)

  async def _save_off_loop(self, record: dict) -> None:
    """Save progress without syncing the disk on the event loop.

    One lock per batch keeps writes in order, and each encodes the newest
    state when its turn comes, so the last write always holds the latest
    progress. The write finishes even if its caller is cancelled.
    """
    if self.records.get(record["id"]) is not record:
      return  # Pruned already: never write it back, nor leave a lock behind.
    lock = self._save_locks.setdefault(record["id"], asyncio.Lock())
    async with lock:
      if self.records.get(record["id"]) is not record:
        return  # Pruned while this save waited: never write it back.
      text = self._encode(record)
      try:
        await settled.run_settled(self._write, record["id"], text)
      except OSError:
        self._write_failed()
        raise
      self._saved(record)

  async def start(self) -> None:
    async with self._starting:
      await self._start()

  async def _start(self) -> None:
    self._slots = asyncio.Semaphore(_RUNNING_ENTRIES)
    self.records = {}
    try:
      # Settled: a retry cancelled mid-read keeps the previous failure, so the
      # next request retries again, and the read never races that retry.
      loaded = await settled.run_settled(self._load_receipts)
      # A retry runs while the server serves requests, so the outcomes are
      # applied here, on the loop, and the records appear only once saved.
      records, settled_texts = {}, {}
      for record, outcomes in loaded:
        records[record["id"]] = record
        if outcomes is not None:
          _apply(record, outcomes)
          settled_texts[record["id"]] = self._encode(record)
      try:
        await settled.run_settled(self._write_all, settled_texts)
      except OSError:
        self._write_failed()
        raise
      self.records = records
    except Exception:
      # A damaged batch receipt or a database error must not fail readiness
      # for unrelated chats and apps. Disable only this owner, visibly, and
      # keep the evidence for repair. One app's unreadable state never gets
      # here: that entry alone stays unfinished. A failed settlement write is
      # a write failure (_write_failed set it): never retried.
      self._read_failed = self.failure != _UNSAVED
      if self._read_failed:
        self.failure = _UNREADABLE
      self.accepting = False
      log.exception("app update progress could not be recovered")
      return
    self.failure = None
    self._read_failed = False
    self.accepting = True

  async def retry_start(self) -> None:
    """Retry a failed boot read; never after a failed write or at shutdown."""
    async with self._starting:
      # Concurrent requests share one retry: re-check under the lock.
      if self._read_failed and not self.tasks:
        await self._start()

  def _write_all(self, texts: dict[str, str]) -> None:
    for key, text in texts.items():
      self._write(key, text)

  def _load_receipts(self) -> list[tuple[dict, dict | None]]:
    """Off the loop: read the receipts and what their unfinished entries
    settled to. Touches no shared state; ``_start`` applies the result."""
    self.root.mkdir(parents=True, exist_ok=True)
    # A crash mid-write leaves only atomic_write's temporary file. No batch is
    # loaded yet, so no writer runs and any that remain are abandoned.
    for leftover in self.root.glob(".*.tmp"):
      leftover.unlink(missing_ok=True)
    expired = (datetime.now(UTC) - _RETAIN_COMPLETE).isoformat()
    loaded = []
    for path in self.root.glob("*.json"):
      # A corrupt receipt is not "no work". Fail visibly rather than silently
      # discarding an accepted batch.
      record = json.loads(path.read_text())
      if str(UUID(record["id"])) != path.stem:
        raise ValueError("Invalid app update batch receipt identity.")
      if record["phase"] == "complete" and record["updated_at"] < expired:
        path.unlink()
        continue
      unfinished = _unfinished(record, record["entries"])
      loaded.append((record, _settled_outcomes(unfinished) if unfinished else None))
    return loaded

  async def stop(self) -> None:
    """Stop admitting, then cancel running entries.

    The installer settles its current pass, rolls back, and releases its locks
    only once its threads end; each entry then records its settled outcome.
    A planned restart usually kills the process before a long install pass
    settles; boot then settles those entries from durable state.
    """
    self.accepting = False
    self._read_failed = False
    tasks = list(self.tasks.values())
    for task in tasks:
      task.cancel()
    if tasks:
      _done, pending = await asyncio.wait(tasks, timeout=_STOP_GRACE_SECONDS)
      if pending:
        log.warning(
          "%d app update batch(es) still settling at shutdown; boot settles them",
          len(pending),
        )

  def submit(self, request: ReviewedUpdateBatch, db) -> dict:
    if not self.accepting:
      raise HTTPException(503, self.failure or "App updates are unavailable while the server restarts.")
    self._prune_finished()
    key = str(request.id)
    updates = [item.model_dump() for item in request.updates]
    old = self.records.get(key)
    if old is not None:
      # The same id is the same approval: a repeated click or a retry after a
      # lost response attaches to it rather than starting work twice.
      if old["updates"] != updates:
        raise HTTPException(409, "This batch id already names different reviewed updates.")
      return self.snapshot(key)
    app_ids = {item["app_id"] for item in updates}
    for record in self.records.values():
      if any(e["app_id"] in app_ids and e["status"] in _ACTIVE for e in record["entries"]):
        raise HTTPException(409, detail={
          "code": "app_update_running", "batch_id": record["id"],
          "message": "An update for this app is already running. Open its progress.",
        })
    entries = []
    for item in updates:
      app = live_app_or_404(db, item["app_id"])
      entries.append({
        "app_id": app.id, "name": app.name, "status": "queued",
        "version": app.version, "error": "", "error_code": None,
        "retryable": False, "warnings": [], "resolver_chat_id": None,
      })
    record = {
      "id": key, "created_at": _now(), "updates": updates, "entries": entries,
    }
    # No await between the durable receipt, the owned task and the response.
    try:
      self._save(record)
    except OSError as exc:
      raise HTTPException(503, self.failure) from exc
    task = asyncio.create_task(self._run(record), name=f"app-update-batch:{key}")
    self.tasks[key] = task
    task.add_done_callback(lambda done: self._finished(key, done))
    return self.snapshot(key)

  def _prune_finished(self) -> None:
    """Forget finished batches past retention, as boot does."""
    expired = (datetime.now(UTC) - _RETAIN_COMPLETE).isoformat()
    for key, record in list(self.records.items()):
      lock = self._save_locks.get(key)
      if lock is not None and lock.locked():
        continue  # Its save would write it back; the next prune takes it.
      if record["phase"] == "complete" and record["updated_at"] < expired:
        (self.root / f"{key}.json").unlink(missing_ok=True)
        self.records.pop(key)
        self._save_locks.pop(key, None)

  def _finished(self, key: str, task: asyncio.Task) -> None:
    self.tasks.pop(key, None)
    # Keep its save lock: a refresh may still hold it. Pruning drops it.
    if not task.cancelled() and task.exception() is not None:
      log.error("app update batch %s stopped: %s", key, task.exception())

  def snapshot(self, key: str) -> dict:
    if self.failure:
      raise HTTPException(503, self.failure)
    record = self.records.get(key)
    if record is None:
      raise HTTPException(404, "App update batch not found.")
    return deepcopy({k: v for k, v in record.items() if k != "updates"})

  def recent(self) -> list[dict]:
    records = sorted(self.records.values(), key=lambda r: r["created_at"], reverse=True)
    latest_complete = next((r["id"] for r in records if r["phase"] == "complete"), None)
    visible = []
    newer_apps = set()
    for record in records:
      # A newer attempt replaces an app's older actionable result, but an
      # unrelated batch must never hide an unresolved conflict or failure.
      actionable = any(
        e["status"] in {"conflict", "failed"} and e["app_id"] not in newer_apps
        for e in record["entries"]
      )
      if record["phase"] == "applying" or actionable or record["id"] == latest_complete:
        visible.append(self.snapshot(record["id"]))
      newer_apps.update(e["app_id"] for e in record["entries"])
    return visible

  async def refresh_results(self, key: str, db=None) -> None:
    """Re-read actionable entries through the one "did it land" rule.

    A conflict takes whatever the rule says. A failure keeps its own reason
    unless the reviewed commit has since landed (the owner updated the app
    another way), when it is no longer actionable. An entry left unfinished
    because its outcome was unknown (and no longer running) is settled again.
    """
    record = self.records.get(key)
    if record is None:
      raise HTTPException(404, "App update batch not found.")
    if db is not None:
      db.rollback()  # release the request's read before blocking work
    statuses = {"conflict", "failed"}
    if key not in self.tasks:
      # Nothing runs it: an entry left unfinished by an unknown settlement.
      statuses |= _ACTIVE
    actionable = _unfinished(record, record["entries"], statuses)
    if not actionable:
      return
    outcomes = await asyncio.to_thread(_settled_outcomes, actionable)
    failed = {e["app_id"] for e in record["entries"] if e["status"] == "failed"}
    outcomes = {
      app_id: patch for app_id, patch in outcomes.items()
      if app_id not in failed or patch.get("status") == "updated"
    }
    if _apply(record, outcomes):
      try:
        await self._save_off_loop(record)
      except OSError as exc:
        raise HTTPException(503, self.failure) from exc

  async def snapshots(self, key: str):
    """Coalesce slow readers to the newest durable snapshot, never a backlog."""
    try:
      self.snapshot(key)
    except HTTPException:
      return
    wake = asyncio.Event()
    self.listeners.setdefault(key, set()).add(wake)
    try:
      while True:
        wake.clear()
        try:
          snapshot = self.snapshot(key)
        except HTTPException:
          # Progress can no longer be saved: end the stream cleanly; the
          # client's next GET reports the 503.
          return
        yield snapshot
        if snapshot["phase"] == "complete":
          return
        try:
          await asyncio.wait_for(wake.wait(), timeout=_STREAM_KEEPALIVE_SECONDS)
        except asyncio.TimeoutError:
          yield None
    finally:
      self.listeners[key].discard(wake)
      if not self.listeners[key]:
        self.listeners.pop(key)

  async def _run(self, record: dict) -> None:
    async def one(update, entry):
      try:
        async with self._slots:
          started = time.monotonic()
          entry["status"] = "applying"
          await self._save_off_loop(record)
          try:
            entry.update(await self.execute(update))
            entry.update(error="", error_code=None, retryable=False)
          except asyncio.CancelledError:
            raise
          except Exception as exc:
            if not isinstance(exc, HTTPException):
              log.exception("app update failed for %s", update["app_id"])
            entry.update(_failure(exc))
          entry["duration_ms"] = round((time.monotonic() - started) * 1000)
          await self._save_off_loop(record)
      except asyncio.CancelledError:
        # Shutdown, queued or running: the installer has settled. Read what
        # actually landed off the loop, then apply it here, where siblings
        # also save this record.
        # An unknown outcome stays unfinished, never "not applied".
        _apply(record, await asyncio.to_thread(
          _settled_outcomes, _unfinished(record, [entry]),
        ))
        await self._save_off_loop(record)
        raise
      activity.log_event(
        "app_update_outcome", batch_id=record["id"], app_id=entry["app_id"],
        outcome=entry["status"], duration_ms=entry["duration_ms"],
      )
      if entry["status"] != "failed":
        # As the single route: the durable row changed, refresh app lists.
        get_system_broadcast().publish({"type": "app_updated", "appId": str(entry["app_id"])})

    # Independent owner-selected apps: one failure never stops its siblings.
    results = await asyncio.gather(
      *(one(u, e) for u, e in zip(record["updates"], record["entries"])),
      return_exceptions=True,
    )
    for result in results:
      if isinstance(result, BaseException):
        raise result
