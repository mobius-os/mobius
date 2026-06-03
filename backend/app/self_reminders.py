"""Agent self-scheduling — append-only store of relational check-ins.

The agent schedules its OWN future work here ("check in on the user in
three days"), distinct from app-scoped cron (init-cron-scaffold.sh +
/data/apps/<slug>/job.sh). Cron runs a mini-app's job script and never
resumes a chat turn; a self-reminder, when due, resumes the chat it was
created in by posting a hidden message back to it. That is the whole
point: the agent develops a sense of time and relationship rather than
only ever being invoked by the user.

Store: /data/shared/self-reminders.jsonl, one JSON object per line.

  {"id":"<str>", "chat_id":"<str>", "due_at":<unix-int>,
   "note":"<str>", "created_at":<unix-int>,
   "status":"pending|done|cancelled"}

Append-only with logical status. enqueue() appends a `pending` record;
mark_done() and cancel() append a fresh `done` / `cancelled` record for
the same id rather than rewriting the line in place. Reads fold the file
so the LAST record wins per id — the same shape activity.jsonl uses, and
it keeps writes lock-free against the cron dispatcher reading concurrently
(a partial line is skipped, never a corrupt rewrite).

Caps and a horizon bound runaway self-scheduling: at most
MAX_PENDING_PER_CHAT live reminders per chat, and a due time no further
out than MAX_HORIZON_SECONDS. Both raise ReminderError (not a generic
ValueError) so the route can map them to a 4xx with the message intact.

Dispatcher gate: the cron dispatcher fires nothing until the owner opts
in by creating the sentinel file (see is_dispatcher_enabled). Deploying
installs the plumbing — store, endpoint, cron entry — but stays inert
until then, so a self-reminder written before opt-in simply waits.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger("mobius.self_reminders")

# Per-chat cap on live (pending) reminders. A relational check-in is a
# rare, deliberate act; a chat needing more than twenty outstanding ones
# is almost certainly a runaway loop, not a real schedule. The route
# surfaces the cap to the agent so it can cancel a stale one instead.
MAX_PENDING_PER_CHAT = 20

# Furthest a reminder may be scheduled out. A year is generous for a
# relational follow-up and still rejects an accidental "due in 10 years"
# (a units bug — seconds vs milliseconds — is the likely cause, and
# this catches it loudly rather than burying a reminder past any
# realistic session).
MAX_HORIZON_SECONDS = 366 * 24 * 60 * 60

# A note longer than this is almost certainly a mistake (the agent
# dumping a transcript into the field); bound it so one record can't
# bloat the file. Generous enough for a paragraph of context.
MAX_NOTE_LEN = 2000

# The opt-in sentinel filename, under /data/shared. Presence (not
# contents) is the signal; an empty file is enough to enable dispatch.
_SENTINEL_NAME = "self-reminders.enabled"

# Per-process write serialization, mirroring activity.py: hold the lock
# across the append so two concurrent enqueue/mark/cancel calls from the
# single uvicorn worker can't interleave half-lines. The cron dispatcher
# reads in a separate process and tolerates a torn final line by skipping
# it, so no cross-process lock is needed.
_write_lock = threading.Lock()


class ReminderError(ValueError):
  """A self-reminder was rejected for a caller-fixable reason.

  Subclasses ValueError so existing `except ValueError` paths still
  catch it, but the distinct type lets the route map it to a 4xx with
  the message passed straight through (caps, bad horizon, empty note,
  unknown id). Anything else bubbling out of this module is a real
  server fault, not a bad request.
  """


def _store_path() -> Path:
  """Resolves the canonical store path. Computed per-call (not at module
  load) so tests that override DATA_DIR after import write to the right
  place — the same reason activity.py recomputes its path."""
  return Path(get_settings().data_dir) / "shared" / "self-reminders.jsonl"


def _sentinel_path() -> Path:
  """Resolves the opt-in sentinel path under /data/shared."""
  return Path(get_settings().data_dir) / "shared" / _SENTINEL_NAME


def is_dispatcher_enabled() -> bool:
  """Returns True only when the owner has opted into dispatch.

  The cron dispatcher calls this first and exits without posting
  anything when it returns False, so a deploy installs the plumbing but
  fires nothing until the sentinel exists. DEFAULT OFF: the file is
  never created by the platform — only by an explicit owner action.
  """
  try:
    return _sentinel_path().exists()
  except OSError:
    return False


def _read_all_records() -> list[dict[str, Any]]:
  """Returns every record line in file order, skipping malformed lines.

  A torn final line (a concurrent append the reader caught mid-write) or
  a hand-edited bad line is skipped rather than crashing the scan — the
  store is the agent's own scratch space, not a database, and one bad
  line must not block the rest. Missing file reads as empty.
  """
  path = _store_path()
  records: list[dict[str, Any]] = []
  try:
    with path.open("r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          rec = json.loads(line)
        except json.JSONDecodeError:
          continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
          records.append(rec)
  except FileNotFoundError:
    return []
  except OSError as exc:
    log.warning("self-reminders read failed: %s", exc)
    return []
  return records


def _fold_latest(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
  """Folds append-only records so the LAST line per id wins.

  enqueue/mark_done/cancel each append a new line for the same id, so the
  current state of a reminder is its most recent record. Iterating in
  file order and overwriting by id yields the live view.
  """
  latest: dict[str, dict[str, Any]] = {}
  for rec in records:
    latest[rec["id"]] = rec
  return latest


def _append(record: dict[str, Any]) -> None:
  """Appends one record as a JSONL line under the write lock.

  Raises OSError on a write failure — unlike activity.py (sidecar
  telemetry that swallows errors), a self-reminder that silently fails
  to persist would leave the agent believing it scheduled a check-in
  that will never fire. The route turns the failure into a 5xx so the
  agent learns the write didn't land.
  """
  path = _store_path()
  line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
  with _write_lock:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
      f.write(line + "\n")
      f.flush()


def _coerce_due_at(
  due_at: int | float | None,
  due_in_seconds: int | float | None,
  now: int,
) -> int:
  """Resolves the absolute due time (unix seconds) from either input.

  Exactly one of `due_at` (absolute) or `due_in_seconds` (relative) must
  be given. The relative form is the natural one for the agent ("in three
  days" → 3*86400), so it's offered alongside the absolute form rather
  than forcing the caller to compute now()+delta itself.
  """
  if (due_at is None) == (due_in_seconds is None):
    raise ReminderError(
      "Provide exactly one of due_at or due_in_seconds."
    )
  if due_in_seconds is not None:
    if due_in_seconds <= 0:
      raise ReminderError("due_in_seconds must be positive.")
    resolved = now + int(due_in_seconds)
  else:
    resolved = int(due_at)
  if resolved <= now:
    raise ReminderError("due time must be in the future.")
  if resolved - now > MAX_HORIZON_SECONDS:
    raise ReminderError(
      f"due time is too far out (max {MAX_HORIZON_SECONDS} seconds "
      "from now)."
    )
  return resolved


def count_pending(chat_id: str) -> int:
  """Returns the number of live (pending) reminders for one chat."""
  latest = _fold_latest(_read_all_records())
  return sum(
    1 for r in latest.values()
    if r.get("chat_id") == chat_id and r.get("status") == "pending"
  )


def enqueue(
  chat_id: str,
  note: str,
  *,
  due_at: int | float | None = None,
  due_in_seconds: int | float | None = None,
  now: int | None = None,
) -> dict[str, Any]:
  """Appends a pending reminder for `chat_id` and returns the stored record.

  Validates inputs and enforces both the per-chat cap and the horizon
  before writing, so a rejected reminder never lands in the file. `now`
  is injectable so tests don't depend on wall-clock time. Raises
  ReminderError for any caller-fixable problem (empty note, bad horizon,
  cap exceeded, both/neither due field).
  """
  now = int(time.time()) if now is None else int(now)
  chat_id = (chat_id or "").strip()
  if not chat_id:
    raise ReminderError("chat_id is required.")
  note = (note or "").strip()
  if not note:
    raise ReminderError("note is required.")
  if len(note) > MAX_NOTE_LEN:
    raise ReminderError(f"note is too long (max {MAX_NOTE_LEN} chars).")
  resolved_due = _coerce_due_at(due_at, due_in_seconds, now)

  if count_pending(chat_id) >= MAX_PENDING_PER_CHAT:
    raise ReminderError(
      f"this chat already has {MAX_PENDING_PER_CHAT} pending reminders "
      "(the cap). Cancel one before adding another."
    )

  record = {
    "id": uuid.uuid4().hex,
    "chat_id": chat_id,
    "due_at": resolved_due,
    "note": note,
    "created_at": now,
    "status": "pending",
  }
  _append(record)
  return record


def list_pending(chat_id: str | None = None) -> list[dict[str, Any]]:
  """Returns live (pending) reminders, oldest-due first.

  Optionally filtered to one chat. Used by the route's GET so the agent
  can see what it has outstanding (and pick one to cancel).
  """
  latest = _fold_latest(_read_all_records())
  out = [
    r for r in latest.values()
    if r.get("status") == "pending"
    and (chat_id is None or r.get("chat_id") == chat_id)
  ]
  out.sort(key=lambda r: r.get("due_at", 0))
  return out


def list_due(now: int | None = None) -> list[dict[str, Any]]:
  """Returns pending reminders whose due_at has passed, oldest-due first.

  The dispatcher's read: every reminder ripe to fire right now. `now` is
  injectable for tests. Done/cancelled records are excluded by the fold
  (their latest status isn't pending).
  """
  now = int(time.time()) if now is None else int(now)
  out = [
    r for r in list_pending()
    if int(r.get("due_at", 0)) <= now
  ]
  return out


def mark_done(reminder_id: str) -> dict[str, Any]:
  """Appends a `done` record for `reminder_id` and returns it.

  Called by the dispatcher after it posts the check-in, so the reminder
  doesn't fire twice. Raises ReminderError if the id is unknown or
  already terminal (done/cancelled) — a double-dispatch attempt should
  fail loudly rather than silently no-op.
  """
  return _transition(reminder_id, "done")


def cancel(reminder_id: str) -> dict[str, Any]:
  """Appends a `cancelled` record for `reminder_id` and returns it.

  The agent's escape hatch when a planned check-in is no longer wanted
  (the user resolved the thing early, or the cap is full). Raises
  ReminderError if the id is unknown or already terminal.
  """
  return _transition(reminder_id, "cancelled")


def _transition(reminder_id: str, new_status: str) -> dict[str, Any]:
  """Appends a status-change record for a pending reminder.

  Shared by mark_done and cancel: both only ever move a `pending`
  reminder to a terminal state, never resurrect or re-terminate one.
  Refusing to transition a non-pending record is what makes the
  dispatcher idempotent — a reminder already marked done can't be
  re-dispatched.
  """
  latest = _fold_latest(_read_all_records())
  current = latest.get(reminder_id)
  if current is None:
    raise ReminderError(f"no reminder with id {reminder_id!r}.")
  if current.get("status") != "pending":
    raise ReminderError(
      f"reminder {reminder_id!r} is already "
      f"{current.get('status')!r}, not pending."
    )
  updated = dict(current)
  updated["status"] = new_status
  updated["resolved_at"] = int(time.time())
  _append(updated)
  return updated
