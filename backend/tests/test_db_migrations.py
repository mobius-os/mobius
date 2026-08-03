import ast
import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import String, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
import app.database as database
from app.database import (
  _agent_lifecycle_width_migrations,
  run_migrations,
  schema_migration_history,
)


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


def test_run_migrations_removes_retired_job_authority_receipts(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'job-authority.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    memory = models.App(
      name="Memory",
      description="",
      jsx_source="export default () => null",
      capability_contract={
        "schema": 3,
        "data": {"shared_memory": "write"},
        "background": {
          "job": "fetch.sh",
          "mode": "scheduled",
          "agent": True,
          "authority": "scoped",
        },
      },
    )
    session.add(memory)
    session.commit()
    app_id = memory.id

  run_migrations(eng)
  run_migrations(eng)

  with Session(eng) as session:
    contract = session.get(models.App, app_id).capability_contract
  assert contract == {
    "schema": 4,
    "data": {"shared_memory": "write"},
    "background": {
      "job": "fetch.sh",
      "mode": "scheduled",
    },
  }


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
    conn.execute(text(
      "INSERT INTO apps (id, name) VALUES (1, 'Legacy icon app')"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  inspector = inspect(eng)
  cols = {c["name"] for c in inspector.get_columns("apps")}
  indexes = {i["name"] for i in inspector.get_indexes("apps")}

  assert "manifest_url" in cols
  assert "share_manifest_url" in cols
  assert "ix_apps_manifest_url" in indexes
  # Reversible-uninstall tombstone column is added on an existing apps table
  # (feature 110) — the path that runs on a real prod boot, not create_all.
  assert "deleted_at" in cols
  assert "system_prompt_file" in cols
  assert "icon_override_png" in cols
  assert "icon_ownership_split" in cols
  with eng.connect() as conn:
    # The historical migration remains immutable even though runtime
    # convergence no longer reads this retired marker.
    split = conn.execute(text(
      "SELECT icon_ownership_split FROM apps WHERE id = 1"
    )).scalar_one()
  assert split in (False, 0)


def test_run_migrations_adds_managed_sign_in_identity_to_existing_owner(tmp_path):
  db_path = tmp_path / "legacy-owner.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    # Production migrations are gated on the pre-existing apps table.
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE owner ("
      "id INTEGER PRIMARY KEY, "
      "username VARCHAR(64) NOT NULL, "
      "password_hash VARCHAR(255) NOT NULL"
      ")"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  cols = {c["name"] for c in inspect(eng).get_columns("owner")}
  assert "sso_subject" in cols
  assert "sso_email" in cols


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
  assert {
    "provider_session_id",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "model_context_window",
    "usage_json",
  } <= cols


def test_run_migrations_moves_legacy_running_marker_into_chat_runs(tmp_path):
  """The authority cutover preserves an interrupted live turn before dropping
  the two legacy Chat columns, and is safe to resume after a partial boot."""
  eng = create_engine(f"sqlite:///{tmp_path / 'legacy-run-marker.db'}")
  models.Base.metadata.create_all(eng)
  started = datetime(2026, 7, 30, 23, 45, 12)
  with Session(eng) as session:
    session.add(models.Chat(
      id="legacy-running",
      title="Interrupted turn",
      provider="codex",
      messages=[{"role": "user", "content": "keep this turn"}],
    ))
    session.commit()
  with eng.connect() as conn:
    conn.execute(text("ALTER TABLE chats ADD COLUMN run_status VARCHAR(16)"))
    conn.execute(text("ALTER TABLE chats ADD COLUMN run_started_at DATETIME"))
    conn.execute(text(
      "UPDATE chats SET run_status='running', "
      "run_started_at=:started WHERE id='legacy-running'"
    ), {"started": started})
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  chat_columns = {c["name"] for c in inspect(eng).get_columns("chats")}
  assert "run_status" not in chat_columns
  assert "run_started_at" not in chat_columns
  with Session(eng) as session:
    runs = session.query(models.ChatRun).filter_by(
      chat_id="legacy-running",
    ).all()
  assert len(runs) == 1
  assert runs[0].status == "running"
  assert runs[0].provider == "codex"
  assert runs[0].started_at == started


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
  assert "auto_resume_on_restart" in cols
  assert cols["auto_resume_on_restart"]["nullable"] is False
  assert cols["auto_resume_on_restart"]["default"] is not None
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
    restart_values = conn.execute(text(
      "SELECT id, auto_resume_on_restart FROM chats ORDER BY id"
    )).all()
  assert value in (False, 0)
  assert future_value in (False, 0)
  assert all(restart in (True, 1) for _, restart in restart_values)


def test_run_migrations_preserves_existing_continuation_choices(tmp_path):
  """New defaults must not rewrite choices already stored on local installs."""
  db_path = tmp_path / "existing-continuation-policies.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE chats ("
      "id VARCHAR(64) PRIMARY KEY, title VARCHAR(255), updated_at DATETIME, "
      "auto_resume_on_limit BOOLEAN NOT NULL DEFAULT TRUE, "
      "auto_resume_on_restart BOOLEAN NOT NULL DEFAULT FALSE"
      ")"
    ))
    conn.execute(text(
      "INSERT INTO chats (id, title, auto_resume_on_limit, "
      "auto_resume_on_restart) VALUES ('chosen', 'Chosen', TRUE, FALSE)"
    ))
    conn.commit()

  run_migrations(eng)

  with eng.connect() as conn:
    values = conn.execute(text(
      "SELECT auto_resume_on_limit, auto_resume_on_restart "
      "FROM chats WHERE id = 'chosen'"
    )).one()
  assert values == (1, 0)


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
  assert str(column.server_default.arg).lower() == "false"
  restart = models.Chat.__table__.c.auto_resume_on_restart
  assert restart.nullable is False
  assert restart.default is not None
  assert restart.server_default is not None
  assert str(restart.server_default.arg).lower() == "true"


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
  assert "auto_resume_on_restart_default" in cols
  assert cols["auto_resume_on_restart_default"]["nullable"] is False
  with eng.connect() as conn:
    value = conn.execute(text(
      "SELECT auto_resume_on_limit_default FROM owner WHERE id = 1"
    )).scalar_one()
    restart_value = conn.execute(text(
      "SELECT auto_resume_on_restart_default FROM owner WHERE id = 1"
    )).scalar_one()
  assert value in (False, 0)
  assert restart_value in (True, 1)


def test_fresh_owner_schema_has_auto_resume_default():
  column = models.Owner.__table__.c.auto_resume_on_limit_default

  assert column.nullable is False
  assert column.default is not None
  assert column.server_default is not None
  assert str(column.server_default.arg).lower() == "false"
  restart = models.Owner.__table__.c.auto_resume_on_restart_default
  assert restart.nullable is False
  assert restart.default is not None
  assert restart.server_default is not None
  assert str(restart.server_default.arg).lower() == "true"


def test_run_migrations_adds_read_at_and_backfills_notifications(tmp_path):
  """Pre-feature notification history must not arrive as a full unread badge.

  Old-schema notifications table (no read_at) → run_migrations adds the
  column and stamps existing rows read_at = sent_at, in one transaction.
  Idempotent across reruns.
  """
  db_path = tmp_path / "legacy-notifications.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    # Production migrations are gated on the pre-existing apps table.
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE notifications ("
      "id VARCHAR(64) PRIMARY KEY, "
      "owner_id INTEGER NOT NULL, "
      "source_type VARCHAR(16) NOT NULL, "
      "title VARCHAR(256) NOT NULL, "
      "sent_at DATETIME, "
      "clicked_at DATETIME"
      ")"
    ))
    conn.execute(text(
      "INSERT INTO notifications (id, owner_id, source_type, title, sent_at) "
      "VALUES ('n-legacy', 1, 'agent', 'Old', '2026-01-02 03:04:05')"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  inspector = inspect(eng)
  cols = {c["name"] for c in inspector.get_columns("notifications")}
  assert "read_at" in cols
  with eng.connect() as conn:
    read_at = conn.execute(text(
      "SELECT read_at FROM notifications WHERE id = 'n-legacy'"
    )).scalar_one()
  assert str(read_at) == "2026-01-02 03:04:05"
def test_run_migrations_records_an_inspectable_append_only_history(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'migration-ledger.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "token_nonce VARCHAR(32), capability_contract JSON"
      ")"
    ))

  run_migrations(eng)
  first = schema_migration_history(eng)
  run_migrations(eng)
  second = schema_migration_history(eng)

  assert [row["version"] for row in first] == [
    "0001_legacy_schema_convergence",
    "0002_chat_run_goal_objective",
    "0003_chat_run_root_identity",
  ]
  assert second == first


def test_chat_run_root_migration_backfills_existing_physical_runs(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'run-root.db'}")
  applied_at = datetime(2026, 8, 1)
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128))"
    ))
    conn.execute(text(
      "CREATE TABLE chat_runs ("
      "id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(64) NOT NULL, "
      "status VARCHAR(16) NOT NULL)"
    ))
    conn.execute(text(
      "INSERT INTO chat_runs (id, chat_id, status) "
      "VALUES ('physical-old', 'chat-old', 'completed')"
    ))
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    conn.execute(text(
      "INSERT INTO schema_migrations (version, applied_at) VALUES "
      "('0001_legacy_schema_convergence', :at), "
      "('0002_chat_run_goal_objective', :at)"
    ), {"at": applied_at})

  run_migrations(eng)

  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT root_run_id FROM chat_runs WHERE id = 'physical-old'"
    )).scalar_one() == "physical-old"


def test_goal_migration_backfills_only_the_running_turns_initiating_goal(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'goal-run.db'}")
  models.Base.metadata.create_all(eng)
  started_at = datetime(2026, 7, 31, 12, 0, 0)
  started_ms = int(started_at.replace(tzinfo=database.UTC).timestamp() * 1000)
  with Session(eng) as session:
    session.add(models.Chat(
      id="goal-chat",
      title="Goal",
      messages=[
        {
          "role": "user",
          "content": "/goal finish the migration",
          "ts": started_ms - 5,
        },
        {"role": "assistant", "content": "Working", "ts": started_ms + 5},
        {
          "role": "user",
          "content": "A steered question",
          "ts": started_ms + 10,
        },
      ],
      pending_messages=[],
    ))
    session.add(models.ChatRun(
      id="goal-run",
      chat_id="goal-chat",
      status="running",
      provider="codex",
      started_at=started_at,
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE chat_runs DROP COLUMN goal_objective"))

  run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    objective = conn.execute(text(
      "SELECT goal_objective FROM chat_runs WHERE id = 'goal-run'"
    )).scalar_one()
  assert objective == "finish the migration"


def test_applied_legacy_schema_migration_is_immutable():
  """Editing migration 0001 must require an intentional new migration."""
  source = Path(database.__file__).read_text(encoding="utf-8")
  module = ast.parse(source)
  migration = next(
    node for node in module.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_converge_legacy_schema"
  )
  semantic_shape = ast.dump(migration, include_attributes=False).encode()
  assert hashlib.sha256(semantic_shape).hexdigest() == (
    "4f7b1f167534e0f692eaa004e40c124b36b655c387671f93b66f4932a6e242ec"
  ), "0001 is applied history; append a new numbered migration instead"


def test_failed_migration_is_not_recorded_and_can_retry(tmp_path, monkeypatch):
  eng = create_engine(f"sqlite:///{tmp_path / 'migration-retry.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
  attempts = 0

  def fail_once(_eng):
    nonlocal attempts
    attempts += 1
    raise RuntimeError("interrupted migration")

  monkeypatch.setattr(
    database,
    "_SCHEMA_MIGRATIONS",
    (("9000_retry_contract", fail_once),),
  )

  with pytest.raises(RuntimeError, match="interrupted migration"):
    run_migrations(eng)
  assert schema_migration_history(eng) == []
  with pytest.raises(RuntimeError, match="interrupted migration"):
    run_migrations(eng)
  assert attempts == 2
