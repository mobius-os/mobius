"""The deployed ledger is data, not a projection of today's registry."""
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app import schema_migrations as migrations

SCRIPT = Path(__file__).parents[1] / 'scripts/normalize-migration-ledger-20260908.py'
spec = importlib.util.spec_from_file_location('ledger_cutover', SCRIPT)
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)
FIXTURE = Path(__file__).parent / 'fixtures/deployed_migration_ledger_20260908.json'


def deployed_rows(before_incident=False):
  rows = json.loads(FIXTURE.read_text())['rows']
  return [(row['version'], row['applied_at']) for row in rows
          if not before_incident or row['applied_at'] < '2026-09-08']


def seed_ledger(path, rows):
  with sqlite3.connect(path) as conn:
    conn.execute('CREATE TABLE apps(id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE schema_migrations('
                 'version TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL)')
    conn.executemany('INSERT INTO schema_migrations VALUES (?,?)', rows)


@pytest.mark.parametrize('before_incident', [True, False])
@pytest.mark.parametrize('append_future', [False, True])
def test_exact_deployed_ledger_normalizes_without_replaying_any_completed_body(
  tmp_path, monkeypatch, before_incident, append_future,
):
  path = tmp_path / 'ledger.db'
  original = deployed_rows(before_incident)
  seed_ledger(path, original)
  historical = set(json.loads(FIXTURE.read_text())['canonical_completed_before_incident'])
  completed = historical | set(dict(original))
  with sqlite3.connect(path) as conn:
    expected = cutover.pending_completions(conn)
    assert len(expected) == (20 if before_incident else 8)
    assert cutover.normalize_ledger(conn) == expected
    assert cutover.normalize_ledger(conn) == []
    normalized = dict(conn.execute('SELECT version,applied_at FROM schema_migrations'))
  assert normalized == dict(original + expected)
  assert set(normalized) == completed

  # Historical completion is fixed evidence. The pending suffix is allowed
  # to grow, and follows registry order rather than numeric or lexical order.
  versions = [version for version, _ in migrations._SCHEMA_MIGRATIONS]
  assert historical <= set(versions)
  if append_future:
    versions += ['future_z', 'future_a']
  pending = [version for version in versions if version not in completed]
  calls = []

  def body(version):
    def invoke(eng):
      assert version not in completed, f'Replayed completed migration {version}'
      calls.append(version)
    return invoke

  monkeypatch.setattr(migrations, '_SCHEMA_MIGRATIONS', tuple(
    (version, body(version)) for version in versions
  ))
  eng = create_engine(f'sqlite:///{path}')
  migrations.run_migrations(eng)
  assert calls == pending
  calls.clear()
  migrations.run_migrations(eng)
  assert calls == []


def test_normalization_rolls_back_all_completion_rows_on_failure(tmp_path):
  path = tmp_path / 'ledger.db'
  original = deployed_rows(True)
  seed_ledger(path, original)
  with sqlite3.connect(path) as conn:
    conn.execute("CREATE TRIGGER refuse_completion BEFORE INSERT ON schema_migrations "
                 "WHEN NEW.version = '0017_retire_restart_resume_toggle' "
                 "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
      cutover.normalize_ledger(conn)
    assert dict(conn.execute('SELECT version,applied_at FROM schema_migrations')) == dict(original)
    conn.execute('DROP TRIGGER refuse_completion')
    conn.commit()
    assert len(cutover.normalize_ledger(conn)) == 20


def test_normalization_cli_defaults_to_read_only(tmp_path):
  path = tmp_path / 'ledger.db'
  original = deployed_rows(True)
  seed_ledger(path, original)
  result = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                          check=True, capture_output=True, text=True)
  assert json.loads(result.stdout)['applied'] is False
  with sqlite3.connect(path) as conn:
    assert dict(conn.execute('SELECT version,applied_at FROM schema_migrations')) == dict(original)


def retention_database(tmp_path):
  eng = create_engine(f'sqlite:///{tmp_path / "retention.db"}')
  with eng.begin() as conn:
    conn.exec_driver_sql('CREATE TABLE chats(id TEXT PRIMARY KEY)')
    conn.exec_driver_sql('CREATE TABLE chat_runs(id TEXT PRIMARY KEY)')
    conn.exec_driver_sql('CREATE TABLE unrelated_parent(id TEXT PRIMARY KEY)')
    conn.exec_driver_sql('CREATE TABLE unrelated_child(id INTEGER PRIMARY KEY, '
                         'parent_id TEXT REFERENCES unrelated_parent(id))')
    conn.exec_driver_sql('CREATE TABLE agent_lifecycle_events(id TEXT PRIMARY KEY, '
                         'chat_id TEXT REFERENCES chats(id), '
                         'chat_run_id TEXT REFERENCES chat_runs(id))')
    conn.exec_driver_sql("INSERT INTO unrelated_child VALUES (1,'legacy-missing')")
    conn.exec_driver_sql("INSERT INTO agent_lifecycle_events VALUES ('orphan','missing',NULL)")
  return eng


def test_retention_repairs_owned_links_without_requiring_unrelated_debt_cleanup(tmp_path):
  eng = retention_database(tmp_path)
  migrations._repair_chat_retention_orphans(eng)
  migrations._repair_chat_retention_orphans(eng)
  with eng.connect() as conn:
    assert conn.exec_driver_sql('SELECT count(*) FROM agent_lifecycle_events').scalar_one() == 0
    assert [tuple(row) for row in conn.exec_driver_sql('PRAGMA foreign_key_check')] == [
      ('unrelated_child', 1, 'unrelated_parent', 0),
    ]


def test_retention_rejects_new_orphan_even_when_total_violation_count_is_unchanged(tmp_path):
  eng = retention_database(tmp_path)
  with eng.begin() as conn:
    conn.exec_driver_sql('CREATE TRIGGER introduce_debt AFTER DELETE ON agent_lifecycle_events '
                         "BEGIN INSERT INTO unrelated_child VALUES (2,'new-missing'); END")
  with pytest.raises(RuntimeError, match='introduced foreign-key violations'):
    migrations._repair_chat_retention_orphans(eng)
  with eng.connect() as conn:
    assert conn.exec_driver_sql('SELECT count(*) FROM agent_lifecycle_events').scalar_one() == 1
    assert conn.exec_driver_sql('SELECT count(*) FROM unrelated_child').scalar_one() == 1


def test_retention_rejects_unrepaired_owned_debt_even_without_new_violations(tmp_path):
  eng = retention_database(tmp_path)
  with eng.begin() as conn:
    conn.exec_driver_sql('CREATE TRIGGER prevent_cleanup BEFORE DELETE ON agent_lifecycle_events '
                         'BEGIN SELECT RAISE(IGNORE); END')
  with pytest.raises(RuntimeError, match='agent_lifecycle_events->chats'):
    migrations._repair_chat_retention_orphans(eng)


def test_failed_retention_is_not_recorded_and_can_retry_after_cause_is_removed(tmp_path, monkeypatch):
  eng = retention_database(tmp_path)
  version = '0031_chat_retention_orphan_repair'
  monkeypatch.setattr(migrations, '_SCHEMA_MIGRATIONS', (
    (version, migrations._repair_chat_retention_orphans),
  ))
  with eng.begin() as conn:
    conn.exec_driver_sql('CREATE TABLE apps(id INTEGER PRIMARY KEY)')
    conn.exec_driver_sql('CREATE TRIGGER prevent_cleanup BEFORE DELETE ON agent_lifecycle_events '
                         'BEGIN SELECT RAISE(IGNORE); END')
  with pytest.raises(RuntimeError, match='owned or introduced'):
    migrations.run_migrations(eng)
  assert migrations.schema_migration_history(eng) == []
  with eng.begin() as conn:
    conn.exec_driver_sql('DROP TRIGGER prevent_cleanup')
  migrations.run_migrations(eng)
  first = migrations.schema_migration_history(eng)
  assert [row['version'] for row in first] == [version]
  migrations.run_migrations(eng)
  assert migrations.schema_migration_history(eng) == first
