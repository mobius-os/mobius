"""Append-only JSONL platform-activity log.

Records platform events so introspective mini-apps and cron agents
(notably the nightly Reflection agent) can see what the owner did over a
window without scraping chat.log or guessing from mtime traces.

Event vocabulary (one JSON object per line, no trailing version field —
if we ever break compatibility we write a new file activity.v2.jsonl and
read both):

  {"ev":"app_open",       "ts", "app_id", "slug"}
  {"ev":"app_install",    "ts", "app_id", "slug", "source":"bootstrap|store|url"}
  {"ev":"app_uninstall",  "ts", "app_id", "slug"}   # logical (tombstone) delete
  {"ev":"storage_write",  "ts", "app_id", "path", "size_delta"}  # delete = negative delta
  {"ev":"cron_outcome",   "ts", "app_id", "job", "exit_code", "duration_ms"}
  {"ev":"skill_loaded",   "ts", "chat_id", "skill"}
  {"ev":"memory_load",    "ts", "source", "paths", "mode"}
  {"ev":"app_error",      "ts", "app_id"?, "message", "where"?, "stack"?, "url"?}
  {"ev":"app_signal",     "ts", "app_id", "id", "occurred_at", "name", "payload"}
  {"ev":"chat_sent",      "ts", "chat_id", "provider", "app_id"?}  # one per user turn
  {"ev":"chat_created",   "ts", "chat_id"}
  {"ev":"provider_switch","ts", "chat_id", "provider", "from_provider"}
  {"ev":"chat_log_read",  "ts", "app_id", "scope", "count"}  # app read redacted logs
  {"ev":"slug_collision", "ts", "requested_slug", "assigned_slug", "source"}

`app_id` may be 0 / null for platform-level events (the bootstrap store
install has no numeric id when it fires; an `app_error` with NO `app_id`
is a SHELL error, not an app's — that null is the shell/app discriminator).

`skill_loaded` is chat-scoped (carries `chat_id`, not `app_id`): it
records each time the agent invokes the Skill tool, so "most-used
skills" can be aggregated from the log. See `most_used_skills`.

Deliberately NOT recorded: navigation / drawer / scroll / click and
per-token chat deltas — high-frequency, low-signal UI noise that would
drown the events that matter against the 90-day retention. `app_open`
already captures which apps got used; `chat_sent` captures user turns.

Rotation: weekly. On each write we check the timestamp of the active
file's first parseable event; if it is older than 7 days we rename the
file to activity.YYYY-WW.jsonl and start a fresh activity.jsonl. After
rotation we sweep activity.*.jsonl files older than 90 days. This keeps
the working set bounded without any external scheduler.

Disabling: set MOBIUS_ACTIVITY_LOG=off. Tests that don't need the
log (most of them) use this so they don't litter /data/logs/.

Per-process write lock — a `threading.Lock` serializes writes from
the FastAPI worker so two concurrent storage PUTs can't interleave
half-lines. Cross-process serialization isn't needed: the container
runs a single uvicorn worker today, and cron jobs that opt into
outcome logging go through the same emitter via `cron-emit.sh`,
which POSTs to /api/admin/activity/emit (not direct file writes) so
all writes funnel through this lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger("mobius.activity")

# Tunables. Both are constants here on purpose — there's no operator
# need to vary either today, and pulling them from env would just
# make tests harder to reason about.
ROTATION_DAYS = 7
RETENTION_DAYS = 90

# Cache for storage_write debounce. Key: (app_id, path). Value: the
# ISO8601 timestamp string of the most recent emit. Module-level so it
# survives across requests within the same worker process. Reset by
# tests via `_reset_for_tests()` so suite ordering doesn't matter.
_DEBOUNCE_WINDOW_SEC = 60
_debounce: dict[tuple[int, str], str] = {}

# Same debounce shape for app_error: a render loop or a retrying fetch can
# throw the identical error many times a second; collapse to one per window
# per (app_id, message) so one broken app can't flood the 90-day log file.
_ERROR_DEBOUNCE_WINDOW_SEC = 60
_error_debounce: dict[tuple[int | None, str], str] = {}

# Per-process write serialization. Holds the lock while we (a) check
# rotation, (b) write the line, (c) flush. Critical section is small
# (one file handle open + one line write), so contention is fine.
_write_lock = threading.Lock()


def _activity_path() -> Path:
  """Resolves the canonical activity-log path. Computed per-call
  (not module-load) so tests that override DATA_DIR after import
  still write to the right place."""
  return Path(get_settings().data_dir) / "logs" / "activity.jsonl"


def _is_disabled() -> bool:
  """Tests + tooling can set MOBIUS_ACTIVITY_LOG=off to silence the
  emitter. Anything other than 'off' (case-insensitive) leaves it on,
  including the empty string — opt-out is explicit."""
  return (os.environ.get("MOBIUS_ACTIVITY_LOG") or "").lower() == "off"


def _now_iso() -> str:
  """ISO8601 UTC string with seconds precision. The tests + the read
  endpoint both parse this with `datetime.fromisoformat`."""
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _active_file_started(path: Path) -> datetime | None:
  """When the active log BEGAN — the `ts` of its first parseable line.

  Rotation must NOT key on mtime: the append on every write keeps mtime
  ~now, so `now - mtime` never reaches ROTATION_DAYS and the file grows
  unbounded. The first line's ts is the file's true start and is stable
  across writes. Returns None if the file is empty/unreadable/header-less,
  in which case the caller skips rotation (nothing to rotate yet)."""
  try:
    with path.open("r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          ev = json.loads(line)
        except json.JSONDecodeError:
          continue
        ts = ev.get("ts")
        if not isinstance(ts, str):
          continue
        try:
          dt = datetime.fromisoformat(ts)
        except ValueError:
          continue
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
  except OSError:
    return None
  return None


def _rotate_if_due(path: Path, now: datetime) -> None:
  """Renames the active file to activity.YYYY-WW.jsonl if it has been
  collecting for more than ROTATION_DAYS, then sweeps any activity.*.jsonl
  older than RETENTION_DAYS. Both checks happen on every write — cheap
  enough (one short read per write) and side-steps a background scheduler.

  Age is measured from the file's FIRST event ts, not mtime: the append on
  every write resets mtime, so an mtime check would never trip (the bug
  this replaces — the active file grew unbounded).

  Called with `_write_lock` held."""
  if not path.exists():
    return
  started = _active_file_started(path)
  if started is None:
    return
  if now - started < timedelta(days=ROTATION_DAYS):
    return
  # ISO week number gives us a stable rotation suffix that sorts
  # naturally and survives DST boundaries — strftime("%G-W%V") is
  # the ISO-week-numbering-year + ISO week. Keyed to when the data
  # STARTED (first event) so the archive name reflects its contents.
  suffix = started.strftime("%G-W%V")
  rotated = path.with_name(f"activity.{suffix}.jsonl")
  # If the rotated name already exists (rare — would mean two
  # rotations landed in the same ISO week, e.g. clock jumped),
  # fall through to a counter-suffixed variant so we never
  # overwrite history.
  if rotated.exists():
    n = 2
    while rotated.with_suffix(f".{n}.jsonl").exists():
      n += 1
    rotated = rotated.with_suffix(f".{n}.jsonl")
  try:
    path.rename(rotated)
  except OSError as exc:
    log.warning("activity log rotation rename failed: %s", exc)
    return
  _sweep_old(path.parent, now)


def _sweep_old(logs_dir: Path, now: datetime) -> None:
  """Deletes activity.*.jsonl files whose mtime is older than
  RETENTION_DAYS. Best-effort; a stat failure on one file does not
  stop the sweep."""
  cutoff = now - timedelta(days=RETENTION_DAYS)
  try:
    candidates = list(logs_dir.glob("activity.*.jsonl"))
  except OSError:
    return
  for fp in candidates:
    try:
      mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
    except OSError:
      continue
    if mtime < cutoff:
      try:
        fp.unlink()
      except OSError as exc:
        log.warning("activity log sweep failed for %s: %s", fp, exc)


def log_events(
  events: list[tuple[str, dict[str, Any]]], *, durable: bool = False,
) -> bool:
  """Append an event batch under one lock/open/flush, returning success.

  Errors are still swallowed for the many best-effort call sites, but the bool
  lets the client-signal ingest endpoint return 503 and retain its durable
  browser queue instead of acknowledging data that never reached disk.
  """
  if _is_disabled() or not events:
    return True
  try:
    lines = []
    for ev, source_fields in events:
      fields = dict(source_fields)
      payload: dict[str, Any] = {"ev": ev}
      payload["ts"] = fields.pop("ts", None) or _now_iso()
      payload.update(fields)
      # ASCII escaping is deliberate: a browser can truncate between UTF-16
      # surrogate halves, and ensure_ascii=False would raise during UTF-8 write.
      lines.append(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    encoded = ("\n".join(lines) + "\n").encode("ascii")
    path = _activity_path()
    now = datetime.now(timezone.utc)
    with _write_lock:
      path.parent.mkdir(parents=True, exist_ok=True)
      _rotate_if_due(path, now)
      with path.open("ab+") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size:
          f.seek(-1, os.SEEK_END)
          if f.read(1) != b"\n":
            # Repair a partial tail left by a prior interrupted append so the
            # retried first event starts on a parseable line.
            f.seek(0)
            content = f.read()
            last_newline = content.rfind(b"\n")
            f.truncate(last_newline + 1 if last_newline >= 0 else 0)
            f.seek(0, os.SEEK_END)
        f.write(encoded)
        f.flush()
        if durable:
          os.fsync(f.fileno())
    return True
  except (OSError, UnicodeError, TypeError, ValueError) as exc:
    log.warning("activity log batch write failed (%s): %s", events[0][0], exc)
    return False


def log_event(ev: str, **fields: Any) -> bool:
  """Append one event; best-effort callers may ignore the success result.

  Fields are passed through unchanged; the caller is responsible
  for shape (see this module's docstring for the per-event schema).
  `ts` is filled in here if the caller didn't supply one, so every
  call site doesn't have to repeat the ISO formatting.
  """
  return log_events([(ev, fields)])


def should_emit_storage_write(app_id: int, path: str, now: datetime | None = None) -> bool:
  """Debounce gate for storage_write events. Returns True at most
  once per `_DEBOUNCE_WINDOW_SEC` seconds per (app_id, path).

  Caller pattern is `if should_emit_storage_write(...): log_event(
  "storage_write", ...)`. Splitting the gate from the emit keeps
  the emitter unaware of debounce policy and the test for the gate
  trivially isolated.

  The cache is in-memory and resets across process restarts —
  acceptable per the ticket: at most one extra event per restart
  per (app_id, path), no correctness issue.
  """
  now = now or datetime.now(timezone.utc)
  key = (app_id, path)
  last_iso = _debounce.get(key)
  if last_iso is not None:
    try:
      last = datetime.fromisoformat(last_iso)
    except ValueError:
      last = None
    if last is not None and (now - last).total_seconds() < _DEBOUNCE_WINDOW_SEC:
      return False
  _debounce[key] = now.isoformat(timespec="seconds")
  return True


def should_emit_app_error(
  app_id: int | None, message: str, now: datetime | None = None
) -> bool:
  """Debounce gate for app_error: True at most once per
  _ERROR_DEBOUNCE_WINDOW_SEC seconds per (app_id, message) — one render
  loop throwing the same error 60x/sec would otherwise flood the log.
  Mirrors should_emit_storage_write (in-memory, reset-on-restart)."""
  now = now or datetime.now(timezone.utc)
  key = (app_id, message)
  last_iso = _error_debounce.get(key)
  if last_iso is not None:
    try:
      last = datetime.fromisoformat(last_iso)
    except ValueError:
      last = None
    if last is not None and (now - last).total_seconds() < _ERROR_DEBOUNCE_WINDOW_SEC:
      return False
  _error_debounce[key] = now.isoformat(timespec="seconds")
  return True


def _reset_for_tests() -> None:
  """Clears the debounce cache. Called from conftest's fresh_db
  fixture so a debounce entry from a prior test can't suppress a
  later test's emit. Underscore-prefixed: not a public API."""
  _debounce.clear()
  _error_debounce.clear()


def _candidate_files(active: Path) -> list[Path]:
  """Returns activity log files to scan for a read: every rotated
  archive (`activity.YYYY-W##.jsonl`, plus any counter-suffixed
  collision variant), oldest first by mtime, then the active file
  last.

  Ordering matters: events inside one file are roughly time-ordered
  (the emitter appends with `ts=now()`), and archives are themselves
  ordered by when they rotated. Yielding files oldest→newest keeps
  the merged stream in roughly ascending ts so consumers that scan
  forward (the reflection agent's "since X" window) see the natural
  shape.

  No window-based pruning happens here. With 90-day retention and
  weekly rotation the worst case is ~13 archive files; for any
  realistic window the per-event ts filter in `read_events` is
  cheaper than pre-filtering by archive mtime (which can drift if
  someone touches the file, sweeps it late, etc).
  """
  parent = active.parent
  try:
    archives = list(parent.glob("activity.*.jsonl"))
  except OSError:
    archives = []
  # Exclude the active file from the archive list. `activity.jsonl`
  # doesn't match `activity.*.jsonl` (there's no segment between the
  # two dots), but be explicit so a future rename can't silently
  # double-yield the active file's events.
  archives = [p for p in archives if p != active]
  try:
    archives.sort(key=lambda p: p.stat().st_mtime)
  except OSError:
    # A stat failure on one file would crash the whole read.
    # Fall back to name-sort: archive names contain the ISO year +
    # week so lexicographic order tracks chronological order well
    # enough for the reflection-agent use case.
    archives.sort(key=lambda p: p.name)
  result = list(archives)
  if active.exists():
    result.append(active)
  return result


@contextmanager
def _event_source_snapshot(active: Path):
  """Open a stable snapshot of every activity source.

  Rotation renames ``activity.jsonl``. Merely snapshotting its pathname leaves
  a gap: if a writer rotates after candidate enumeration but before the reader
  opens the active path, the reader opens the new file and the just-rotated
  archive was never in its candidate list. Open all source inodes while holding
  the same lock rotation uses, then stream them after releasing the lock. Open
  descriptors remain valid across a later rename or retention unlink.
  """
  stack = ExitStack()
  sources = []
  try:
    with _write_lock:
      for path in _candidate_files(active):
        try:
          source = stack.enter_context(path.open("r", encoding="utf-8"))
        except OSError as exc:
          log.warning("activity log snapshot open failed for %s: %s", path, exc)
          continue
        sources.append((path, source))
    yield sources
  finally:
    stack.close()


def _yield_events_from_source(path: Path, source):
  """Iterates parsed event dicts from one JSONL file. Malformed lines
  are skipped silently — the log is sidecar data, not a database, and
  a single bad line must not block the rest from reaching the consumer.
  Missing `ts` or non-string `ts` likewise drops the line."""
  try:
    for line in source:
      line = line.strip()
      if not line:
        continue
      try:
        ev = json.loads(line)
      except json.JSONDecodeError:
        continue
      ts_str = ev.get("ts")
      if not isinstance(ts_str, str):
        continue
      try:
        ts = datetime.fromisoformat(ts_str)
      except ValueError:
        continue
      # Naive timestamps are treated as UTC — _now_iso always
      # writes tz-aware, so this fallback only matters for
      # hand-crafted test inputs.
      if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
      yield ev, ts
  except OSError as exc:
    log.warning("activity log read failed for %s: %s", path, exc)
    return


def _yield_events_from(path: Path):
  """Path-opening compatibility wrapper for focused tests and tooling."""
  try:
    with path.open("r", encoding="utf-8") as source:
      yield from _yield_events_from_source(path, source)
  except OSError as exc:
    log.warning("activity log read failed for %s: %s", path, exc)
    return


def log_skill_load(chat_id: str | None, skill: str, ts: str | None = None) -> None:
  """Records one Skill-tool invocation in the activity log.

  Thin wrapper over `log_event` so the runner has a single, named call
  site for the skill-observability path and doesn't repeat the event
  vocabulary. A blank skill name is dropped — an empty chip carries no
  signal and would only pollute the "most-used" aggregation.
  """
  skill = (skill or "").strip()
  if not skill:
    return
  log_event("skill_loaded", ts=ts, chat_id=chat_id, skill=skill)


def most_used_skills(
  since: datetime,
  until: datetime,
) -> list[dict]:
  """Aggregates skill_loaded events into a most-used ranking.

  Scans the activity log over [since, until] and returns a list of
  `{"skill": <name>, "count": <int>}` dicts sorted by count descending,
  then by skill name for a stable tie-break. Reads through the same
  cross-archive scanner `read_events` uses, so a window that straddles
  a weekly rotation still sees every load.

  Returns an empty list when no skills loaded in the window — the
  caller renders an empty "no skills used yet" state rather than
  special-casing None.
  """
  counts: dict[str, int] = {}
  active = _activity_path()
  with _event_source_snapshot(active) as sources:
    for path, source in sources:
      for ev, ts in _yield_events_from_source(path, source):
        if ts < since or ts > until:
          continue
        if ev.get("ev") != "skill_loaded":
          continue
        skill = ev.get("skill")
        if not isinstance(skill, str) or not skill:
          continue
        counts[skill] = counts.get(skill, 0) + 1
  ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
  return [{"skill": skill, "count": count} for skill, count in ranked]


def read_events(
  since: datetime,
  until: datetime,
  app_id: int | None = None,
):
  """Yields event dicts from /data/logs/activity.jsonl AND every
  rotated archive (activity.YYYY-W##.jsonl) whose `ts` falls in
  [since, until] and (if `app_id` is given) whose `app_id` matches.

  Cross-file reads matter for the reflection-agent's "since 24h ago"
  window: a Monday-6am-UTC run lands after the Sunday-night rotation,
  so the active file holds only ~6 hours of Monday events; the prior
  ~18 hours live in last week's archive. Reading the active file
  alone dropped that window — fixed by enumerating archives via
  `_candidate_files` and chaining through them in mtime order.

  Generator so a large window doesn't buffer events into memory. Source
  descriptors are opened together for a rotation-safe snapshot, then each file
  is line-filtered and yielded in turn. Worst case (90-day retention, weekly
  rotation) is ~13 archive descriptors plus active — bounded.

  Malformed lines (corrupt JSON, missing ts) are skipped silently.
  """
  active = _activity_path()
  with _event_source_snapshot(active) as sources:
    for path, source in sources:
      for ev, ts in _yield_events_from_source(path, source):
        if ts < since or ts > until:
          continue
        if app_id is not None and ev.get("app_id") != app_id:
          continue
        yield ev
