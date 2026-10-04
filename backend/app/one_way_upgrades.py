"""One-way storage steps: the compatibility floor and the step gate.

See ONE_WAY_UPGRADES_DESIGN.md. A *one-way step* changes how existing data is
stored in a way older code cannot read. This module owns the pieces every such
step shares:

- the database's compatibility floor (``platform_compat``), read before
  ``create_all`` or any ledger migration so a too-old release changes nothing;
- the pre-``create_all`` table snapshot that tells a genuinely new database
  from a damaged or partial one;
- the ordered step registry and the startup gate that runs before the chat
  writer starts.

The first registered step converts chat transcripts to position-addressed
message rows; later steps share the same activation and recovery contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import shutil
import sqlite3
import threading
import time
import urllib.parse
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app import compat, sqlite_policy

log = logging.getLogger(__name__)


# The floor table and its single row are created together, in one transaction,
# never by create_all: a floor table without its row can then only mean damage.
COMPAT_TABLE_DDL = (
  "CREATE TABLE IF NOT EXISTS platform_compat ("
  "id INTEGER PRIMARY KEY CHECK (id = 1), "
  "floor INTEGER NOT NULL CHECK (floor >= 0), "
  "updated_at TEXT NOT NULL)"
)


@dataclass(frozen=True)
class Preflight:
  """What the database looked like before this boot changed anything."""

  floor: int
  existing_tables: frozenset[str]
  # "absent" (no floor table yet), "present", or "missing_row" (damaged).
  floor_record: str = "absent"
  missing_authority: tuple[str, ...] = ()


def _valid_level(value: object, name: str) -> int:
  if type(value) is not int or value < 0:
    raise ValueError(f"{name} holds {value!r}, not a non-negative integer")
  return value


def preflight(engine) -> Preflight:
  """Read the floor and the table list without writing anything.

  Uses raw SQL because the ORM tables may not exist yet: this runs before
  ``create_all``. A database without ``platform_compat`` has floor 0.
  """
  with engine.connect() as conn:
    tables = frozenset(
      row[0] for row in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master"
        " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
      )
    )
    floor = 0
    record = "absent"
    if "platform_compat" in tables:
      row = conn.exec_driver_sql(
        "SELECT floor FROM platform_compat WHERE id = 1"
      ).first()
      if row is None:
        record = "missing_row"
      else:
        record = "present"
        floor = _valid_level(row[0], "platform_compat.floor")
    # A missing or damaged floor row must not look safe: an active step's
    # level is a floor too.
    if "platform_upgrades" in tables:
      top = conn.exec_driver_sql(
        "SELECT MAX(level) FROM platform_upgrades WHERE state = 'active'"
      ).first()[0]
      if top is not None:
        floor = max(floor, _valid_level(top, "platform_upgrades.level"))
    conn.rollback()
  missing = tuple(sorted({
    table for step in registered_steps() if step.level <= floor
    for table in step.authoritative_tables if table not in tables
  }))
  return Preflight(
    floor=floor, existing_tables=tables, floor_record=record,
    missing_authority=missing,
  )


def boot_refusal(seen: Preflight) -> tuple[str, tuple[tuple[str, object], ...]] | None:
  """The reason and detail for refusing this database, or None to proceed."""
  if seen.floor_record == "missing_row":
    return "compat_floor_damaged", (
      (
        "message",
        "This database's compatibility record has lost its row, so it cannot "
        "be shown safe for this version. Restore the database through "
        "Recovery. Nothing was changed.",
      ),
    )
  detail = floor_refusal(seen.floor)
  if detail is not None:
    return "below_compatibility_floor", detail
  if seen.missing_authority:
    return "upgrade_authority_missing", (
      ("message", "This upgraded database is missing authoritative tables. "
       "Restore a complete database through Recovery. Nothing was changed."),
      ("missing_tables", seen.missing_authority),
    )
  return None


def ensure_compat_record(database_path: str, seen: Preflight) -> None:
  """Create the floor table and its row atomically when the table is absent.

  Runs on its own connection with an explicit ``BEGIN IMMEDIATE``: Python's
  sqlite3 driver (and so SQLAlchemy's default pysqlite transactions) opens no
  transaction before DDL, so without it ``CREATE TABLE`` would commit alone and
  a crash before the ``INSERT`` would leave the lone table that reads as
  damage. SQLite DDL is transactional inside an explicit transaction.

  The row carries the preflight floor, which already includes any active
  step's level. An existing record is never rewritten here.
  """
  if seen.floor_record != "absent":
    return
  conn = open_pinned(database_path)
  try:
    _begin_immediate(conn)
    try:
      conn.execute(COMPAT_TABLE_DDL)
      _insert_compat_row(conn, seen.floor)
      conn.execute("COMMIT")
    except BaseException:
      conn.execute("ROLLBACK")
      raise
  finally:
    conn.close()


def _insert_compat_row(conn: sqlite3.Connection, floor: int) -> None:
  conn.execute(
    "INSERT OR IGNORE INTO platform_compat (id, floor, updated_at)"
    " VALUES (1, ?, ?)",
    (floor, _now()),
  )


def floor_refusal(floor: int) -> tuple[tuple[str, object], ...] | None:
  """Degraded-boot detail when this code is below the database floor."""
  if floor <= compat.COMPAT_LEVEL:
    return None
  return (
    ("floor", floor),
    ("compat_level", compat.COMPAT_LEVEL),
    (
      "message",
      f"This database was upgraded to compatibility level {floor}; this "
      f"Möbius version understands only level {compat.COMPAT_LEVEL}. Return "
      "to a version at or above that level. Nothing was changed.",
    ),
  )


# --- steps and the startup gate ---------------------------------------------

# About this many raw legacy bytes are converted per committed transaction.
BATCH_BYTES = 20 * 1024 * 1024
# Full fingerprint passes per activation round, and activation rounds, before
# persistent change is treated as an unexpected concurrent writer.
MAX_PASSES = 3
MAX_ROUNDS = 3
PROGRESS_LOG_SECONDS = 5.0
# Free-space margin on top of the estimate: the WAL of the largest batch, and
# ten percent for indexes and page slack.
WAL_MARGIN_BATCHES = 2
SPACE_SLACK = 1.10

# Framework tables every step writes, keyed by (level, unit_id). The step's
# own new-form tables are listed on the step.
FRAMEWORK_UNIT_TABLES = ("upgrade_units", "upgrade_archive", "upgrade_quarantine")
# Framework bookkeeping only this module writes; ledger migrations must not.
FRAMEWORK_STATE_TABLES = ("platform_compat", "platform_upgrades", "upgrade_tasks")


class StepRefusal(RuntimeError):
  """The gate cannot finish; the boot stays unserviceable, legacy untouched.

  ``run_startup_tasks`` reports ``database_failure_reason`` and
  ``database_failure_detail`` instead of the task's generic reason.
  """

  def __init__(self, reason: str, message: str, **detail: object):
    super().__init__(message)
    self.database_failure_reason = reason
    self.database_failure_detail = (("message", message), *detail.items())


class DamagedUnit(Exception):
  """A unit's legacy value cannot be converted. It is quarantined, not lost."""


@dataclass(frozen=True)
class PostTask:
  """A post-activation task, run in bounded batches while the server serves.

  ``run_batch`` runs inside a transaction the framework opens and commits
  together with the task's progress row; it must not commit itself. It returns
  (units done in this batch, units remaining or None if unknown). Remaining 0
  means the task is complete.
  """

  name: str
  run_batch: Callable[[sqlite3.Connection], tuple[int, int | None]]
  # Bytes this batch may add to the database (WAL included) at most.
  batch_space_bytes: int | Callable[[sqlite3.Connection], int] = BATCH_BYTES * WAL_MARGIN_BATCHES


class OneWayStep:
  """One registered storage change older code cannot read.

  Subclasses implement the hooks; every hook must be idempotent. During
  PREPARING a step reads its legacy data and never modifies it, and never
  writes the unit table's rows (SQLite rewrites a whole row, large values
  included, on any update). See ONE_WAY_UPGRADES_DESIGN.md §2.
  """

  level: int = 0
  name: str = ""
  # Rows of this table are the step's units; deleting one deletes the unit's
  # step-owned copies through the lifecycle trigger.
  unit_table: str = ""
  unit_key: str = "id"
  # The step's own new-form tables and the column holding the unit key.
  owned_tables: tuple[tuple[str, str], ...] = ()
  # Once activated, these are authority, not rebuildable derived indexes.
  # Check their original presence before create_all can hide a partial restore.
  authoritative_tables: tuple[str, ...] = ()

  def legacy_present(self, conn: sqlite3.Connection) -> bool:
    raise NotImplementedError

  def create_owned_tables(self, conn: sqlite3.Connection) -> None:
    """Create every step-owned table (idempotent DDL)."""
    raise NotImplementedError

  def iter_unit_ids(self, conn: sqlite3.Connection) -> Iterable[str]:
    raise NotImplementedError

  def read_raw(self, conn: sqlite3.Connection, unit_id: str) -> bytes | None:
    """The unit's raw legacy value, or None if the unit no longer exists."""
    raise NotImplementedError

  def convert(self, conn: sqlite3.Connection, unit_id: str, raw: bytes) -> None:
    """Replace the unit's new-form rows. Raise DamagedUnit if unconvertible."""
    raise NotImplementedError

  def convert_damaged(self, conn: sqlite3.Connection, unit_id: str, error: str) -> None:
    """Replace the unit's new-form rows with a visible damaged placeholder."""
    raise NotImplementedError

  def prepared_matches(self, conn: sqlite3.Connection, unit_id: str, raw: bytes) -> bool:
    """Whether resumable new-form artifacts still represent this source unit."""
    return True

  def remove_unit(self, conn: sqlite3.Connection, unit_id: str) -> None:
    """Delete new-form rows for a unit that no longer exists."""
    raise NotImplementedError

  def activation_schema_edits(self, conn: sqlite3.Connection) -> None:
    """O(schema) edits only (the tripwire renames). Runs inside the lock."""
    raise NotImplementedError

  def post_tasks(self) -> tuple[PostTask, ...]:
    return ()


# The registry. Release F registers nothing; a step release appends its step
# and raises app.compat's levels in the same change.
_REGISTRY: list[OneWayStep] | None = None

_GATE_PASSED = False
# The gate has ended in this process, passed or refused. Only the gate raises
# the floor, so from here on the floor cannot change within this process.
_GATE_SETTLED = False


def registered_steps() -> tuple[OneWayStep, ...]:
  global _REGISTRY
  if _REGISTRY is None:
    # The concrete step stays import-light; its ORM imports live in hooks.
    from app.transcript_upgrade import TranscriptStep
    _REGISTRY = [TranscriptStep()]
  return tuple(sorted(_REGISTRY, key=lambda step: step.level))


def check_registry(steps: Sequence[OneWayStep]) -> None:
  """Levels are 1..n, unique, and match app.compat's declarations."""
  levels = [step.level for step in steps]
  if levels != list(range(1, len(levels) + 1)):
    raise ValueError(f"one-way step levels must be 1..n in order, got {levels}")
  top = levels[-1] if levels else 0
  if top != compat.REQUIRED_IMAGE_LEVEL or top > compat.COMPAT_LEVEL:
    raise ValueError(
      f"highest step level {top} must equal REQUIRED_IMAGE_LEVEL "
      f"{compat.REQUIRED_IMAGE_LEVEL} and not exceed COMPAT_LEVEL "
      f"{compat.COMPAT_LEVEL}"
    )


def assert_source_supported() -> None:
  """The import-time verdict: registry, declared levels, and the image.

  Database-free, so app.main can run it before anything opens the database
  (this module imports only the standard library, app.compat and
  app.sqlite_policy; a registered step module must stay equally light).
  """
  check_registry(registered_steps())
  compat.assert_image_supports_source()


def step_owned_tables(steps: Sequence[OneWayStep] | None = None) -> frozenset[str]:
  """Every table a later ledger migration must never write (the guardrail)."""
  steps = registered_steps() if steps is None else steps
  owned = set(FRAMEWORK_UNIT_TABLES) | set(FRAMEWORK_STATE_TABLES)
  for step in steps:
    owned.update(table for table, _column in step.owned_tables)
  return frozenset(owned)


def _now() -> str:
  return datetime.now(UTC).replace(tzinfo=None).isoformat(" ")


def open_pinned(database_path: str) -> sqlite3.Connection:
  """A dedicated connection for the gate, never a pooled checkout.

  ``PRAGMA data_version`` is only comparable on one physical connection, so
  the whole prepare/activate protocol runs on this one. Autocommit mode: the
  gate issues every BEGIN itself.
  """
  conn = sqlite3.connect(database_path, isolation_level=None, check_same_thread=False)
  for pragma in sqlite_policy.connection_pragmas():
    conn.execute(pragma).fetchall()
  return conn


def _data_version(conn: sqlite3.Connection) -> int:
  return conn.execute("PRAGMA data_version").fetchone()[0]


def _begin_immediate(conn: sqlite3.Connection, attempts: int = 3) -> None:
  for attempt in range(attempts):
    try:
      conn.execute("BEGIN IMMEDIATE")
      return
    except sqlite3.OperationalError as exc:
      if "locked" not in str(exc) and "busy" not in str(exc):
        raise
      if attempt == attempts - 1:
        raise StepRefusal(
          "upgrade_contended",
          "Another process kept the database locked during the upgrade.",
        ) from exc
      time.sleep(1.0)


def _state(conn: sqlite3.Connection, level: int) -> str | None:
  row = conn.execute(
    "SELECT state FROM platform_upgrades WHERE level = ?", (level,)
  ).fetchone()
  return None if row is None else row[0]


def _trigger_sql(step: OneWayStep) -> str:
  key = f"OLD.{step.unit_key}"
  statements = [
    f"DELETE FROM {table} WHERE level = {step.level} AND unit_id = CAST({key} AS TEXT);"
    for table in FRAMEWORK_UNIT_TABLES
  ]
  statements += [
    f"DELETE FROM {table} WHERE {column} = {key};"
    for table, column in step.owned_tables
  ]
  return (
    f"CREATE TRIGGER one_way_step_{step.level}_cleanup"
    f" AFTER DELETE ON {step.unit_table} BEGIN "
    + " ".join(statements) + " END"
  )


def install_cleanup_trigger(conn: sqlite3.Connection, step: OneWayStep) -> None:
  """(Re)create the step's lifecycle trigger. Call inside a transaction.

  It names only tables that exist for its whole life; whenever a step's owned
  tables change, replace it in the same transaction, since a trigger naming a
  missing table would make every delete of a unit row fail.
  """
  conn.execute(f"DROP TRIGGER IF EXISTS one_way_step_{step.level}_cleanup")
  conn.execute(_trigger_sql(step))


def _raise_floor(conn: sqlite3.Connection, level: int) -> None:
  conn.execute(
    "INSERT INTO platform_compat (id, floor, updated_at) VALUES (1, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET"
    " floor = MAX(floor, excluded.floor), updated_at = excluded.updated_at",
    (level, _now()),
  )


def _begin_step(conn: sqlite3.Connection, step: OneWayStep, *, activate: bool) -> None:
  """Create every step-owned table, install the trigger, record the state.

  ``activate`` is for a new database, which has nothing to convert.
  """
  _begin_immediate(conn)
  try:
    step.create_owned_tables(conn)
    install_cleanup_trigger(conn, step)
    conn.execute(
      "INSERT INTO platform_upgrades (level, name, state, started_at)"
      " VALUES (?, ?, 'preparing', ?)"
      " ON CONFLICT(level) DO NOTHING",
      (step.level, step.name, _now()),
    )
    if activate:
      _mark_active(conn, step)
    conn.execute("COMMIT")
  except BaseException:
    conn.execute("ROLLBACK")
    raise


def _mark_active(conn: sqlite3.Connection, step: OneWayStep) -> None:
  now = _now()
  tasks = step.post_tasks()
  conn.execute(
    "UPDATE platform_upgrades SET state = 'active', activated_at = ?"
    " WHERE level = ?",
    (now, step.level),
  )
  for task in tasks:
    conn.execute(
      "INSERT INTO upgrade_tasks (level, task, status, done_units, retries, updated_at)"
      " VALUES (?, ?, 'pending', 0, 0, ?) ON CONFLICT(level, task) DO NOTHING",
      (step.level, task.name, now),
    )
  if not tasks:
    conn.execute(
      "UPDATE platform_upgrades SET completed_at = ? WHERE level = ?",
      (now, step.level),
    )
  _raise_floor(conn, step.level)


def _free_bytes(database_path: str) -> int:
  return shutil.disk_usage(Path(database_path).parent).free


def _require_space(database_path: str, needed: int) -> None:
  free = _free_bytes(database_path)
  if free < needed:
    raise StepRefusal(
      "upgrade_needs_space",
      f"The upgrade needs {needed - free} more bytes of free disk space. "
      "Nothing was changed that older versions rely on.",
      needed_bytes=needed,
      free_bytes=free,
    )


def _fingerprints(conn: sqlite3.Connection, level: int) -> dict[str, tuple[str, int]]:
  return {
    unit_id: (sha, length)
    for unit_id, sha, length in conn.execute(
      "SELECT unit_id, sha256, raw_length FROM upgrade_units WHERE level = ?",
      (level,),
    )
  }


def _commit_batch(
  conn: sqlite3.Connection,
  step: OneWayStep,
  batch: list[tuple[str, bytes, str]],
  vanished: list[str],
) -> None:
  _begin_immediate(conn)
  try:
    now = _now()
    for unit_id in vanished:
      step.remove_unit(conn, unit_id)
      for table in FRAMEWORK_UNIT_TABLES:
        conn.execute(
          f"DELETE FROM {table} WHERE level = ? AND unit_id = ?",
          (step.level, unit_id),
        )
    for unit_id, raw, sha in batch:
      conn.execute(
        "DELETE FROM upgrade_quarantine WHERE level = ? AND unit_id = ?",
        (step.level, unit_id),
      )
      try:
        step.convert(conn, unit_id, raw)
      except DamagedUnit as exc:
        damaged = True
        conn.execute(
          "INSERT INTO upgrade_quarantine"
          " (level, unit_id, raw, raw_length, sha256, error, created_at)"
          " VALUES (?, ?, ?, ?, ?, ?, ?)",
          (step.level, unit_id, raw, len(raw), sha, str(exc) or type(exc).__name__, now),
        )
        step.convert_damaged(conn, unit_id, str(exc))
        # The quarantine copy stands in for the archive entry.
        conn.execute(
          "DELETE FROM upgrade_archive WHERE level = ? AND unit_id = ?",
          (step.level, unit_id),
        )
      else:
        damaged = False
        conn.execute(
          "INSERT INTO upgrade_archive (level, unit_id, zlib_raw, raw_length, sha256)"
          " VALUES (?, ?, ?, ?, ?)"
          " ON CONFLICT(level, unit_id) DO UPDATE SET zlib_raw = excluded.zlib_raw,"
          " raw_length = excluded.raw_length, sha256 = excluded.sha256",
          (step.level, unit_id, zlib.compress(raw, 1), len(raw), sha),
        )
      conn.execute(
        "INSERT INTO upgrade_units (level, unit_id, sha256, raw_length, damaged, converted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(level, unit_id) DO UPDATE SET sha256 = excluded.sha256,"
        " raw_length = excluded.raw_length, damaged = excluded.damaged,"
        " converted_at = excluded.converted_at",
        (step.level, unit_id, sha, len(raw), int(damaged), now),
      )
    conn.execute("COMMIT")
  except BaseException:
    conn.execute("ROLLBACK")
    raise


def _sync_units(
  conn: sqlite3.Connection,
  step: OneWayStep,
  database_path: str,
  clock: Callable[[], float] = time.monotonic,
) -> int:
  """One full fingerprint pass; convert what changed. Returns units converted.

  Commits in ~BATCH_BYTES batches, so a killed boot keeps its progress.
  """
  known = _fingerprints(conn, step.level)
  current = list(step.iter_unit_ids(conn))
  vanished = sorted(set(known) - set(current))
  batch: list[tuple[str, bytes, str]] = []
  batch_bytes = 0
  converted = 0
  hashed_bytes = 0
  last_log = clock()

  def flush() -> None:
    nonlocal batch, batch_bytes, vanished, converted
    if not batch and not vanished:
      return
    _require_space(
      database_path,
      int(batch_bytes * SPACE_SLACK) + BATCH_BYTES * WAL_MARGIN_BATCHES,
    )
    _commit_batch(conn, step, batch, vanished)
    converted += len(batch) + len(vanished)
    batch, batch_bytes, vanished = [], 0, []

  for unit_id in current:
    raw = step.read_raw(conn, unit_id)
    if raw is None:
      continue
    hashed_bytes += len(raw)
    sha = hashlib.sha256(raw).hexdigest()
    complete = (
      known.get(unit_id) == (sha, len(raw))
      and verified_legacy_copy(conn, step.level, unit_id) == raw
      and step.prepared_matches(conn, unit_id, raw)
    )
    if not complete:
      batch.append((unit_id, raw, sha))
      batch_bytes += len(raw)
      if batch_bytes >= BATCH_BYTES:
        flush()
    if clock() - last_log >= PROGRESS_LOG_SECONDS:
      last_log = clock()
      log.info(
        "one-way step %s: checked %.0f MB, converted %d units",
        step.name, hashed_bytes / 1e6, converted,
      )
  flush()
  return converted


def _prepare_and_activate(
  conn: sqlite3.Connection, step: OneWayStep, database_path: str,
) -> None:
  """Convert until a pass is clean, then activate under a short lock.

  The lock only verifies that no other connection committed since the clean
  pass began (``PRAGMA data_version``) and applies O(schema) edits. Persistent
  change fails closed as contention; there is no data-sized locked fallback.
  """
  for _round in range(MAX_ROUNDS):
    before = _data_version(conn)
    for _pass in range(MAX_PASSES):
      if _sync_units(conn, step, database_path) == 0:
        break
    else:
      raise StepRefusal(
        "upgrade_contended",
        f"Data kept changing while upgrading {step.name}; another process is "
        "writing the database.",
        level=step.level,
      )
    _begin_immediate(conn)
    try:
      if _data_version(conn) != before:
        conn.execute("ROLLBACK")
        continue
      step.activation_schema_edits(conn)
      _mark_active(conn, step)
      conn.execute("COMMIT")
      log.info("one-way step %s (level %d) is active", step.name, step.level)
      return
    except BaseException:
      if conn.in_transaction:
        conn.execute("ROLLBACK")
      raise
  raise StepRefusal(
    "upgrade_contended",
    f"Another process kept committing while activating {step.name}.",
    level=step.level,
  )


HOST_RECOVERY_PROOF = Path("/run/mobius-rebuild-active.json")


def assert_host_recovery_supports(level: int) -> None:
  """Require this boot's root proof of the installed recovery owner.

  The installer pins deployment intent and a read-only Host-state mount in
  Compose, independent of app-writable /data. Root entrypoint verifies actual
  workers.json and ACTIVE bytes before importing application code. An old
  controller without this setup may not activate new authority either.
  """
  from app.config import get_settings
  settings = get_settings()
  control = Path(settings.data_dir) / "mobius-rebuild"
  required = settings.mobius_host_recovery_required
  # lexists also catches broken symlinks; directory loss cannot exempt an
  # explicitly configured Host, and old installations require installer setup.
  if not required and not os.path.lexists(control):
    return
  try:
    if not required:
      raise ValueError("Old Host installation lacks the recovery prerequisite")
    path = HOST_RECOVERY_PROOF
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
        or info.st_mode & 0o022):
      raise ValueError("Host proof is not root-owned")
    proof = json.loads(path.read_text(encoding="utf-8"))
    boot_id = os.environ.get("MOBIUS_BOOT_ID")
    if not isinstance(proof, dict) or not boot_id or proof.get("boot_id") != boot_id:
      raise ValueError("Host proof belongs to another boot")
    active = proof["active_worker"]
    if (not isinstance(active, dict) or type(active.get("revision")) is not int
        or active["revision"] < 4 or type(active.get("rollback_floor_level")) is not int
        or active["rollback_floor_level"] < level
        or not isinstance(active.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", active["sha256"]) is None):
      raise ValueError("No verified active recovery capability")
  except (OSError, ValueError, TypeError, KeyError):
    raise StepRefusal(
      "host_helper_outdated",
      "Update the Host replacement helper from this reviewed checkout before "
      "converting storage. Its ACTIVE recovery worker must understand the "
      "database floor; a trial candidate is not enough. Legacy data is unchanged.",
      required_rollback_floor_level=level,
    ) from None


def run_gate(
  database_path: str,
  existing_tables: frozenset[str] | None,
  steps: Sequence[OneWayStep] | None = None,
) -> None:
  """Bring every registered step to ACTIVE before the chat writer starts.

  Raises StepRefusal when that is not possible; the boot then stays
  unserviceable while the legacy data remains authoritative and untouched.
  """
  global _GATE_SETTLED
  try:
    _run_gate(database_path, existing_tables, steps)
  finally:
    _GATE_SETTLED = True


def _run_gate(
  database_path: str,
  existing_tables: frozenset[str] | None,
  steps: Sequence[OneWayStep] | None,
) -> None:
  global _GATE_PASSED
  steps = registered_steps() if steps is None else tuple(steps)
  check_registry(steps)
  if not steps:
    _GATE_PASSED = True
    return
  # Only a database that had no application tables at all before this boot's
  # create_all is new. Anything else missing a step's legacy structure is
  # damaged or partially restored.
  fresh = existing_tables is not None and not existing_tables
  conn = open_pinned(database_path)
  try:
    for step in steps:
      state = _state(conn, step.level)
      if state == "active":
        continue
      assert_host_recovery_supports(step.level)
      if state is None and fresh:
        _begin_step(conn, step, activate=True)
        continue
      if not step.legacy_present(conn):
        raise StepRefusal(
          "upgrade_schema_inconsistent",
          f"The database has application tables but no legacy data for "
          f"{step.name}, and that step never completed. It may be damaged or "
          "partially restored; restore it before starting this version.",
          level=step.level,
        )
      _begin_step(conn, step, activate=False)
      _prepare_and_activate(conn, step, database_path)
  finally:
    conn.close()
  _GATE_PASSED = True


_REPORTED_FLOOR: int | None = None


def reported_floor() -> int | None:
  """The database's effective compatibility floor for deployment controllers.

  The account service reads it from /api/health to decide whether rolling a
  failed replacement back to an older image is safe. Only the startup gate
  raises the floor, so once it has ended -- passed, or refused with the
  legacy data still authoritative -- this process can no longer change it. A
  pass caches the value; a refusal reports the floor it left (usually the old
  one, read fresh each time), letting a controller restore the previous image
  instead of stranding the instance. Another process on the same database
  could still raise it later, so a controller must only trust the value from
  the image it is deciding about. None means unknown (the gate is still running, the database is
  unreadable, or the floor record is damaged): callers must then treat an
  older image as unsafe.
  """
  global _REPORTED_FLOOR
  if _REPORTED_FLOOR is not None:
    return _REPORTED_FLOOR
  if registered_steps() and not _GATE_SETTLED:
    # A conversion may still raise the floor in this very process: the value
    # read now could be stale a moment later, so it is unknown until the gate
    # has ended.
    return None
  try:
    from app.database import engine
    seen = preflight(engine)
  except Exception:
    return None
  if seen.floor_record == "missing_row":
    return None
  if _GATE_PASSED:
    # A refused process re-reads instead: another process on the same database
    # (a recovery or trial container) could still raise the floor after it.
    _REPORTED_FLOOR = seen.floor
  return seen.floor


def readiness_verdict() -> dict | None:
  """Not ready while registered steps exist and the gate has not passed."""
  if registered_steps() and not _GATE_PASSED:
    return {"reason": "one_way_upgrade_pending"}
  return None


# --- post-activation tasks ----------------------------------------------------


def verified_legacy_copy(conn: sqlite3.Connection, level: int, unit_id: str) -> bytes | None:
  """The unit's preserved raw legacy value, or None unless it fully verifies.

  Reads the archive entry (or, for a damaged unit, its quarantine copy) with
  bounded decompression and requires its length and SHA-256 to match both the
  copy's own metadata and the unit's fingerprint. A post-activation task may
  clear a hot legacy value only when this returns exactly that value, checked
  in the same transaction as the clear.
  """
  fingerprint = conn.execute(
    "SELECT sha256, raw_length FROM upgrade_units WHERE level = ? AND unit_id = ?",
    (level, unit_id),
  ).fetchone()
  if fingerprint is None:
    return None
  archived = conn.execute(
    "SELECT zlib_raw, raw_length, sha256 FROM upgrade_archive"
    " WHERE level = ? AND unit_id = ?",
    (level, unit_id),
  ).fetchone()
  if archived is not None:
    compressed, length, sha = archived
    inflater = zlib.decompressobj()
    try:
      raw = inflater.decompress(bytes(compressed), length + 1)
    except zlib.error:
      return None
    if not inflater.eof:
      return None
  else:
    quarantined = conn.execute(
      "SELECT raw, raw_length, sha256 FROM upgrade_quarantine"
      " WHERE level = ? AND unit_id = ?",
      (level, unit_id),
    ).fetchone()
    if quarantined is None:
      return None
    raw, length, sha = bytes(quarantined[0]), quarantined[1], quarantined[2]
  if (
    len(raw) != length
    or (sha, length) != (fingerprint[0], fingerprint[1])
    or hashlib.sha256(raw).hexdigest() != sha
  ):
    return None
  return raw


def _task_status(conn: sqlite3.Connection, level: int, name: str) -> str | None:
  row = conn.execute(
    "SELECT status FROM upgrade_tasks WHERE level = ? AND task = ?", (level, name),
  ).fetchone()
  return None if row is None else row[0]


def run_post_activation_batch(
  database_path: str,
  steps: Sequence[OneWayStep] | None = None,
) -> bool:
  """Run one batch of the first unfinished task. Returns False when all done.

  Verification, the data change, and the progress row commit together, so a
  kill at any point leaves consistent, resumable progress.
  """
  steps = registered_steps() if steps is None else tuple(steps)
  conn = open_pinned(database_path)
  try:
    for step in steps:
      if _state(conn, step.level) != "active":
        continue
      for task in step.post_tasks():
        if _task_status(conn, step.level, task.name) == "done":
          continue
        space = task.batch_space_bytes(conn) if callable(task.batch_space_bytes) else task.batch_space_bytes
        _require_space(database_path, space)
        # The progress row must exist outside the batch transaction, so a
        # failure can still record its error and a success can never be lost.
        conn.execute(
          "INSERT INTO upgrade_tasks (level, task, status, done_units, retries, updated_at)"
          " VALUES (?, ?, 'pending', 0, 0, ?) ON CONFLICT(level, task) DO NOTHING",
          (step.level, task.name, _now()),
        )
        _begin_immediate(conn)
        try:
          done, remaining = task.run_batch(conn)
          finished = remaining == 0
          progress = conn.execute(
            "UPDATE upgrade_tasks SET status = ?, done_units = done_units + ?,"
            " remaining_units = ?, last_error = NULL, updated_at = ?,"
            " completed_at = ? WHERE level = ? AND task = ?",
            (
              "done" if finished else "running", done, remaining, _now(),
              _now() if finished else None, step.level, task.name,
            ),
          )
          if progress.rowcount != 1:
            raise RuntimeError(
              f"progress for {step.name}/{task.name} was not recorded"
            )
          conn.execute("COMMIT")
        except BaseException as exc:
          if conn.in_transaction:
            conn.execute("ROLLBACK")
          conn.execute(
            "UPDATE upgrade_tasks SET retries = retries + 1, last_error = ?,"
            " updated_at = ? WHERE level = ? AND task = ?",
            (f"{type(exc).__name__}: {exc}"[:2000], _now(), step.level, task.name),
          )
          raise
        return True
      conn.execute(
        "UPDATE platform_upgrades SET completed_at = ? WHERE level = ?"
        " AND completed_at IS NULL",
        (_now(), step.level),
      )
    return False
  finally:
    conn.close()


def upgrade_status(database_path: str) -> list[dict]:
  """Owner-visible progress: every step's state and its tasks' progress."""
  conn = sqlite3.connect(
    f"file:{urllib.parse.quote(database_path)}?mode=ro", uri=True,
  )
  try:
    tables = {row[0] for row in conn.execute(
      "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    if "platform_upgrades" not in tables:
      return []
    steps = []
    for level, name, state, activated_at, completed_at in conn.execute(
      "SELECT level, name, state, activated_at, completed_at"
      " FROM platform_upgrades ORDER BY level"
    ).fetchall():
      tasks = [
        {
          "task": task, "status": status, "done_units": done,
          "remaining_units": remaining, "retries": retries, "last_error": error,
        }
        for task, status, done, remaining, retries, error in conn.execute(
          "SELECT task, status, done_units, remaining_units, retries, last_error"
          " FROM upgrade_tasks WHERE level = ? ORDER BY task", (level,),
        )
      ]
      steps.append({
        "level": level, "name": name, "state": state,
        "activated_at": activated_at, "completed_at": completed_at,
        "tasks": tasks,
      })
    return steps
  finally:
    conn.close()


def start_post_activation_worker(
  database_path: str,
  steps: Sequence[OneWayStep] | None = None,
  *,
  sleep: Callable[[float], None] = time.sleep,
) -> threading.Thread | None:
  """Finish post-activation tasks in the background while the server serves.

  Retries with capped exponential backoff; progress and the last error stay
  durable in ``upgrade_tasks`` (see ``upgrade_status``). Returns None when no
  registered step has post-activation tasks, as in release F.
  """
  steps = registered_steps() if steps is None else tuple(steps)
  if not any(step.post_tasks() for step in steps):
    return None

  def loop() -> None:
    delay = 1.0
    while True:
      try:
        if not run_post_activation_batch(database_path, steps):
          log.info("one-way post-activation tasks are complete")
          return
        delay = 1.0
      except Exception:
        log.exception("one-way post-activation batch failed; retrying in %.0fs", delay)
        sleep(delay)
        delay = min(delay * 2, 300.0)

  worker = threading.Thread(target=loop, name="one-way-post-activation", daemon=True)
  worker.start()
  return worker
