"""Build-time contracts for self-contained, append-only migration history."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _migration_guard():
  script = Path(__file__).parents[1] / "scripts" / "check-schema-migrations.py"
  spec = importlib.util.spec_from_file_location("migration_guard", script)
  assert spec and spec.loader
  guard = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = guard
  spec.loader.exec_module(guard)
  return guard


def test_checkout_schema_migration_history_matches_committed_ledger():
  """No-target checks catch one-sided checkout drift, not ledger co-edits."""
  script = Path(__file__).parents[1] / "scripts" / "check-schema-migrations.py"
  completed = subprocess.run(
    [sys.executable, str(script)],
    text=True,
    capture_output=True,
    check=False,
  )
  assert completed.returncode == 0, completed.stderr
  assert "migration hashes match migration_history.json" in (
    completed.stdout
  )


def test_published_history_cannot_be_rehashed_in_place():
  """Changing a published function remains a rewrite without a manifest."""
  guard = _migration_guard()
  published = guard.inspect_history(
    "def initial(db):\n  return db\n"
    "_SCHEMA_MIGRATIONS = ((\"0001_initial\", initial),)\n",
    source="published.py",
  )
  rewritten = guard.inspect_history(
    "def initial(db):\n  return str(db)\n"
    "_SCHEMA_MIGRATIONS = ((\"0001_initial\", initial),)\n",
    source="rewritten.py",
  )
  appended = guard.inspect_history(
    "def initial(db):\n  return db\n"
    "def next_step(db):\n  return db\n"
    "_SCHEMA_MIGRATIONS = ("
    "(\"0001_initial\", initial), (\"0002_next\", next_step))\n",
    source="appended.py",
  )

  assert guard.append_only_error(published, rewritten) == (
    "published migration 0001_initial changed"
  )
  assert guard.append_only_error(published, appended) is None


def test_published_history_cannot_be_removed_or_reordered():
  guard = _migration_guard()

  published = {"0001_initial": "one", "0002_next": "two"}
  removed = guard.append_only_error(published, {"0001_initial": "one"})
  reordered = guard.append_only_error(published, {
    "0002_next": "two",
    "0001_initial": "one",
  })
  assert removed == "published migration 0002_next was removed"
  assert reordered == (
    "published migration 0001_initial was reordered, removed, or renamed "
    "to 0002_next"
  )


def test_committed_history_rejects_one_sided_registry_or_code_drift():
  guard = _migration_guard()
  candidate = {
    "0001_initial": ("initial", "a" * 64),
    "0002_next": ("next_step", "b" * 64),
  }

  assert guard.committed_history_error({
    "0001_initial": "a" * 64,
  }, candidate) == (
    "registry and migration_history.json differ; append the new version and "
    "its hash without editing or renumbering prior entries"
  )
  assert guard.committed_history_error({
    "0002_next": "b" * 64,
    "0001_initial": "a" * 64,
  }, candidate) == (
    "registry and migration_history.json differ; append the new version and "
    "its hash without editing or renumbering prior entries"
  )
  assert guard.committed_history_error({
    "0001_initial": "c" * 64,
    "0002_next": "b" * 64,
  }, candidate) == (
    "migration code differs from migration_history.json for 0001_initial; "
    "restore the matching entry and append a new migration"
  )


def test_new_migration_cannot_import_mutable_runtime_helpers():
  """Published migrations own their behavior instead of freezing app code."""
  guard = _migration_guard()
  source = (
    "def migrate(db):\n"
    "  from app.helper import normalize\n"
    "  return normalize(db)\n"
    "_SCHEMA_MIGRATIONS = ((\"0016_new\", migrate),)\n"
  )

  with pytest.raises(SystemExit):
    guard.inspect_history(source, source="candidate.py")


def test_migration_hash_includes_migration_owned_helpers():
  guard = _migration_guard()
  first = guard.inspect_history(
    "def normalize(value):\n"
    "  return value\n"
    "def migrate(db):\n"
    "  return normalize(db)\n"
    "_SCHEMA_MIGRATIONS = ((\"0016_new\", migrate),)\n",
    source="first.py",
  )
  second = guard.inspect_history(
    "def normalize(value):\n"
    "  return str(value)\n"
    "def migrate(db):\n"
    "  return normalize(db)\n"
    "_SCHEMA_MIGRATIONS = ((\"0016_new\", migrate),)\n",
    source="second.py",
  )

  assert first["0016_new"] != second["0016_new"]
