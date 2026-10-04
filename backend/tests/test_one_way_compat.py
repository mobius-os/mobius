"""Compatibility levels, the import-time image check, and the database floor.

ONE_WAY_UPGRADES_DESIGN.md §1: a source must refuse an image whose baked
fallback is older than it needs, and a release below the database floor must
refuse to serve without changing anything.
"""

import json
import re
from pathlib import Path

import pytest

from app import compat, main as main_module, one_way_upgrades
from app.database import engine


# --- declared levels -------------------------------------------------------


def test_declared_level_reads_literal_assignments_only():
  assert compat.declared_level("COMPAT_LEVEL = 3\n") == 3
  assert compat.declared_level("COMPAT_LEVEL: int = 2\n") == 2
  assert compat.declared_level("OTHER = 1\n") is None
  with pytest.raises(ValueError):
    compat.declared_level("COMPAT_LEVEL = 1 + 1\n")
  with pytest.raises(ValueError):
    compat.declared_level("COMPAT_LEVEL = True\n")
  with pytest.raises(ValueError):
    compat.declared_level("COMPAT_LEVEL = -1\n")
  # Python keeps the last assignment; a duplicate is ambiguous, not "the first".
  with pytest.raises(ValueError):
    compat.declared_level("COMPAT_LEVEL = 1\nCOMPAT_LEVEL = 3\n")


def test_shipped_declarations_are_consistent():
  compat.check_declarations()
  source = Path(compat.__file__).read_text(encoding="utf-8")
  assert compat.declared_level(source) == compat.COMPAT_LEVEL
  assert (
    compat.declared_level(source, "REQUIRED_IMAGE_LEVEL")
    == compat.REQUIRED_IMAGE_LEVEL
  )


def test_trigger_file_matches_the_source_level():
  """Every step release advances the image-owned trigger with compat.py."""
  trigger = Path(compat.__file__).resolve().parents[1] / "runtime" / "one_way_capability.json"
  claimed = json.loads(trigger.read_text(encoding="utf-8"))["level"]
  assert type(claimed) is int
  assert claimed == compat.COMPAT_LEVEL


def test_baked_level_is_zero_for_a_missing_or_malformed_file(tmp_path):
  assert compat.baked_image_level(tmp_path / "missing.py") == 0
  broken = tmp_path / "compat.py"
  broken.write_text("COMPAT_LEVEL = (\n", encoding="utf-8")
  assert compat.baked_image_level(broken) == 0
  broken.write_text("COMPAT_LEVEL = 4\n", encoding="utf-8")
  assert compat.baked_image_level(broken) == 4


# --- which image level applies ---------------------------------------------


def _baked_tree(tmp_path, level):
  root = tmp_path / "platform-baked"
  target = root / "backend" / "app" / "compat.py"
  target.parent.mkdir(parents=True)
  if level is not None:
    target.write_text(f"COMPAT_LEVEL = {level}\n", encoding="utf-8")
  return root


def _outside_test_runtime(monkeypatch):
  monkeypatch.delenv("MOBIUS_TEST_RUNTIME", raising=False)
  monkeypatch.delenv("MOBIUS_TEST_DATABASE_ISOLATED", raising=False)
  for name in (
    compat.CANDIDATE_MARKER_ENV, compat.CANDIDATE_LEVEL_ENV,
    compat.TEST_LEVEL_ENV, compat.DEV_LEVEL_ENV,
  ):
    monkeypatch.delenv(name, raising=False)


def test_baked_level_applies_in_the_image(tmp_path, monkeypatch):
  _outside_test_runtime(monkeypatch)
  root = _baked_tree(tmp_path, 2)
  assert compat.image_level(allow_candidate=True, baked_root=root) == (2, "baked")
  # A baked tree that predates compat.py is level 0, not "no image".
  old = _baked_tree(tmp_path / "old", None)
  assert compat.image_level(allow_candidate=True, baked_root=old) == (0, "baked")


def test_dev_level_applies_only_without_a_baked_checkout(tmp_path, monkeypatch):
  _outside_test_runtime(monkeypatch)
  monkeypatch.setenv(compat.DEV_LEVEL_ENV, "5")
  assert compat.image_level(
    allow_candidate=False, baked_root=tmp_path / "absent",
  ) == (5, "dev")
  root = _baked_tree(tmp_path, 1)
  assert compat.image_level(allow_candidate=False, baked_root=root) == (1, "baked")


def test_candidate_level_needs_the_marker_and_the_validator_context(tmp_path, monkeypatch):
  _outside_test_runtime(monkeypatch)
  root = _baked_tree(tmp_path, 0)
  monkeypatch.setenv(compat.CANDIDATE_LEVEL_ENV, "3")
  # Without the marker the level alone is ignored.
  assert compat.image_level(allow_candidate=True, baked_root=root) == (0, "baked")
  monkeypatch.setenv(compat.CANDIDATE_MARKER_ENV, "1")
  assert compat.image_level(allow_candidate=True, baked_root=root) == (3, "candidate")
  # A served boot never honours it.
  assert compat.image_level(allow_candidate=False, baked_root=root) == (0, "baked")


def test_test_level_needs_both_isolated_test_flags(tmp_path, monkeypatch):
  _outside_test_runtime(monkeypatch)
  root = _baked_tree(tmp_path, 0)
  monkeypatch.setenv(compat.TEST_LEVEL_ENV, "7")
  monkeypatch.setenv("MOBIUS_TEST_RUNTIME", "1")
  assert compat.image_level(allow_candidate=False, baked_root=root) == (0, "baked")
  monkeypatch.setenv("MOBIUS_TEST_DATABASE_ISOLATED", "1")
  assert compat.image_level(allow_candidate=False, baked_root=root) == (7, "test")


def test_levels_from_the_environment_must_be_integers(tmp_path, monkeypatch):
  _outside_test_runtime(monkeypatch)
  monkeypatch.setenv(compat.DEV_LEVEL_ENV, "one")
  with pytest.raises(compat.ImageBelowSourceError):
    compat.image_level(allow_candidate=False, baked_root=tmp_path / "absent")


# --- the refusals -----------------------------------------------------------


def _require_level(monkeypatch, level):
  monkeypatch.setattr(compat, "COMPAT_LEVEL", level)
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", level)


def test_import_check_refuses_an_older_image(tmp_path, monkeypatch):
  _outside_test_runtime(monkeypatch)
  _require_level(monkeypatch, 1)
  root = _baked_tree(tmp_path, 0)
  monkeypatch.setattr(compat, "BAKED_ROOT", root)
  # image_level's default argument was bound at definition; pass it through.
  monkeypatch.setattr(
    compat, "image_level",
    lambda *, allow_candidate, baked_root=root, _f=compat.image_level: _f(
      allow_candidate=allow_candidate, baked_root=baked_root,
    ),
  )
  with pytest.raises(compat.ImageBelowSourceError, match="level 1"):
    compat.assert_image_supports_source()
  # The validator's candidate level lets the same source import.
  monkeypatch.setenv(compat.CANDIDATE_MARKER_ENV, "1")
  monkeypatch.setenv(compat.CANDIDATE_LEVEL_ENV, "1")
  compat.assert_image_supports_source()
  # ...but a served boot re-checks against the real image and refuses.
  verdict = compat.serve_time_image_verdict()
  assert verdict is not None
  assert verdict["image_level"] == 0
  assert verdict["required_image_level"] == 1


def test_declaration_mismatch_fails_the_import_check(monkeypatch):
  monkeypatch.setattr(compat, "COMPAT_LEVEL", 0)
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", 1)
  with pytest.raises(ValueError):
    compat.assert_image_supports_source()


@pytest.fixture
def own_database(tmp_path, monkeypatch):
  """A separate database initialised by a real first boot."""
  from sqlalchemy import create_engine

  own = create_engine(f"sqlite:///{tmp_path / 'own.db'}")
  monkeypatch.setattr(main_module, "engine", own)
  assert main_module._init_db().serviceable
  yield own
  own.dispose()


def _schema_and_ledger(eng):
  with eng.connect() as conn:
    schema = sorted(
      tuple(row) for row in conn.exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_master ORDER BY name"
      )
    )
    ledger = sorted(
      tuple(row) for row in conn.exec_driver_sql(
        "SELECT * FROM schema_migrations"
      )
    )
  return schema, ledger


def test_database_above_this_code_is_refused_without_changes(own_database):
  with own_database.begin() as conn:
    conn.exec_driver_sql(
      "INSERT OR REPLACE INTO platform_compat (id, floor, updated_at)"
      " VALUES (1, ?, CURRENT_TIMESTAMP)",
      (compat.COMPAT_LEVEL + 1,),
    )
    # Something create_all would recreate if it ran.
    conn.exec_driver_sql("DROP TABLE upgrade_tasks")
  before = _schema_and_ledger(own_database)

  result = main_module._init_db()

  assert result.failure_reason == "below_compatibility_floor"
  assert not result.serviceable
  detail = dict(result.failure_detail)
  assert detail["floor"] == compat.COMPAT_LEVEL + 1
  assert detail["compat_level"] == compat.COMPAT_LEVEL
  assert _schema_and_ledger(own_database) == before


def test_a_new_database_is_seen_as_empty_before_create_all(tmp_path, monkeypatch):
  from sqlalchemy import create_engine

  own = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
  monkeypatch.setattr(main_module, "engine", own)
  try:
    first = main_module._init_db()
    second = main_module._init_db()
  finally:
    own.dispose()
  assert first.serviceable and first.existing_tables == frozenset()
  assert {"chats", "platform_compat", "schema_migrations"} <= second.existing_tables


def test_serviceable_boot_records_the_pre_create_all_tables():
  result = main_module._init_db()
  assert result.serviceable
  assert "chats" in result.existing_tables
  with engine.connect() as conn:
    assert conn.exec_driver_sql(
      "SELECT id, floor FROM platform_compat"
    ).fetchall() == [(1, compat.COMPAT_LEVEL)]


def test_the_first_boot_creates_the_floor_table_with_its_row(own_database):
  with own_database.connect() as conn:
    assert conn.exec_driver_sql(
      "SELECT id, floor FROM platform_compat"
    ).fetchall() == [(1, 0)]


def test_image_refusal_happens_before_any_database_access(monkeypatch):
  monkeypatch.setattr(
    compat, "serve_time_image_verdict",
    lambda: {"required_image_level": 1, "image_level": 0, "message": "too old"},
  )
  touched = []
  monkeypatch.setattr(
    one_way_upgrades, "preflight", lambda _engine: touched.append("preflight"),
  )
  monkeypatch.setattr(
    main_module.Base.metadata, "create_all",
    lambda *a, **k: touched.append("create_all"),
  )
  result = main_module._init_db()
  assert result.failure_reason == "image_below_source"
  assert dict(result.failure_detail)["image_level"] == 0
  assert touched == []


def test_preflight_reads_floor_zero_without_the_table(tmp_path):
  from sqlalchemy import create_engine

  empty = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
  try:
    seen = one_way_upgrades.preflight(empty)
  finally:
    empty.dispose()
  assert seen.floor == 0
  assert seen.existing_tables == frozenset()


def test_import_verdict_checks_the_registry_before_the_database(monkeypatch):
  class _Step(one_way_upgrades.OneWayStep):
    level = 1
    name = "unreleased"

  monkeypatch.setattr(one_way_upgrades, "_REGISTRY", [_Step()])
  # A registered step without raised levels must refuse, even in later releases.
  monkeypatch.setattr(compat, "COMPAT_LEVEL", 0)
  monkeypatch.setattr(compat, "REQUIRED_IMAGE_LEVEL", 0)
  with pytest.raises(ValueError, match="REQUIRED_IMAGE_LEVEL"):
    one_way_upgrades.assert_source_supported()


def test_a_floor_table_without_its_row_is_refused_unchanged(own_database):
  """The table and its row are created together, so a lone table is damage."""
  with own_database.begin() as conn:
    conn.exec_driver_sql("DELETE FROM platform_compat")
  before = _schema_and_ledger(own_database)
  result = main_module._init_db()
  assert result.failure_reason == "compat_floor_damaged"
  assert _schema_and_ledger(own_database) == before
  with own_database.connect() as conn:
    assert conn.exec_driver_sql("SELECT COUNT(*) FROM platform_compat").scalar() == 0


def test_an_active_step_raises_the_floor_without_the_floor_table(own_database):
  """A partial restore that lost the floor table must not look safe."""
  with own_database.begin() as conn:
    conn.exec_driver_sql("DROP TABLE platform_compat")
    conn.exec_driver_sql(
      "INSERT INTO platform_upgrades (level, name, state, started_at)"
      " VALUES (?, 'future', 'active', CURRENT_TIMESTAMP)",
      (compat.COMPAT_LEVEL + 1,),
    )
  result = main_module._init_db()
  assert result.failure_reason == "below_compatibility_floor"
  assert dict(result.failure_detail)["floor"] == compat.COMPAT_LEVEL + 1


def test_a_non_integer_floor_fails_closed(own_database):
  with own_database.begin() as conn:
    conn.exec_driver_sql(
      "INSERT OR REPLACE INTO platform_compat (id, floor, updated_at)"
      " VALUES (1, 'x', CURRENT_TIMESTAMP)"
    )
  with pytest.raises(ValueError, match="platform_compat.floor"):
    main_module._init_db()


def test_the_floor_record_is_created_atomically(tmp_path, monkeypatch):
  """A crash between creating the table and inserting its row leaves neither."""
  import sqlite3

  path = str(tmp_path / "atomic.db")
  sqlite3.connect(path).close()
  seen = one_way_upgrades.Preflight(floor=0, existing_tables=frozenset())

  def crash(_conn, _floor):
    raise RuntimeError("killed between the statements")

  monkeypatch.setattr(one_way_upgrades, "_insert_compat_row", crash)
  with pytest.raises(RuntimeError):
    one_way_upgrades.ensure_compat_record(path, seen)
  conn = sqlite3.connect(path)
  try:
    assert conn.execute(
      "SELECT name FROM sqlite_master WHERE name = 'platform_compat'"
    ).fetchall() == []
  finally:
    conn.close()

  monkeypatch.undo()
  one_way_upgrades.ensure_compat_record(path, seen)
  conn = sqlite3.connect(path)
  try:
    assert conn.execute("SELECT id, floor FROM platform_compat").fetchall() == [(1, 0)]
  finally:
    conn.close()


def test_image_label_declares_the_baked_level_and_the_build_enforces_it():
  """Controllers that inspect an image without running it read this label."""
  dockerfile = (Path(compat.__file__).resolve().parents[2] / "Dockerfile").read_text()
  default = re.search(r"^ARG MOBIUS_COMPAT_LEVEL=(\d+)$", dockerfile, re.M)
  assert default and int(default[1]) == compat.COMPAT_LEVEL
  assert 'you.mobius.compat-level="${MOBIUS_COMPAT_LEVEL}"' in dockerfile
  assert 'assert labelled == str(baked)' in dockerfile
