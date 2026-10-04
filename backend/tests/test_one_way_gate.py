"""The one-way step gate, exercised with a test-only toy step.

ONE_WAY_UPGRADES_DESIGN.md §2. These tests register a small step against
its own database to pin the framework's
guarantees: legacy data untouched until activation, resumable fingerprinted
conversion, a short schema-only activation lock, fail-closed refusals, the
cleanup trigger, and post-activation completion.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import zlib

import pytest
from sqlalchemy import create_engine

from app import compat, models, one_way_upgrades as owu
from app.startup import (
  DatabaseBootResult,
  StartupContext,
  StartupTask,
  run_startup_tasks,
)


FRAMEWORK_MODELS = (
  models.PlatformUpgrade,
  models.UpgradeTask,
  models.UpgradeQuarantine,
  models.UpgradeUnit,
  models.UpgradeArchive,
)


def _columns(conn, table):
  return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _clear_legacy_batch(conn):
  """Toy post-activation task: empty hot legacy values once verified."""
  rows = conn.execute(
    "SELECT id, CAST(legacy_v1 AS BLOB) FROM toy_units"
    " WHERE legacy_v1 != '[]' ORDER BY id LIMIT 2"
  ).fetchall()
  done = 0
  for unit_id, current in rows:
    copy = owu.verified_legacy_copy(conn, ToyStep.level, unit_id)
    if copy is not None and copy == bytes(current):
      conn.execute("UPDATE toy_units SET legacy_v1 = '[]' WHERE id = ?", (unit_id,))
      done += 1
  remaining = conn.execute(
    "SELECT COUNT(*) FROM toy_units WHERE legacy_v1 != '[]'"
  ).fetchone()[0]
  return done, remaining


class ToyStep(owu.OneWayStep):
  """Moves each toy unit's JSON list into one row per item."""

  level = 1
  name = "toy"
  unit_table = "toy_units"
  unit_key = "id"
  owned_tables = (("toy_rows", "unit_id"),)

  def __init__(self):
    self.fail_on = None
    self.edits = []

  def legacy_present(self, conn):
    return "legacy" in _columns(conn, "toy_units")

  def create_owned_tables(self, conn):
    conn.execute(
      "CREATE TABLE IF NOT EXISTS toy_rows (unit_id TEXT NOT NULL,"
      " seq INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY (unit_id, seq))"
    )

  def iter_unit_ids(self, conn):
    return [row[0] for row in conn.execute("SELECT id FROM toy_units ORDER BY id")]

  def read_raw(self, conn, unit_id):
    row = conn.execute(
      "SELECT CAST(legacy AS BLOB) FROM toy_units WHERE id = ?", (unit_id,),
    ).fetchone()
    return None if row is None else bytes(row[0])

  def convert(self, conn, unit_id, raw):
    if unit_id == self.fail_on:
      raise RuntimeError("simulated crash")
    try:
      items = json.loads(raw)
    except ValueError as exc:
      raise owu.DamagedUnit(f"not JSON: {exc}") from exc
    if not isinstance(items, list):
      raise owu.DamagedUnit("not a list")
    self.remove_unit(conn, unit_id)
    conn.executemany(
      "INSERT INTO toy_rows (unit_id, seq, body) VALUES (?, ?, ?)",
      [(unit_id, seq, json.dumps(item)) for seq, item in enumerate(items)],
    )

  def convert_damaged(self, conn, unit_id, error):
    self.remove_unit(conn, unit_id)
    conn.execute(
      "INSERT INTO toy_rows (unit_id, seq, body) VALUES (?, 0, ?)",
      (unit_id, json.dumps({"damaged": error})),
    )

  def remove_unit(self, conn, unit_id):
    conn.execute("DELETE FROM toy_rows WHERE unit_id = ?", (unit_id,))

  def activation_schema_edits(self, conn):
    self.edits.append((conn.in_transaction, conn.total_changes))
    conn.execute("ALTER TABLE toy_units RENAME COLUMN legacy TO legacy_v1")

  def post_tasks(self):
    return (owu.PostTask("clear_legacy", _clear_legacy_batch, batch_space_bytes=0),)


@pytest.fixture(autouse=True)
def _step_levels(monkeypatch):
  monkeypatch.setattr(compat, "COMPAT_LEVEL", 1)
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", 1)
  monkeypatch.setattr(owu, "_GATE_PASSED", False)
  monkeypatch.setattr(owu, "_GATE_SETTLED", False)
  monkeypatch.setattr(owu, "_REPORTED_FLOOR", None)
  monkeypatch.setattr(owu.time, "sleep", lambda _s: None)


def _database(tmp_path, units=None):
  path = str(tmp_path / "gate.db")
  engine = create_engine(f"sqlite:///{path}")
  models.Base.metadata.create_all(engine, tables=[m.__table__ for m in FRAMEWORK_MODELS])
  engine.dispose()
  conn = sqlite3.connect(path)
  # As F's first boot leaves it: the floor table with its single row.
  conn.execute(owu.COMPAT_TABLE_DDL)
  conn.execute("INSERT INTO platform_compat VALUES (1, 0, 'boot')")
  conn.execute("CREATE TABLE toy_units (id TEXT PRIMARY KEY, legacy TEXT NOT NULL)")
  conn.executemany("INSERT INTO toy_units VALUES (?, ?)", list((units or {}).items()))
  conn.commit()
  conn.close()
  return path


def _query(path, sql, params=()):
  conn = sqlite3.connect(path)
  try:
    return conn.execute(sql, params).fetchall()
  finally:
    conn.close()


def _rows(path, unit_id):
  return [json.loads(body) for (body,) in _query(
    path, "SELECT body FROM toy_rows WHERE unit_id = ? ORDER BY seq", (unit_id,),
  )]


def _state(path):
  rows = _query(path, "SELECT state FROM platform_upgrades WHERE level = 1")
  return rows[0][0] if rows else None


def _floor(path):
  rows = _query(path, "SELECT floor FROM platform_compat WHERE id = 1")
  return rows[0][0] if rows else 0


EXISTING = frozenset({"toy_units", "platform_compat"})


# --- registry ------------------------------------------------------------------


def test_release_f_registers_no_steps_and_passes_the_gate(tmp_path, monkeypatch):
  # Preserve the level-zero bridge contract even though this release now
  # registers the concrete level-one transcript step by default.
  monkeypatch.setattr(owu, "_REGISTRY", [])
  monkeypatch.setattr(compat, "COMPAT_LEVEL", 0)
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", 0)
  assert owu.registered_steps() == ()
  owu.check_registry(owu.registered_steps())
  owu.run_gate(str(tmp_path / "unused.db"), frozenset({"chats"}))
  assert owu.readiness_verdict() is None


def test_registry_levels_must_match_the_compat_declarations(monkeypatch):
  owu.check_registry([ToyStep()])
  second = ToyStep()
  second.level = 3
  with pytest.raises(ValueError):
    owu.check_registry([ToyStep(), second])
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", 0)
  with pytest.raises(ValueError):
    owu.check_registry([ToyStep()])


def test_step_owned_tables_include_framework_and_step_tables():
  assert owu.step_owned_tables([ToyStep()]) >= {
    "toy_rows", "upgrade_units", "upgrade_archive", "upgrade_quarantine",
  }


# --- the happy path ----------------------------------------------------------


def test_existing_database_converts_then_activates(tmp_path):
  units = {"a": json.dumps([1, 2]), "b": json.dumps(["x"])}
  path = _database(tmp_path, units)
  step = ToyStep()

  owu.run_gate(path, EXISTING, [step])

  assert _state(path) == "active"
  assert _floor(path) == 1
  assert _rows(path, "a") == [1, 2] and _rows(path, "b") == ["x"]
  # The tripwire rename happened, and the legacy values themselves did not change.
  assert _query(path, "SELECT id, legacy_v1 FROM toy_units ORDER BY id") == sorted(units.items())
  # Every unit is fingerprinted and archived byte-for-byte.
  for unit_id, raw in units.items():
    (sha, length), = _query(
      path, "SELECT sha256, raw_length FROM upgrade_units WHERE level = 1 AND unit_id = ?",
      (unit_id,),
    )
    assert (sha, length) == (hashlib.sha256(raw.encode()).hexdigest(), len(raw))
    (compressed,), = _query(
      path, "SELECT zlib_raw FROM upgrade_archive WHERE level = 1 AND unit_id = ?", (unit_id,),
    )
    assert zlib.decompress(compressed) == raw.encode()
  assert owu.readiness_verdict() is None or not owu._REGISTRY


def test_the_lock_holds_only_schema_edits_and_bookkeeping(tmp_path, monkeypatch):
  path = _database(tmp_path, {f"u{i}": json.dumps(list(range(50))) for i in range(20)})
  step = ToyStep()
  marks = {}
  real_mark = owu._mark_active

  def mark(conn, the_step):
    real_mark(conn, the_step)
    marks["after"] = conn.total_changes

  monkeypatch.setattr(owu, "_mark_active", mark)
  owu.run_gate(path, EXISTING, [step])
  (in_transaction, before), = step.edits
  assert in_transaction
  # State row, floor row and one task row: never data-sized work.
  assert marks["after"] - before <= 3


def test_prepare_never_writes_the_unit_rows(tmp_path):
  path = _database(tmp_path, {"a": "[1]", "b": "[2]"})
  conn = sqlite3.connect(path)
  conn.execute("CREATE TABLE audit (op TEXT)")
  conn.execute(
    "CREATE TRIGGER audit_toy AFTER UPDATE ON toy_units BEGIN"
    " INSERT INTO audit VALUES ('update'); END"
  )
  conn.commit()
  conn.close()
  owu.run_gate(path, EXISTING | {"audit"}, [ToyStep()])
  assert _query(path, "SELECT COUNT(*) FROM audit") == [(0,)]


def test_fresh_database_goes_straight_to_active(tmp_path):
  path = _database(tmp_path)
  conn = sqlite3.connect(path)
  # A fresh schema never had the legacy column.
  conn.execute("DROP TABLE toy_units")
  conn.execute("CREATE TABLE toy_units (id TEXT PRIMARY KEY, legacy_v1 TEXT NOT NULL DEFAULT '[]')")
  conn.commit()
  conn.close()
  owu.run_gate(path, frozenset(), [ToyStep()])
  assert _state(path) == "active"
  assert _floor(path) == 1
  assert _query(path, "SELECT name FROM sqlite_master WHERE name = 'toy_rows'") == [("toy_rows",)]
  assert _query(
    path, "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'one_way_step_1_cleanup'",
  ) == [("one_way_step_1_cleanup",)]


def test_already_active_steps_are_skipped(tmp_path):
  path = _database(tmp_path, {"a": "[1]"})
  owu.run_gate(path, EXISTING, [ToyStep()])
  again = ToyStep()
  owu.run_gate(path, EXISTING, [again])
  assert again.edits == []


# --- fail closed, lossless ----------------------------------------------------


def test_damaged_unit_is_quarantined_and_does_not_block(tmp_path):
  path = _database(tmp_path, {"good": "[1]", "bad": "{not json"})
  owu.run_gate(path, EXISTING, [ToyStep()])
  assert _state(path) == "active"
  assert _rows(path, "good") == [1]
  assert "damaged" in _rows(path, "bad")[0]
  (raw, length, sha), = _query(
    path, "SELECT raw, raw_length, sha256 FROM upgrade_quarantine WHERE unit_id = 'bad'",
  )
  assert bytes(raw) == b"{not json" and length == 9
  assert sha == hashlib.sha256(b"{not json").hexdigest()
  assert _query(path, "SELECT COUNT(*) FROM upgrade_archive WHERE unit_id = 'bad'") == [(0,)]


def test_a_crash_mid_prepare_resumes_without_duplicates(tmp_path, monkeypatch):
  units = {f"u{i:02d}": json.dumps([i, i]) for i in range(10)}
  path = _database(tmp_path, units)
  monkeypatch.setattr(owu, "BATCH_BYTES", 16)
  step = ToyStep()
  step.fail_on = "u06"
  with pytest.raises(RuntimeError, match="simulated crash"):
    owu.run_gate(path, EXISTING, [step])
  assert _state(path) == "preparing"
  assert _query(path, "SELECT COUNT(*) FROM upgrade_units") [0][0] > 0
  # Legacy data is untouched and still under its old name.
  assert "legacy" in _columns(sqlite3.connect(path), "toy_units")

  step.fail_on = None
  owu.run_gate(path, EXISTING, [step])
  assert _state(path) == "active"
  assert _query(path, "SELECT COUNT(*) FROM toy_rows") == [(20,)]
  for unit_id, raw in units.items():
    assert _rows(path, unit_id) == json.loads(raw)


def test_older_code_writes_during_preparing_are_reconverted(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]", "b": "[2]", "c": "[3]"})
  monkeypatch.setattr(owu, "BATCH_BYTES", 1)
  step = ToyStep()
  step.fail_on = "c"
  with pytest.raises(RuntimeError):
    owu.run_gate(path, EXISTING, [step])
  # Older code serves meanwhile: a same-length raw write with no timestamp,
  # a new unit, and a hard purge (which fires the cleanup trigger).
  conn = sqlite3.connect(path)
  conn.execute("UPDATE toy_units SET legacy = '[9]' WHERE id = 'a'")
  conn.execute("INSERT INTO toy_units VALUES ('d', '[4]')")
  conn.execute("DELETE FROM toy_units WHERE id = 'b'")
  conn.commit()
  conn.close()

  step.fail_on = None
  owu.run_gate(path, EXISTING, [step])
  assert _rows(path, "a") == [9]
  assert _rows(path, "d") == [4]
  assert _rows(path, "b") == []
  for table in ("upgrade_units", "upgrade_archive"):
    assert _query(path, f"SELECT COUNT(*) FROM {table} WHERE unit_id = 'b'") == [(0,)]


def test_a_commit_by_another_connection_forces_another_round(tmp_path, monkeypatch):
  """A write that lands after a clean pass, just before the lock, is caught."""
  path = _database(tmp_path, {"a": "[1]"})
  real_sync = owu._sync_units
  passes = []

  def sync(conn, step, database_path, *args, **kwargs):
    converted = real_sync(conn, step, database_path, *args, **kwargs)
    passes.append(converted)
    if converted == 0 and passes.count(0) == 1:
      other = sqlite3.connect(path)
      other.execute("UPDATE toy_units SET legacy = '[5]' WHERE id = 'a'")
      other.commit()
      other.close()
    return converted

  monkeypatch.setattr(owu, "_sync_units", sync)
  step = ToyStep()
  owu.run_gate(path, EXISTING, [step])
  assert _state(path) == "active"
  assert _rows(path, "a") == [5]
  # Round 1: convert, clean, lock refused. Round 2: reconvert, clean, activate.
  assert passes == [1, 0, 1, 0]
  assert len(step.edits) == 1


def test_persistent_change_fails_closed_as_contention(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]"})
  versions = iter(range(100))
  monkeypatch.setattr(owu, "_data_version", lambda _conn: next(versions))
  with pytest.raises(owu.StepRefusal) as refused:
    owu.run_gate(path, EXISTING, [ToyStep()])
  assert refused.value.database_failure_reason == "upgrade_contended"
  assert _state(path) == "preparing"
  assert "legacy" in _columns(sqlite3.connect(path), "toy_units")


def test_a_held_write_lock_fails_closed(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]"})
  monkeypatch.setattr(
    owu.sqlite_policy, "connection_pragmas",
    lambda: ("PRAGMA busy_timeout=20", "PRAGMA journal_mode=WAL"),
  )
  holder = sqlite3.connect(path, isolation_level=None)
  holder.execute("PRAGMA journal_mode=WAL")
  holder.execute("BEGIN IMMEDIATE")
  try:
    with pytest.raises(owu.StepRefusal) as refused:
      owu.run_gate(path, EXISTING, [ToyStep()])
  finally:
    holder.execute("ROLLBACK")
    holder.close()
  assert refused.value.database_failure_reason == "upgrade_contended"


def test_missing_legacy_on_an_existing_database_fails_closed(tmp_path):
  path = _database(tmp_path)
  conn = sqlite3.connect(path)
  conn.execute("ALTER TABLE toy_units RENAME COLUMN legacy TO something_else")
  conn.commit()
  conn.close()
  with pytest.raises(owu.StepRefusal) as refused:
    owu.run_gate(path, EXISTING, [ToyStep()])
  assert refused.value.database_failure_reason == "upgrade_schema_inconsistent"
  assert _state(path) is None


def test_short_disk_space_fails_closed_before_changing_anything(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]"})
  monkeypatch.setattr(owu, "_free_bytes", lambda _path: 10)
  with pytest.raises(owu.StepRefusal) as refused:
    owu.run_gate(path, EXISTING, [ToyStep()])
  assert refused.value.database_failure_reason == "upgrade_needs_space"
  assert dict(refused.value.database_failure_detail)["free_bytes"] == 10
  assert _query(path, "SELECT COUNT(*) FROM toy_rows") == [(0,)]
  assert "legacy" in _columns(sqlite3.connect(path), "toy_units")


# --- the cleanup trigger ------------------------------------------------------


def test_deleting_a_unit_removes_every_step_owned_copy_in_each_phase(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]", "b": "{bad", "c": "[3]", "d": "[4]"})
  monkeypatch.setattr(owu, "BATCH_BYTES", 1)
  step = ToyStep()
  step.fail_on = "d"
  with pytest.raises(RuntimeError):
    owu.run_gate(path, EXISTING, [step])

  def copies(unit_id):
    return sum(
      _query(path, f"SELECT COUNT(*) FROM {table} WHERE unit_id = ?", (unit_id,))[0][0]
      for table in ("toy_rows", "upgrade_units", "upgrade_archive", "upgrade_quarantine")
    )

  assert copies("a") and copies("b")
  conn = sqlite3.connect(path)
  conn.execute("DELETE FROM toy_units WHERE id IN ('a', 'b')")  # during PREPARING
  conn.commit()
  conn.close()
  assert copies("a") == 0 and copies("b") == 0

  step.fail_on = None
  owu.run_gate(path, EXISTING, [step])
  conn = sqlite3.connect(path)
  conn.execute("DELETE FROM toy_units WHERE id = 'c'")  # after activation
  conn.commit()
  conn.close()
  assert copies("c") == 0 and copies("d") > 0


# --- post-activation completion -----------------------------------------------


def test_post_activation_clears_verified_values_and_completes(tmp_path):
  units = {"a": "[1]", "b": "[2]", "c": "{bad"}
  path = _database(tmp_path, units)
  step = ToyStep()
  owu.run_gate(path, EXISTING, [step])
  assert _query(path, "SELECT status FROM upgrade_tasks") == [("pending",)]

  batches = 0
  while owu.run_post_activation_batch(path, [step]):
    batches += 1
  assert batches == 2
  assert _query(path, "SELECT legacy_v1 FROM toy_units ORDER BY id") == [("[]",), ("[]",), ("[]",)]
  (status, done, remaining), = _query(
    path, "SELECT status, done_units, remaining_units FROM upgrade_tasks",
  )
  assert (status, done, remaining) == ("done", 3, 0)
  assert _query(path, "SELECT completed_at IS NOT NULL FROM platform_upgrades") == [(1,)]
  assert owu.upgrade_status(path)[0]["tasks"][0]["status"] == "done"


def test_a_mismatched_value_is_never_cleared(tmp_path):
  path = _database(tmp_path, {"a": "[1]"})
  step = ToyStep()
  owu.run_gate(path, EXISTING, [step])
  conn = sqlite3.connect(path)
  conn.execute("UPDATE upgrade_archive SET zlib_raw = ?", (zlib.compress(b"[2]"),))
  conn.commit()
  conn.close()
  owu.run_post_activation_batch(path, [step])
  assert _query(path, "SELECT legacy_v1 FROM toy_units") == [("[1]",)]
  assert _query(path, "SELECT status, remaining_units FROM upgrade_tasks") == [("running", 1)]


def test_a_failing_batch_records_its_error_and_retries(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]"})
  step = ToyStep()
  owu.run_gate(path, EXISTING, [step])

  def boom(_conn):
    raise RuntimeError("disk hiccup")

  broken = ToyStep()
  monkeypatch.setattr(broken, "post_tasks", lambda: (owu.PostTask("clear_legacy", boom, 0),))
  with pytest.raises(RuntimeError):
    owu.run_post_activation_batch(path, [broken])
  (retries, error), = _query(path, "SELECT retries, last_error FROM upgrade_tasks")
  assert retries == 1 and "disk hiccup" in error
  # Legacy value intact; the real task then completes.
  while owu.run_post_activation_batch(path, [step]):
    pass
  assert _query(path, "SELECT status FROM upgrade_tasks") == [("done",)]


# --- startup wiring -----------------------------------------------------------


def test_a_step_refusal_becomes_the_boot_reason(tmp_path):
  class _Settings:
    data_dir = str(tmp_path)

  def refuse(_context):
    raise owu.StepRefusal("upgrade_needs_space", "free 1 GB", needed_bytes=1)

  context = StartupContext(
    app=None, settings=_Settings(), boot_id="b", init_db=DatabaseBootResult,
    install_pm_commit_launcher=lambda *_a: False, assert_provider_defaults=lambda *_a: None,
    logger=logging.getLogger("test"),
  )
  task = StartupTask("gate", refuse, database_failure_reason="one_way_upgrade_incomplete")
  asyncio.run(run_startup_tasks(context, (task,)))
  assert context.database_boot.failure_reason == "upgrade_needs_space"
  assert dict(context.database_boot.failure_detail)["needed_bytes"] == 1


def test_readiness_waits_for_the_gate_when_steps_are_registered(monkeypatch):
  monkeypatch.setattr(owu, "_REGISTRY", [ToyStep()])
  monkeypatch.setattr(owu, "_GATE_PASSED", False)
  assert owu.readiness_verdict() == {"reason": "one_way_upgrade_pending"}
  monkeypatch.setattr(owu, "_GATE_PASSED", True)
  assert owu.readiness_verdict() is None


def test_the_gate_runs_before_the_chat_writer():
  from app.startup import DATABASE_STARTUP_TASKS

  names = [task.name for task in DATABASE_STARTUP_TASKS]
  assert names.index("complete one-way upgrades") < names.index("start chat writer")
  gate = DATABASE_STARTUP_TASKS[names.index("complete one-way upgrades")]
  assert gate.database_failure_reason == "one_way_upgrade_incomplete"


def test_a_missing_progress_row_is_recreated_not_looped_on(tmp_path):
  path = _database(tmp_path, {"a": "[1]"})
  step = ToyStep()
  owu.run_gate(path, EXISTING, [step])
  conn = sqlite3.connect(path)
  conn.execute("DELETE FROM upgrade_tasks")
  conn.commit()
  conn.close()
  batches = 0
  while owu.run_post_activation_batch(path, [step]):
    batches += 1
    assert batches < 5
  assert _query(path, "SELECT status, done_units FROM upgrade_tasks") == [("done", 1)]


# --- the floor reported to deployment controllers -----------------------------


def _report_from(monkeypatch, path, steps):
  """Point reported_floor at this database with these steps registered."""
  import app.database

  monkeypatch.setattr(owu, "_REGISTRY", list(steps))
  monkeypatch.setattr(app.database, "engine", create_engine(f"sqlite:///{path}"))


def test_a_real_refusal_reports_the_floor_it_left(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]"})
  step = ToyStep()
  _report_from(monkeypatch, path, [step])
  assert owu.reported_floor() is None  # the gate has not run: unknown
  monkeypatch.setattr(owu, "_free_bytes", lambda _path: 10)
  with pytest.raises(owu.StepRefusal):
    owu.run_gate(path, EXISTING, [step])
  assert owu.readiness_verdict() == {"reason": "one_way_upgrade_pending"}
  assert owu.reported_floor() == 0
  # Never cached after a refusal: a later raise by another process is seen.
  conn = sqlite3.connect(path)
  conn.execute("UPDATE platform_compat SET floor = 1 WHERE id = 1")
  conn.commit()
  conn.close()
  assert owu.reported_floor() == 1


def test_a_failure_inside_activation_reports_the_old_floor(tmp_path, monkeypatch):
  path = _database(tmp_path, {"a": "[1]"})
  step = ToyStep()

  def broken_edit(conn):
    conn.execute("ALTER TABLE toy_units RENAME COLUMN legacy TO legacy_v1")
    raise RuntimeError("simulated failure before COMMIT")

  step.activation_schema_edits = broken_edit
  _report_from(monkeypatch, path, [step])
  with pytest.raises(RuntimeError):
    owu.run_gate(path, EXISTING, [step])
  assert "legacy" in _columns(sqlite3.connect(path), "toy_units")
  assert _floor(path) == 0
  assert owu.reported_floor() == 0


def test_a_later_step_refusing_reports_the_earlier_steps_floor(tmp_path, monkeypatch):
  class RefusingStep(ToyStep):
    level = 2
    name = "toy2"

    def legacy_present(self, conn):
      return False

  monkeypatch.setattr(compat, "COMPAT_LEVEL", 2)
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", 2)
  path = _database(tmp_path, {"a": "[1]"})
  steps = [ToyStep(), RefusingStep()]
  _report_from(monkeypatch, path, steps)
  with pytest.raises(owu.StepRefusal) as refused:
    owu.run_gate(path, EXISTING, steps)
  assert refused.value.database_failure_reason == "upgrade_schema_inconsistent"
  assert _state(path) == "active"
  assert owu.reported_floor() == 1
