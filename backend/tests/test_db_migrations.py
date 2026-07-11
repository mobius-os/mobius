from sqlalchemy import create_engine, inspect, text

from app.database import run_migrations


def test_run_migrations_adds_manifest_url_to_existing_apps_table(tmp_path):
  db_path = tmp_path / "legacy.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, "
      "name VARCHAR(255) NOT NULL"
      ")"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  inspector = inspect(eng)
  cols = {c["name"] for c in inspector.get_columns("apps")}
  indexes = {i["name"] for i in inspector.get_indexes("apps")}

  assert "manifest_url" in cols
  assert "ix_apps_manifest_url" in indexes
  # Reversible-uninstall tombstone column is added on an existing apps table
  # (feature 110) — the path that runs on a real prod boot, not create_all.
  assert "deleted_at" in cols


def test_run_migrations_adds_park_columns_to_existing_chat_runs(tmp_path):
  """A deployed DB has `chat_runs` WITHOUT the provider-park columns
  (design §2.4) — create_all only covers fresh installs, so the ALTER path
  must add them (idempotently) on a real boot."""
  db_path = tmp_path / "legacy-runs.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    # run_migrations returns early without an `apps` table (fresh install).
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE chat_runs ("
      "id VARCHAR(64) PRIMARY KEY, "
      "chat_id VARCHAR(64) NOT NULL, "
      "status VARCHAR(16) NOT NULL DEFAULT 'running'"
      ")"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  inspector = inspect(eng)
  cols = {c["name"] for c in inspector.get_columns("chat_runs")}
  assert "parked_until" in cols
  assert "park_reason" in cols
