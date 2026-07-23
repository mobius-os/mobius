import pytest
from sqlalchemy import String, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import models
from app.database import _agent_lifecycle_width_migrations, run_migrations


def test_run_migrations_drops_removed_image_generation_columns(tmp_path):
  db_path = tmp_path / "legacy-image-generation.db"
  eng = create_engine(f"sqlite:///{db_path}")
  models.Base.metadata.create_all(eng)
  with eng.connect() as conn:
    conn.execute(text(
      "ALTER TABLE owner ADD COLUMN gemini_api_key_enc TEXT"
    ))
    conn.execute(text(
      "ALTER TABLE chats ADD COLUMN generated_images JSON "
      "NOT NULL DEFAULT '[]'"
    ))
    conn.commit()

  run_migrations(eng)

  inspector = inspect(eng)
  owner_columns = {column["name"] for column in inspector.get_columns("owner")}
  chat_columns = {column["name"] for column in inspector.get_columns("chats")}
  assert "gemini_api_key_enc" not in owner_columns
  assert "generated_images" not in chat_columns


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
  assert "system_prompt_file" in cols


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
  assert "restart_nonce" in cols


def test_agent_lifecycle_width_migration_is_postgres_only_and_idempotent():
  legacy = [
    {"name": "activation_id", "type": String(70)},
    {"name": "parent_activation_id", "type": String(70)},
  ]
  expected = [
    "ALTER TABLE agent_lifecycle_events "
    "ALTER COLUMN activation_id TYPE VARCHAR(75)",
    "ALTER TABLE agent_lifecycle_events "
    "ALTER COLUMN parent_activation_id TYPE VARCHAR(75)",
  ]

  assert _agent_lifecycle_width_migrations("postgresql", legacy) == expected
  assert _agent_lifecycle_width_migrations("sqlite", legacy) == []
  assert _agent_lifecycle_width_migrations("postgresql", [
    {"name": "activation_id", "type": String(75)},
    {"name": "parent_activation_id", "type": String(75)},
  ]) == []


def test_run_migrations_removes_only_persisted_codex_prompt_summaries(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'lifecycle-privacy.db'}")
  models.Base.metadata.create_all(eng)
  common = (
    "INSERT INTO agent_lifecycle_events ("
    "event_key, chat_id, provider, provider_agent_id, agent_id, activation_id, "
    "parent_kind, event_type, state, observed_at, time_quality, source, "
    "source_event_id, summary) VALUES ("
    ":event_key, 'chat', :provider, :provider_agent_id, :agent_id, "
    ":activation_id, 'unknown', :event_type, :state, CURRENT_TIMESTAMP, "
    "'observed', 'runner', :source_event_id, :summary)"
  )
  rows = [
    ("spawn", "codex", "agent_spawned", "running", "thread-started:child",
     "private thread preview"),
    ("resume", "codex", "agent_started", "running", "call:child:started",
     "private delegated prompt"),
    ("native", "codex", "agent_started", "running", "native-item-id",
     "/root/scout"),
    ("terminal", "codex", "agent_terminal", "done", "call:child:completed",
     "provider result summary"),
    ("claude", "claude", "agent_started", "running", "message-uuid",
     "task description"),
  ]
  with eng.connect() as conn:
    conn.execute(text(
      "INSERT INTO chats (id, title, title_locked, messages, pending_messages, "
      "uploads, provider) VALUES ('chat', 'Chat', 0, '[]', '[]', '[]', 'claude')"
    ))
    for index, (key, provider, event_type, state, source_id, summary) in enumerate(
      rows,
    ):
      conn.execute(text(common), {
        "event_key": key,
        "provider": provider,
        "provider_agent_id": f"provider-{index}",
        "agent_id": f"agent-{index}",
        "activation_id": f"activation-{index}",
        "event_type": event_type,
        "state": state,
        "source_event_id": source_id,
        "summary": summary,
      })
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    summaries = dict(conn.execute(text(
      "SELECT event_key, summary FROM agent_lifecycle_events ORDER BY event_key"
    )).all())
  assert summaries == {
    "claude": "task description",
    "native": None,
    "resume": None,
    "spawn": None,
    "terminal": "provider result summary",
  }


def test_run_migrations_adds_chat_auto_resume_policy(tmp_path):
  db_path = tmp_path / "legacy-chats.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE chats ("
      "id VARCHAR(64) PRIMARY KEY, title VARCHAR(255), updated_at DATETIME"
      ")"
    ))
    conn.execute(text(
      "INSERT INTO chats (id, title) VALUES ('legacy', 'Legacy')"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  cols = {
    c["name"]: c for c in inspect(eng).get_columns("chats")
  }
  assert "auto_resume_on_limit" in cols
  assert cols["auto_resume_on_limit"]["nullable"] is False
  assert cols["auto_resume_on_limit"]["default"] is not None
  assert "auto_resume_on_restart" not in cols
  assert "system_prompt_snapshot_id" in cols
  with eng.connect() as conn:
    value = conn.execute(text(
      "SELECT auto_resume_on_limit FROM chats WHERE id = 'legacy'"
    )).scalar_one()
    conn.execute(text(
      "INSERT INTO chats (id, title) VALUES ('new-after-upgrade', 'New')"
    ))
    future_value = conn.execute(text(
      "SELECT auto_resume_on_limit FROM chats "
      "WHERE id = 'new-after-upgrade'"
    )).scalar_one()
  assert value in (True, 1)
  assert future_value in (True, 1)


def test_run_migrations_adds_bounded_live_assistant_snapshot(tmp_path):
  db_path = tmp_path / "legacy-live-assistant.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE chats (id VARCHAR(64) PRIMARY KEY, title VARCHAR(255), "
      "updated_at DATETIME)"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  cols = {c["name"] for c in inspect(eng).get_columns("chats")}
  assert "live_assistant" in cols


def test_fresh_chat_schema_has_database_auto_resume_default():
  """Fresh create_all DDL must match the upgraded-table contract."""
  column = models.Chat.__table__.c.auto_resume_on_limit

  assert column.nullable is False
  assert column.default is not None
  assert column.server_default is not None
  assert str(column.server_default.arg).lower() == "true"


def test_run_migrations_adds_owner_auto_resume_default(tmp_path):
  db_path = tmp_path / "legacy-owner.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE owner (id INTEGER PRIMARY KEY, username VARCHAR(64), "
      "hashed_password VARCHAR(255))"
    ))
    conn.execute(text(
      "INSERT INTO owner (id, username, hashed_password) "
      "VALUES (1, 'owner', 'hash')"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  cols = {c["name"]: c for c in inspect(eng).get_columns("owner")}
  assert "auto_resume_on_limit_default" in cols
  assert cols["auto_resume_on_limit_default"]["nullable"] is False
  with eng.connect() as conn:
    value = conn.execute(text(
      "SELECT auto_resume_on_limit_default FROM owner WHERE id = 1"
    )).scalar_one()
  assert value in (True, 1)


def test_fresh_owner_schema_has_auto_resume_default():
  column = models.Owner.__table__.c.auto_resume_on_limit_default

  assert column.nullable is False
  assert column.default is not None
  assert column.server_default is not None
  assert str(column.server_default.arg).lower() == "true"
