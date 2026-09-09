import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import String, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
import app.schema_migrations as migrations
from app.config import get_settings
from app.schema_migrations import (
  _agent_lifecycle_width_migrations,
  run_migrations,
  schema_migration_history,
)


PREVIOUS_RELEASE_SCHEMA = (
  Path(__file__).parent / "fixtures" / "schema_0013.sql"
)


def _migration_versions_before(target: str) -> list[str]:
  """Select historical setup by identity, independent of future appends."""
  versions = [version for version, _migration in migrations._SCHEMA_MIGRATIONS]
  return versions[:versions.index(target)]


def test_previous_release_database_upgrades_to_current_orm(tmp_path):
  """The real boot order must close every ORM gap on an existing install.

  Fresh databases are insufficient evidence because ``create_all`` creates
  current tables and columns before migrations run. The frozen SQL fixture is
  the empty schema from the release immediately preceding migration 0014,
  including its already-applied ledger. Loading that artifact first makes a
  newly declared column observable unless a genuinely new migration adds it.
  """
  db_path = tmp_path / "previous-release.db"
  with sqlite3.connect(db_path) as connection:
    connection.executescript(PREVIOUS_RELEASE_SCHEMA.read_text(encoding="utf-8"))

  eng = create_engine(f"sqlite:///{db_path}")
  before = {column["name"] for column in inspect(eng).get_columns("chat_runs")}
  assert "goal_plan_json" not in before
  assert "goal_plan_revision" not in before

  # Production creates new tables first, then upgrades existing ones. Keep the
  # test on that exact ordering: reversing it would prove a different system.
  models.Base.metadata.create_all(bind=eng)
  run_migrations(eng)
  first_history = schema_migration_history(eng)
  run_migrations(eng)

  assert migrations.mapped_schema_gaps(eng) == []
  assert schema_migration_history(eng) == first_history
  assert [row["version"] for row in first_history] == [
    version for version, _migration in migrations._SCHEMA_MIGRATIONS
  ]


def test_provider_admission_upgrade_preserves_legacy_uncertainty(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'provider-admission.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE chat_runs DROP COLUMN provider_execution_admitted"))
    conn.execute(text(
      "INSERT INTO chat_runs (id, chat_id, status) "
      "VALUES ('legacy', 'chat', 'running')"
    ))
  migrations._add_provider_execution_admission(eng)
  migrations._add_provider_execution_admission(eng)
  with Session(eng) as session:
    assert session.get(models.ChatRun, "legacy").provider_execution_admitted is None
    fresh = models.ChatRun(id="fresh", chat_id="chat", status="running")
    session.add(fresh)
    session.commit()
    assert fresh.provider_execution_admitted is False


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


def test_agent_work_claim_history_survives_owning_chat_purge(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'claim-history.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE owner (id INTEGER NOT NULL PRIMARY KEY)"
    ))
    conn.execute(text(
      "CREATE TABLE chats (id VARCHAR(64) NOT NULL PRIMARY KEY, "
      "deleted_at DATETIME NULL)"
    ))
    conn.execute(text(
      "CREATE TABLE agent_work_claims ("
      "id VARCHAR(64) NOT NULL PRIMARY KEY, owner_id INTEGER NOT NULL, "
      "work_key VARCHAR(256) NOT NULL, summary VARCHAR(500) NOT NULL, "
      "owner_chat_id VARCHAR(64) NOT NULL, owner_run_id VARCHAR(64) NOT NULL, "
      "owner_goal_id VARCHAR(64), previous_owner_chat_id VARCHAR(64), "
      "takeover_reason VARCHAR(1000), revision INTEGER NOT NULL DEFAULT '1', "
      "notification_revision INTEGER NOT NULL DEFAULT '1', "
      "claimed_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
      "released_at DATETIME, completed_at DATETIME, outcome VARCHAR(1000), "
      "UNIQUE (owner_id, work_key), "
      "FOREIGN KEY(owner_id) REFERENCES owner(id) ON DELETE CASCADE, "
      "FOREIGN KEY(owner_chat_id) REFERENCES chats(id) ON DELETE CASCADE)"
    ))
    conn.execute(text(
      "CREATE TABLE agent_work_interests ("
      "id VARCHAR(64) NOT NULL PRIMARY KEY, claim_id VARCHAR(64) NOT NULL, "
      "chat_id VARCHAR(64) NOT NULL, goal_id VARCHAR(64) NOT NULL, "
      "created_at DATETIME NOT NULL, resolved_at DATETIME, "
      "UNIQUE (claim_id, chat_id, goal_id), "
      "FOREIGN KEY(claim_id) REFERENCES agent_work_claims(id) ON DELETE CASCADE, "
      "FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE)"
    ))
    conn.execute(text("INSERT INTO owner (id) VALUES (1)"))
    conn.execute(text(
      "INSERT INTO chats (id, deleted_at) VALUES "
      "('done-chat', '2026-09-01 00:00:00'), "
      "('open-chat', '2026-09-01 00:00:00'), "
      "('follower-chat', NULL)"
    ))
    values = (
      "id, owner_id, work_key, summary, owner_chat_id, owner_run_id, "
      "revision, notification_revision, claimed_at, updated_at, completed_at"
    )
    conn.execute(text(
      f"INSERT INTO agent_work_claims ({values}) VALUES "
      "('done', 1, 'test:done', 'Done', 'done-chat', 'run-1', 1, 1, "
      "'2026-09-01', '2026-09-01', '2026-09-01'), "
      "('open', 1, 'test:open', 'Open', 'open-chat', 'run-2', 1, 1, "
      "'2026-09-01', '2026-09-01', NULL)"
    ))
    conn.execute(text(
      "INSERT INTO agent_work_interests "
      "(id, claim_id, chat_id, goal_id, created_at) VALUES "
      "('interest', 'open', 'follower-chat', 'goal-1', '2026-09-01')"
    ))

  migrations._make_agent_work_claim_history_durable(eng)
  inspector = inspect(eng)
  owner_chat = next(
    column for column in inspector.get_columns("agent_work_claims")
    if column["name"] == "owner_chat_id"
  )
  owner_chat_fk = next(
    item for item in inspector.get_foreign_keys("agent_work_claims")
    if item["constrained_columns"] == ["owner_chat_id"]
  )
  assert owner_chat["nullable"] is True
  assert owner_chat_fk["options"]["ondelete"] == "SET NULL"

  with eng.begin() as conn:
    released_at, outcome = conn.execute(text(
      "SELECT released_at, outcome FROM agent_work_claims WHERE id='open'"
    )).one()
    assert released_at is not None
    assert "deleted" in outcome
    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.execute(text("DELETE FROM chats WHERE id='done-chat'"))
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT owner_chat_id FROM agent_work_claims WHERE id='done'"
    )).scalar_one() is None
    assert conn.execute(text(
      "SELECT count(*) FROM agent_work_interests WHERE id='interest'"
    )).scalar_one() == 1


def test_attached_delegation_work_migration_is_additive_and_idempotent(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'attached-work.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE delegations (id VARCHAR(64) PRIMARY KEY)"
    ))

  migrations._add_attached_delegation_work(eng)
  migrations._add_attached_delegation_work(eng)

  inspector = inspect(eng)
  columns = {column["name"] for column in inspector.get_columns("delegations")}
  assert {
    "startup_prompt",
    "source_work_id",
    "source_work_intent",
    "source_work_context_app_id",
    "source_work_envelope",
    "source_work_status",
    "source_work_result",
    "source_work_active_chat_id",
  }.issubset(columns)
  indexes = {
    index["name"]: index for index in inspector.get_indexes("delegations")
  }
  assert indexes["ix_delegations_source_work_id"]["unique"]
  assert indexes["ix_delegations_source_work_active_chat_id"]["unique"]
  assert "ix_delegations_source_work_context_app_id" in indexes


def test_chat_wait_condition_owner_migration_is_additive_and_idempotent(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'wait-owner.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chat_waits (id VARCHAR(64) PRIMARY KEY)"
    ))

  migrations._add_chat_wait_condition_owner(eng)
  migrations._add_chat_wait_condition_owner(eng)

  columns = {
    column["name"] for column in inspect(eng).get_columns("chat_waits")
  }
  assert "condition_owner" in columns


def test_chat_wait_condition_owner_migration_adds_nullable_column(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'wait-condition-owner.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE chat_waits DROP COLUMN condition_owner"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0026_chat_wait_condition_owner"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 9, 4)})

  run_migrations(eng)

  columns = {
    column["name"] for column in inspect(eng).get_columns("chat_waits")
  }
  assert "condition_owner" in columns
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0026_chat_wait_condition_owner'"
    )).scalar_one() == 1


def test_chat_app_artifact_migration_preserves_prior_preview_acknowledgement(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'chat-app-artifacts.db'}")
  models.Base.metadata.create_all(eng)
  touched_at = datetime(2026, 8, 29, 12, 0, 0)
  with Session(eng) as session:
    session.add(models.Chat(id="chat-a", title="Chat A"))
    session.flush()
    session.add(models.App(
      id=7,
      name="Atlas",
      description="",
      jsx_source="",
      compiled_path="/tmp/app.js",
      slug="atlas",
      source_dir="/tmp/atlas",
      chat_id="chat-a",
      created_at=touched_at,
      updated_at=touched_at,
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE app_preview_state ("
      "app_id INTEGER PRIMARY KEY, seen_updated_at DATETIME NOT NULL, "
      "seen_as_final BOOLEAN NOT NULL DEFAULT 0)"
    ))
    conn.execute(text(
      "INSERT INTO app_preview_state (app_id, seen_updated_at, seen_as_final) "
      "VALUES (7, :ts, 1)"
    ), {"ts": touched_at})

  migrations._backfill_chat_app_artifacts(eng)
  migrations._backfill_chat_app_artifacts(eng)

  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT chat_id, app_id, touched_at, seen_at FROM chat_app_artifacts"
    )).all()
  assert len(rows) == 1
  assert rows[0][0:2] == ("chat-a", 7)
  assert str(rows[0].touched_at) == str(rows[0].seen_at)


def test_run_migrations_removes_retired_job_authority_receipts(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  eng = create_engine(f"sqlite:///{tmp_path / 'job-authority.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    memory = models.App(
      name="Memory",
      slug="memory",
      source_dir=str(tmp_path / "apps" / "memory"),
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
    "schema": 5,
    "data": {"shared_memory": "write"},
    "background": {
      "job": "fetch.sh",
      "mode": "scheduled",
    },
    "public": {"network": []},
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
  assert "published_manifest_url" in cols
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


def test_app_identity_migration_backfills_source_and_enforces_future_writes(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  eng = create_engine(f"sqlite:///{tmp_path / 'app-identity.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512))"
    ))
    conn.execute(text(
      "INSERT INTO apps (id, name, slug, source_dir) "
      "VALUES (1, 'Canonical app', 'canonical-app', NULL)"
    ))
    conn.execute(text(
      "INSERT INTO apps (id, name, slug, source_dir) "
      "VALUES (2, 'Canonical app', NULL, NULL)"
    ))

  run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    identities = conn.execute(text(
      "SELECT slug, source_dir FROM apps ORDER BY id"
    )).all()
    indexes = {item[1] for item in conn.execute(text("PRAGMA index_list(apps)"))}
  apps_root = Path(get_settings().data_dir) / "apps"
  assert identities == [
    ("canonical-app", str(apps_root / "canonical-app")),
    ("canonical-app-2", str(apps_root / "canonical-app-2")),
  ]
  assert "ix_apps_source_dir" in indexes

  with pytest.raises(IntegrityError, match="apps require slug and source_dir"):
    with eng.begin() as conn:
      conn.execute(text(
        "UPDATE apps SET source_dir = NULL WHERE id = 1"
      ))
  with pytest.raises(IntegrityError, match="apps require slug and source_dir"):
    with eng.begin() as conn:
      conn.execute(text(
        "UPDATE apps SET slug = '' WHERE id = 1"
      ))
  with pytest.raises(IntegrityError):
    with eng.begin() as conn:
      conn.execute(text(
        "UPDATE apps SET source_dir = :source_dir WHERE id = 2"
      ), {"source_dir": str(apps_root / "canonical-app")})


def test_app_identity_migration_materializes_legacy_source_without_overwriting_draft(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  occupied = apps_root / "legacy-app"
  occupied.mkdir(parents=True)
  (occupied / "index.jsx").write_text("// owner's newer draft", encoding="utf-8")
  stored = "export default function App() { return <main>Legacy</main> }"
  eng = create_engine(f"sqlite:///{tmp_path / 'legacy-source.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps (id, name, slug, source_dir, jsx_source) "
      "VALUES (7, 'Legacy app', 'legacy-app', NULL, :source)"
    ), {"source": stored})

  run_migrations(eng)
  with eng.connect() as conn:
    source_dir = Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 7"
    )).scalar_one())

  assert source_dir == apps_root / "legacy-app-legacy-7"
  assert (occupied / "index.jsx").read_text(encoding="utf-8") == "// owner's newer draft"
  assert (source_dir / "index.jsx").read_text(encoding="utf-8") == stored
  assert (source_dir / ".git").is_dir()
  from app import app_git
  assert app_git.worktree_dirty(source_dir) is False

  from app import compiler

  async def fake_compile(source, *, out_path, source_path):
    assert source == stored
    assert Path(source_path) == source_dir / "index.jsx"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("compiled legacy app", encoding="utf-8")

  class FakeDB:
    def commit(self):
      return None

    def rollback(self):
      raise AssertionError("legacy rebuild must not roll back")

  monkeypatch.setattr(compiler, "compile_jsx", fake_compile)
  app = SimpleNamespace(
    id=7,
    source_dir=str(source_dir),
    source_commit=None,
    compiled_path=str(tmp_path / "missing-old-bundle.js"),
    jsx_source=stored,
    updated_at=None,
  )
  asyncio.run(compiler.recompile_app_bundle(FakeDB(), app, stored))
  assert Path(app.compiled_path).read_text(encoding="utf-8") == "compiled legacy app"


@pytest.mark.parametrize("occupied_nominal", [False, True])
def test_app_identity_migration_retries_after_repo_initialization_crash(
  tmp_path, monkeypatch, occupied_nominal,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  if occupied_nominal:
    nominal = apps_root / "retry-app"
    nominal.mkdir(parents=True)
    (nominal / "index.jsx").write_text("// keep draft", encoding="utf-8")
  stored = "export default () => <main>Retry</main>"
  eng = create_engine(f"sqlite:///{tmp_path / 'retry-source.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES (9, 'Retry app', 'retry-app', NULL, :source)"
    ), {"source": stored})

  from app import app_git
  real_ensure_repo = app_git.ensure_repo
  failed = False

  def crash_after_repo(path):
    nonlocal failed
    real_ensure_repo(path)
    if not failed:
      failed = True
      raise OSError("simulated crash after repo initialization")

  monkeypatch.setattr(app_git, "ensure_repo", crash_after_repo)
  with pytest.raises(OSError, match="simulated crash"):
    run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    source_dir = Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 9"
    )).scalar_one())
  expected = apps_root / (
    "retry-app-legacy-9" if occupied_nominal else "retry-app"
  )
  assert source_dir == expected
  assert (source_dir / "index.jsx").read_text(encoding="utf-8") == stored
  assert not (source_dir / ".mobius-identity-migration").exists()
  assert app_git.worktree_dirty(source_dir) is False
  if occupied_nominal:
    assert (apps_root / "retry-app" / "index.jsx").read_text(
      encoding="utf-8"
    ) == "// keep draft"


def test_app_identity_migration_preserves_edit_after_partial_source_write(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  stored = "export default () => <main>Stored</main>"
  eng = create_engine(f"sqlite:///{tmp_path / 'retry-owner-edit.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES (17, 'Retry edit', 'retry-edit', NULL, :source)"
    ), {"source": stored})

  from app import app_git
  real_commit_local = app_git.commit_local
  failed = False

  def crash_before_commit(path, message):
    nonlocal failed
    if not failed:
      failed = True
      raise OSError("simulated crash after source write")
    return real_commit_local(path, message)

  monkeypatch.setattr(app_git, "commit_local", crash_before_commit)
  with pytest.raises(OSError, match="after source write"):
    run_migrations(eng)

  original = apps_root / "retry-edit"
  (original / "index.jsx").write_text("// owner's recovery edit", encoding="utf-8")
  run_migrations(eng)

  with eng.connect() as conn:
    assigned = Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 17"
    )).scalar_one())
  assert assigned == apps_root / "retry-edit-legacy-17"
  assert (original / "index.jsx").read_text(encoding="utf-8") == (
    "// owner's recovery edit"
  )
  assert (assigned / "index.jsx").read_text(encoding="utf-8") == stored


def test_app_identity_migration_rejects_existing_resolved_aliases(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  eng = create_engine(f"sqlite:///{tmp_path / 'resolved-aliases.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(1, 'One', 'one', :direct, NULL), "
      "(2, 'Two', 'two', :alias, NULL)"
    ), {
      "direct": str(apps_root / "shared"),
      "alias": str(apps_root / ".." / "apps" / "shared"),
    })

  with pytest.raises(RuntimeError, match="resolve to the same source_dir"):
    run_migrations(eng)
  assert "0004_app_identity_required" not in {
    row["version"] for row in schema_migration_history(eng)
  }


def test_app_identity_migration_canonicalizes_one_existing_alias(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  eng = create_engine(f"sqlite:///{tmp_path / 'canonical-alias.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES (1, 'One', 'one', :alias, NULL)"
    ), {"alias": str(apps_root / ".." / "apps" / "one")})

  run_migrations(eng)
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 1"
    )).scalar_one() == str((apps_root / "one").resolve())


def test_app_identity_migration_skips_symlink_loop_candidate(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  apps_root.mkdir()
  (apps_root / "loop-app").symlink_to("loop-app")
  eng = create_engine(f"sqlite:///{tmp_path / 'symlink-loop.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(19, 'Loop app', 'loop-app', NULL, 'export default () => 19')"
    ))

  run_migrations(eng)
  with eng.connect() as conn:
    assigned = Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 19"
    )).scalar_one())
  assert assigned == apps_root / "loop-app-legacy-19"
  assert (assigned / "index.jsx").read_text(encoding="utf-8") == (
    "export default () => 19"
  )


def test_app_identity_migration_retries_after_pre_marker_directory_crash(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  nominal = apps_root / "mkdir-retry"
  nominal.mkdir(parents=True)
  (nominal / "index.jsx").write_text("// occupied", encoding="utf-8")
  eng = create_engine(f"sqlite:///{tmp_path / 'mkdir-retry.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES (13, 'Retry', 'mkdir-retry', NULL, "
      "'export default () => 13')"
    ))

  target = apps_root / "mkdir-retry-legacy-13"
  real_mkdir = Path.mkdir
  failed = False

  def crash_after_mkdir(path, *args, **kwargs):
    nonlocal failed
    result = real_mkdir(path, *args, **kwargs)
    if Path(path) == target and not failed:
      failed = True
      raise OSError("simulated crash before marker publish")
    return result

  monkeypatch.setattr(Path, "mkdir", crash_after_mkdir)
  with pytest.raises(OSError, match="before marker"):
    run_migrations(eng)
  run_migrations(eng)
  with eng.connect() as conn:
    assert Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 13"
    )).scalar_one()) == target
  assert (target / "index.jsx").read_text(encoding="utf-8") == "export default () => 13"


def test_app_identity_migration_reserves_sanitized_source_names(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  claimed = apps_root / "a-b"
  eng = create_engine(f"sqlite:///{tmp_path / 'identity-collisions.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(1, 'Existing', 'existing', :claimed, 'export default 1'), "
      "(2, 'Slash', 'a/b', NULL, 'export default 2'), "
      "(3, 'Dash', 'a-b', NULL, 'export default 2')"
    ), {"claimed": str(claimed)})

  run_migrations(eng)
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT id, source_dir FROM apps ORDER BY id"
    )).all()
  assert rows == [
    (1, str(claimed)),
    (2, str(apps_root / "a-b-legacy-2")),
    (3, str(apps_root / "a-b-legacy-3")),
  ]
  assert len({source for _, source in rows}) == 3


def test_app_identity_migration_reserves_names_without_stored_jsx(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  claimed = apps_root / "same-name"
  claimed.mkdir(parents=True)
  (claimed / "draft.txt").write_text("preserve", encoding="utf-8")
  eng = create_engine(f"sqlite:///{tmp_path / 'no-jsx-identities.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512))"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(1, 'Existing', 'existing', :claimed), "
      "(2, 'Slash', 'same/name', NULL), "
      "(3, 'Dash', 'same-name', NULL)"
    ), {"claimed": str(claimed)})

  run_migrations(eng)
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT id, source_dir FROM apps ORDER BY id"
    )).all()
  assert rows == [
    (1, str(claimed)),
    (2, str(apps_root / "same-name-legacy-2")),
    (3, str(apps_root / "same-name-legacy-3")),
  ]
  assert (claimed / "draft.txt").read_text(encoding="utf-8") == "preserve"


def test_app_identity_migration_rejects_symlink_escape_before_writing(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  apps_root = tmp_path / "apps"
  apps_root.mkdir()
  outside = tmp_path / "outside"
  outside.mkdir()
  (apps_root / "escaped").symlink_to(outside, target_is_directory=True)
  eng = create_engine(f"sqlite:///{tmp_path / 'symlink-identity.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(21, 'Escaped', 'escaped', NULL, 'export default 21')"
    ))

  run_migrations(eng)
  with eng.connect() as conn:
    source_dir = Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 21"
    )).scalar_one())
  assert source_dir == apps_root / "escaped-legacy-21"
  assert list(outside.iterdir()) == []
  assert (source_dir / "index.jsx").read_text(encoding="utf-8") == "export default 21"


@pytest.mark.parametrize("unsafe_slug", ["../../outside-apps", "/tmp/outside-apps"])
def test_app_identity_migration_never_treats_url_slug_as_a_source_path(
  tmp_path, monkeypatch, unsafe_slug,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  eng = create_engine(f"sqlite:///{tmp_path / 'unsafe-slug.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, "
      "slug VARCHAR(128), source_dir VARCHAR(512), jsx_source TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES (11, 'Unsafe slug', :slug, NULL, 'export default 1')"
    ), {"slug": unsafe_slug})

  run_migrations(eng)
  with eng.connect() as conn:
    source_dir = Path(conn.execute(text(
      "SELECT source_dir FROM apps WHERE id = 11"
    )).scalar_one())
  apps_root = (tmp_path / "apps").resolve()
  assert source_dir.resolve().parent == apps_root
  assert source_dir.name and source_dir.name not in {".", ".."}


def test_fresh_app_schema_requires_nonempty_slug_and_source_dir(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'fresh-app-identity.db'}")
  models.Base.metadata.create_all(eng)
  columns = {column["name"]: column for column in inspect(eng).get_columns("apps")}
  assert columns["slug"]["nullable"] is False
  assert columns["source_dir"]["nullable"] is False
  checks = {
    item["name"] for item in inspect(eng).get_check_constraints("apps")
  }
  assert {"ck_apps_slug_nonempty", "ck_apps_source_dir_nonempty"} <= checks
  indexes = {item["name"] for item in inspect(eng).get_indexes("apps")}
  assert "ix_apps_source_dir" in indexes


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
  assert "live_assistant" not in cols
  assert "chat_live_assistants" in inspect(eng).get_table_names()


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
  # Restart continuation is always on and has no owner-default seed.
  assert "auto_resume_on_restart_default" not in cols
  with eng.connect() as conn:
    value = conn.execute(text(
      "SELECT auto_resume_on_limit_default FROM owner WHERE id = 1"
    )).scalar_one()
  assert value in (False, 0)


def test_retire_restart_resume_toggle_lifts_stranded_chats(tmp_path):
  """The one-time retirement drops the owner seed column and lifts every chat a
  prior toggle latched off, while preserving a cancelled delegation child's
  internal do-not-resurrect latch."""
  from app.schema_migrations import _retire_restart_resume_toggle

  db_path = tmp_path / "retire.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE owner (id INTEGER PRIMARY KEY, "
      "auto_resume_on_restart_default BOOLEAN NOT NULL DEFAULT TRUE)"
    ))
    conn.execute(text(
      "CREATE TABLE chats (id VARCHAR PRIMARY KEY, "
      "auto_resume_on_restart BOOLEAN NOT NULL DEFAULT TRUE)"
    ))
    conn.execute(text(
      "CREATE TABLE delegations (id VARCHAR PRIMARY KEY, "
      "child_chat_id VARCHAR, cancelled_at DATETIME NULL)"
    ))
    conn.execute(text(
      "INSERT INTO chats (id, auto_resume_on_restart) VALUES "
      "('stranded', 0), ('kept', 1), ('cancelled-child', 0), "
      "('active-child', 0)"
    ))
    # Only a CANCELLED delegation child keeps its latch; a live delegation
    # child is lifted like any other chat.
    conn.execute(text(
      "INSERT INTO delegations (id, child_chat_id, cancelled_at) VALUES "
      "('d1', 'cancelled-child', '2026-08-01 00:00:00'), "
      "('d2', 'active-child', NULL)"
    ))

  _retire_restart_resume_toggle(eng)
  # Idempotent: the dropped seed column means a second pass is a clean no-op.
  _retire_restart_resume_toggle(eng)

  assert "auto_resume_on_restart_default" not in {
    c["name"] for c in inspect(eng).get_columns("owner")
  }
  with eng.connect() as conn:
    rows = dict(conn.execute(text(
      "SELECT id, auto_resume_on_restart FROM chats"
    )).all())
  assert rows["stranded"] in (True, 1)
  assert rows["kept"] in (True, 1)
  assert rows["active-child"] in (True, 1)
  # The cancelled delegation child keeps its internal latch.
  assert rows["cancelled-child"] in (False, 0)


def test_retire_restart_resume_toggle_retries_as_one_transaction(tmp_path):
  """A failed schema retirement must not hide a half-applied migration."""
  from app.schema_migrations import _retire_restart_resume_toggle

  eng = create_engine(f"sqlite:///{tmp_path / 'retire-retry.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE owner (id INTEGER PRIMARY KEY, "
      "auto_resume_on_restart_default BOOLEAN NOT NULL DEFAULT TRUE)"
    ))
    conn.execute(text(
      "CREATE TABLE chats (id VARCHAR PRIMARY KEY, "
      "auto_resume_on_restart BOOLEAN NOT NULL DEFAULT TRUE)"
    ))
    conn.execute(text(
      "INSERT INTO chats (id, auto_resume_on_restart) VALUES ('stranded', 0)"
    ))

  def refuse_drop(_conn, _cursor, statement, _parameters, _context, _many):
    if statement.startswith("ALTER TABLE owner DROP COLUMN"):
      raise RuntimeError("simulated locked schema")

  event.listen(eng, "before_cursor_execute", refuse_drop)
  try:
    with pytest.raises(RuntimeError, match="simulated locked schema"):
      _retire_restart_resume_toggle(eng)
  finally:
    event.remove(eng, "before_cursor_execute", refuse_drop)

  assert "auto_resume_on_restart_default" in {
    c["name"] for c in inspect(eng).get_columns("owner")
  }
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT auto_resume_on_restart FROM chats WHERE id = 'stranded'"
    )).scalar_one() in (False, 0)

  _retire_restart_resume_toggle(eng)
  assert "auto_resume_on_restart_default" not in {
    c["name"] for c in inspect(eng).get_columns("owner")
  }
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT auto_resume_on_restart FROM chats WHERE id = 'stranded'"
    )).scalar_one() in (True, 1)


def test_fresh_owner_schema_has_auto_resume_default():
  column = models.Owner.__table__.c.auto_resume_on_limit_default

  assert column.nullable is False
  assert column.default is not None
  assert column.server_default is not None
  assert str(column.server_default.arg).lower() == "false"
  # Restart continuation is always on: there is no owner-default column.
  assert not hasattr(models.Owner.__table__.c, "auto_resume_on_restart_default")


def test_agent_coordination_delivery_upgrade_backfills_next_turn(tmp_path):
  """Existing peer history remains quiet when delivery becomes explicit."""
  eng = create_engine(f"sqlite:///{tmp_path / 'peer-delivery.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE agent_coordination_messages ("
      "id VARCHAR(64) PRIMARY KEY, body TEXT NOT NULL)"
    ))
    conn.execute(text(
      "INSERT INTO agent_coordination_messages (id, body) "
      "VALUES ('old-note', 'Preserve me')"
    ))

  migrations._add_agent_coordination_delivery(eng)
  migrations._add_agent_coordination_delivery(eng)

  columns = {
    column["name"]: column
    for column in inspect(eng).get_columns("agent_coordination_messages")
  }
  assert columns["delivery"]["nullable"] is False
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT delivery FROM agent_coordination_messages "
      "WHERE id = 'old-note'"
    )).scalar_one() == "next_turn"


def test_fresh_agent_coordination_delivery_defaults_to_next_turn():
  column = models.AgentCoordinationMessage.__table__.c.delivery
  assert column.nullable is False
  assert column.default.arg == "next_turn"
  assert column.server_default.arg == "next_turn"


def test_peer_context_delivery_cursor_upgrade_preserves_existing_runs(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'peer-context-cursor.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY)"
    ))
    conn.execute(text("INSERT INTO chat_runs (id) VALUES ('existing-run')"))

  migrations._add_peer_context_delivery_cursor(eng)
  migrations._add_peer_context_delivery_cursor(eng)

  columns = {
    column["name"]: column
    for column in inspect(eng).get_columns("chat_runs")
  }
  assert columns["peer_message_through_created_at"]["nullable"] is True
  assert columns["peer_message_through_id"]["nullable"] is True
  assert columns["peer_message_delivery_pending"]["nullable"] is True
  with eng.connect() as conn:
    row = conn.execute(text(
      "SELECT peer_message_through_created_at, peer_message_through_id, "
      "peer_message_delivery_pending "
      "FROM chat_runs WHERE id = 'existing-run'"
    )).one()
  assert tuple(row) == (None, None, None)


def test_fresh_chat_run_has_nullable_peer_context_delivery_cursor():
  columns = models.ChatRun.__table__.c
  assert columns.peer_message_through_created_at.nullable is True
  assert columns.peer_message_through_id.nullable is True
  assert columns.peer_message_delivery_pending.nullable is True


def test_activity_delivery_upgrade_preserves_existing_runs(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'activity-delivery.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY)"
    ))
    conn.execute(text("INSERT INTO chat_runs (id) VALUES ('existing-run')"))

  migrations._add_chat_run_activity_delivery(eng)
  migrations._add_chat_run_activity_delivery(eng)

  columns = {
    column["name"]: column
    for column in inspect(eng).get_columns("chat_runs")
  }
  assert columns["activity_delivery_json"]["nullable"] is True
  with eng.connect() as conn:
    value = conn.execute(text(
      "SELECT activity_delivery_json FROM chat_runs "
      "WHERE id = 'existing-run'"
    )).scalar_one()
  assert value is None


def test_fresh_chat_run_has_nullable_activity_delivery_envelope():
  assert models.ChatRun.__table__.c.activity_delivery_json.nullable is True


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
    "0004_app_identity_required",
    "0005_connectors",
    "0006_connector_capability_identity",
    "0007_chat_has_messages",
    "0008_chat_search_documents",
    "0009_app_connections_manage",
    "0010_chat_pending_question_id",
    "0011_delegation_parent_wake",
    "0012_connector_oauth_gcloud",
    "0013_app_hosted_publication",
    "0014_chat_run_goal_plan",
    "0015_chat_run_goal_identity",
    "0016_app_connect_manage",
    "0017_retire_restart_resume_toggle",
    "0018_explicit_legacy_chat_models",
    "0019_chat_active_assistant_identity",
    "0020_app_project_templates",
    "0021_project_chat_collection",
    "0022_project_artifacts",
    "0023_project_color",
    "0024_chat_goal_dismissal",
    "0025_attached_delegation_work",
    "0026_chat_wait_condition_owner",
    "0027_chat_run_goal_identity_index",
    "0028_agent_coordination_rooms",
    "0029_agent_coordination_send_identity",
    "0030_agent_coordination_send_target",
    "0031_chat_retention_orphan_repair",
    "0032_owner_auth_mode",
    "0033_shared_app_retention",
    "0034_shared_app_path_state",
    "0035_project_artifact_drawer_state",
    "0036_chat_app_artifacts",
    "0037_explicit_active_chat_models",
    "0038_repair_active_chat_model_gaps",
    "0039_agent_work_claim_history",
    "0040_provider_execution_admission",
    "0041_app_runtime_revision",
    "0042_linked_app_project_runtime",
    "0043_agent_coordination_delivery",
    "0044_peer_context_delivery_cursor",
    "0045_chat_live_assistants",
    "0046_chat_run_activity_delivery",
    "0047_chat_activity_positions",
    "0048_delegation_result_incorporation",
    "0048_typed_platform_activation_waits",
  ]
  assert second == first


def test_chat_retention_repair_reclaims_broken_workflow_graph(
  tmp_path, monkeypatch,
):
  """0031 repairs old hard-purges and preserves unrelated durable state."""
  data_dir = tmp_path / "data"
  monkeypatch.setattr(get_settings(), "data_dir", str(data_dir))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'retention-orphans.db'}")
  models.Base.metadata.create_all(eng)

  controller_id = "missing-controller"
  child_id = "orphan-child"
  nested_id = "orphan-nested"
  survivor_id = "survivor"
  source_dir = data_dir / "apps" / "repair"
  source_dir.mkdir(parents=True)
  app = models.App(
    name="Repair", slug="repair", source_dir=str(source_dir),
  )
  controller = models.Chat(
    id=controller_id, title="Controller", messages=[], provider="codex",
  )
  child = models.Chat(
    id=child_id, title="Child", messages=[], provider="codex",
  )
  nested = models.Chat(
    id=nested_id, title="Nested", messages=[], provider="codex",
  )
  survivor = models.Chat(
    id=survivor_id, title="Survivor", messages=[], provider="codex",
  )
  missing_run = models.ChatRun(
    id="missing-run", root_run_id="missing-run", chat_id=survivor_id,
    status="completed", provider="codex",
  )
  valid_run = models.ChatRun(
    id="valid-run", root_run_id="valid-run", chat_id=survivor_id,
    status="completed", provider="codex",
  )
  with Session(eng) as session:
    session.add_all((app, controller, child, nested, survivor))
    session.flush()
    session.add_all((
      models.GauntletRun(
        id="orphan-gauntlet", app_id=app.id,
        parent_chat_id=controller.id, parent_root_run_id="controller-run",
        target_path="/data/platform", contract_json={},
        contract_sha256="a" * 64, provider="codex", status="stopped",
        phase="terminal", current_round=1, max_rounds=1, revision=1,
      ),
      models.Delegation(
        id="orphan-delegation", app_id=app.id,
        parent_chat_id=controller.id, parent_root_run_id="controller-run",
        task_key="critic", child_chat_id=child.id, provider="codex",
        scope="read", cwd="/data/platform", prompt_sha256="b" * 64,
      ),
      models.Delegation(
        id="nested-delegation", app_id=app.id,
        parent_chat_id=child.id, parent_root_run_id="child-run",
        task_key="nested", child_chat_id=nested.id, provider="codex",
        scope="read", cwd="/data/platform", prompt_sha256="c" * 64,
      ),
      models.ContributionAutopilot(
        app_id=app.id, record_id="dangling", followup_chat_id=controller.id,
      ),
      models.ContributionAutopilot(
        app_id=app.id, record_id="valid", followup_chat_id=survivor.id,
      ),
      missing_run,
      valid_run,
    ))
    session.flush()
    session.add(models.GauntletTask(
      id="orphan-task", gauntlet_run_id="orphan-gauntlet",
      phase="baseline", round=0, ordinal=0, role="critic", scope="read",
      delegation_id="orphan-delegation", prompt_sha256="d" * 64,
    ))
    for key, run_id in (("orphan-event", "missing-run"),
                        ("valid-event", "valid-run")):
      session.add(models.AgentLifecycleEvent(
        event_key=key, chat_id=survivor.id, chat_run_id=run_id,
        provider="codex", provider_agent_id=key, agent_id=key,
        activation_id=f"activation-{key}", parent_kind="unknown",
        event_type="agent_terminal", state="done", time_quality="observed",
        source="runner",
      ))
    session.commit()

  for chat_id in (child_id, nested_id, survivor_id):
    path = data_dir / "chats" / chat_id
    path.mkdir(parents=True)
    (path / "marker").write_text("derived", encoding="utf-8")

  # Reproduce the pre-retention-fix state: raw hard deletes bypassed the graph
  # and lifecycle cleanup while SQLite foreign-key enforcement was absent.
  with eng.begin() as conn:
    conn.execute(text("DELETE FROM chats WHERE id = 'missing-controller'"))
    conn.execute(text("DELETE FROM chat_runs WHERE id = 'missing-run'"))

  run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(text(
      "SELECT followup_chat_id FROM contribution_autopilot "
      "WHERE record_id = 'dangling'"
    )).scalar_one() is None
    assert conn.execute(text(
      "SELECT followup_chat_id FROM contribution_autopilot "
      "WHERE record_id = 'valid'"
    )).scalar_one() == survivor_id
    assert conn.execute(text(
      "SELECT event_key FROM agent_lifecycle_events ORDER BY event_key"
    )).scalars().all() == ["valid-event"]
    for table in ("gauntlet_tasks", "gauntlet_runs", "delegations"):
      assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    assert conn.execute(text(
      "SELECT id FROM chats ORDER BY id"
    )).scalars().all() == [survivor_id]
  assert not (data_dir / "chats" / child_id).exists()
  assert not (data_dir / "chats" / nested_id).exists()
  assert (data_dir / "chats" / survivor_id).exists()
  assert "0031_chat_retention_orphan_repair" in {
    row["version"] for row in schema_migration_history(eng)
  }


def test_project_chat_collection_migration_preserves_and_backfills_legacy_pair(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'project-chats.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE apps (id INTEGER PRIMARY KEY)"))
    conn.execute(text(
      "CREATE TABLE chats ("
      "id VARCHAR(64) PRIMARY KEY, title VARCHAR(256) NOT NULL"
      ")"
    ))
    conn.execute(text(
      "CREATE TABLE projects ("
      "id VARCHAR(64) PRIMARY KEY, name VARCHAR(256) NOT NULL, "
      "project_type VARCHAR(128) NOT NULL, root_path VARCHAR(1024) NOT NULL UNIQUE, "
      "chat_id VARCHAR(64) NOT NULL UNIQUE REFERENCES chats(id), "
      "source_app_id INTEGER NULL REFERENCES apps(id) ON DELETE SET NULL, "
      "template_snapshot_json JSON NOT NULL, legacy_source_json JSON NULL, "
      "deleted_at DATETIME NULL, created_at DATETIME NULL, updated_at DATETIME NULL"
      ")"
    ))
    conn.execute(text("INSERT INTO chats (id, title) VALUES ('chat-1', 'Legacy')"))
    conn.execute(text(
      "INSERT INTO projects "
      "(id, name, project_type, root_path, chat_id, template_snapshot_json) "
      "VALUES ('project-1', 'Legacy', 'blank', 'projects/project-1', "
      "'chat-1', '{}')"
    ))

  migrations._add_project_chat_collection(eng)
  migrations._add_project_chat_collection(eng)

  project_columns = {
    column["name"]: column for column in inspect(eng).get_columns("projects")
  }
  chat_columns = {column["name"] for column in inspect(eng).get_columns("chats")}
  assert project_columns["chat_id"]["nullable"] is True
  assert "project_id" in chat_columns
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT project_id FROM chats WHERE id = 'chat-1'"
    )).scalar() == "project-1"
    assert conn.execute(text(
      "SELECT chat_id FROM projects WHERE id = 'project-1'"
    )).scalar() is None


def test_project_artifacts_migration_adds_nullable_column_idempotently(tmp_path):
  """0019 adds artifacts_json to an already-deployed projects table."""
  eng = create_engine(f"sqlite:///{tmp_path / 'project-artifacts.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE apps (id INTEGER PRIMARY KEY)"))
    conn.execute(text(
      "CREATE TABLE projects ("
      "id VARCHAR(64) PRIMARY KEY, name VARCHAR(256) NOT NULL, "
      "project_type VARCHAR(128) NOT NULL, root_path VARCHAR(1024) NOT NULL, "
      "template_snapshot_json JSON NOT NULL)"
    ))
    conn.execute(text(
      "INSERT INTO projects (id, name, project_type, root_path, "
      "template_snapshot_json) VALUES ('p1', 'Legacy', 'blank', "
      "'projects/p1', '{}')"
    ))

  migrations._add_project_artifacts(eng)
  migrations._add_project_artifacts(eng)

  columns = {c["name"] for c in inspect(eng).get_columns("projects")}
  assert "artifacts_json" in columns
  with eng.connect() as conn:
    value = conn.execute(text(
      "SELECT artifacts_json FROM projects WHERE id = 'p1'"
    )).scalar_one()
  assert value is None


def test_project_artifacts_migration_no_projects_table_is_a_noop(tmp_path):
  """Fresh installs (no projects table yet) run the migration harmlessly."""
  eng = create_engine(f"sqlite:///{tmp_path / 'no-projects.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE apps (id INTEGER PRIMARY KEY)"))
  migrations._add_project_artifacts(eng)
  assert "projects" not in inspect(eng).get_table_names()


def test_shared_app_path_state_migrates_prototype_data_without_runtime_columns(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  instance_id = "11111111-1111-4111-8111-111111111111"
  snapshot_path = f"shared/app-instances/{instance_id}/build"
  (data_dir / snapshot_path).mkdir(parents=True)
  eng = create_engine(f"sqlite:///{tmp_path / 'shared-state.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE shared_app_instances ("
      "id VARCHAR(64) PRIMARY KEY, snapshot_path VARCHAR(2048) NOT NULL, "
      "state_json JSON NOT NULL, revision INTEGER NOT NULL)"
    ))
    conn.execute(text(
      "INSERT INTO shared_app_instances (id, snapshot_path, state_json, revision) "
      "VALUES (:id, :snapshot_path, :state_json, 7)"
    ), {
      "id": instance_id,
      "snapshot_path": snapshot_path,
      "state_json": json.dumps({"board.json": {"cards": ["kept"]}}),
    })

  migrations._migrate_shared_app_state_files(eng)

  assert json.loads((
    data_dir / "shared" / "app-instances" / instance_id / "data" / "board.json"
  ).read_text(encoding="utf-8")) == {"cards": ["kept"]}
  with eng.connect() as conn:
    migrated = conn.execute(text(
      "SELECT state_json, revision FROM shared_app_instances WHERE id = :id"
    ), {"id": instance_id}).mappings().one()
  assert json.loads(migrated["state_json"]) == {}
  assert migrated["revision"] == 0
  change_columns = {
    column["name"] for column in inspect(eng).get_columns("shared_app_changes")
  }
  assert change_columns == {
    "id", "instance_id", "kind", "path", "version", "actor_key",
    "display_name", "created_at",
  }
  change_indexes = {
    index["name"] for index in inspect(eng).get_indexes("shared_app_changes")
  }
  assert change_indexes == {
    "ix_shared_app_changes_created_at",
    "ix_shared_app_changes_instance_id",
  }
  assert "state_json" not in models.SharedAppInstance.__table__.columns
  assert "revision" not in models.SharedAppInstance.__table__.columns


def test_project_artifact_drawer_migration_is_self_contained_and_idempotent(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'artifact-drawer.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY)"))

  migrations._add_project_artifact_drawer_state(eng)
  migrations._add_project_artifact_drawer_state(eng)

  columns = {
    column["name"]: column
    for column in inspect(eng).get_columns("project_artifact_drawer_state")
  }
  assert set(columns) == {"project_id", "artifact_id", "last_opened_at"}
  assert columns["last_opened_at"]["nullable"] is False
  assert inspect(eng).get_pk_constraint(
    "project_artifact_drawer_state"
  )["constrained_columns"] == ["project_id", "artifact_id"]


def test_pending_question_migration_backfills_only_active_latest_question(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'pending-question.db'}")
  question = {
    "type": "question",
    "question_id": "q-active",
    "questions": [{"id": "choice", "question": "Choose"}],
  }
  transcript = [
    {"role": "user", "content": "start"},
    {
      "role": "assistant",
      # Output after the card is why the marker must be position-independent.
      "blocks": [question, {"type": "text", "content": "parallel output"}],
    },
  ]
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chats ("
      "id VARCHAR(64) PRIMARY KEY, messages JSON, deleted_at DATETIME NULL)"
    ))
    conn.execute(text(
      "CREATE TABLE chat_runs ("
      "id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(64), status VARCHAR(32))"
    ))
    for chat_id in ("active", "completed", "superseded"):
      messages = transcript
      if chat_id == "superseded":
        messages = [*transcript, {"role": "user", "content": "move on"}]
      conn.execute(text(
        "INSERT INTO chats (id, messages) VALUES (:id, :messages)"
      ), {"id": chat_id, "messages": json.dumps(messages)})
    conn.execute(text(
      "INSERT INTO chat_runs (id, chat_id, status) VALUES "
      "('r-active', 'active', 'running'), "
      "('r-completed', 'completed', 'completed'), "
      "('r-superseded', 'superseded', 'running')"
    ))

  migrations._add_chat_pending_question_id(eng)
  migrations._add_chat_pending_question_id(eng)

  assert "pending_question_id" in {
    column["name"] for column in inspect(eng).get_columns("chats")
  }
  with eng.connect() as conn:
    markers = dict(conn.execute(text(
      "SELECT id, pending_question_id FROM chats ORDER BY id"
    )).all())
  assert markers == {
    "active": "q-active",
    "completed": None,
    "superseded": None,
  }


def test_active_assistant_identity_migration_backfills_live_and_parked_rows(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'assistant-identity.db'}")
  parked_messages = [
    {
      "id": "assistant-older",
      "role": "assistant",
      "blocks": [{
        "type": "question",
        "question_id": "q-older",
        "answers": {"pick": "done"},
      }],
    },
    {"role": "user", "hidden": True, "kind": "wait_result"},
    {
      "id": "assistant-current",
      "role": "assistant",
      "blocks": [{
        "type": "question",
        "question_id": "q-current",
      }],
    },
  ]
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chats ("
      "id VARCHAR(64) PRIMARY KEY, messages JSON, live_assistant JSON, "
      "pending_question_id VARCHAR(64), deleted_at DATETIME NULL)"
    ))
    rows = (
      (
        "live",
        [],
        {"id": "assistant-live", "role": "assistant", "blocks": []},
        None,
        None,
      ),
      ("parked", parked_messages, None, "q-current", None),
      (
        "parked-with-stale-live",
        parked_messages,
        {"id": "assistant-stale", "role": "assistant", "blocks": []},
        "q-current",
        None,
      ),
      ("missing-question", parked_messages, None, "q-missing", None),
      ("historical-only", parked_messages, None, None, None),
      ("idless", [{
        "role": "assistant",
        "blocks": [{"type": "question", "question_id": "q-idless"}],
      }], None, "q-idless", None),
      (
        "deleted",
        [],
        {"id": "assistant-deleted", "role": "assistant", "blocks": []},
        None,
        "2026-08-25 12:00:00",
      ),
    )
    for chat_id, messages, live, pending_question_id, deleted_at in rows:
      conn.execute(text(
        "INSERT INTO chats "
        "(id, messages, live_assistant, pending_question_id, deleted_at) "
        "VALUES (:id, :messages, :live, :pending, :deleted_at)"
      ), {
        "id": chat_id,
        "messages": json.dumps(messages),
        "live": json.dumps(live) if live is not None else None,
        "pending": pending_question_id,
        "deleted_at": deleted_at,
      })

  migrations._add_chat_active_assistant_identity(eng)
  # Simulate a process death after SQLite committed the ALTER/backfill but
  # before the ledger marker: a retry sees the column and must still repair any
  # row whose scalar did not land.
  with eng.begin() as conn:
    conn.execute(text(
      "UPDATE chats SET active_assistant_message_id = NULL "
      "WHERE id IN ('parked', 'idless')"
    ))
  migrations._add_chat_active_assistant_identity(eng)

  assert "active_assistant_message_id" in {
    column["name"] for column in inspect(eng).get_columns("chats")
  }
  with eng.connect() as conn:
    migrated = conn.execute(text(
      "SELECT id, active_assistant_message_id, messages "
      "FROM chats ORDER BY id"
    )).mappings().all()
  owners = {
    row["id"]: row["active_assistant_message_id"] for row in migrated
  }
  assert owners == {
    "deleted": None,
    "historical-only": None,
    "idless": None,
    "live": "assistant-live",
    "missing-question": None,
    "parked": "assistant-current",
    "parked-with-stale-live": "assistant-current",
  }
  idless = next(row for row in migrated if row["id"] == "idless")
  idless_messages = (
    json.loads(idless["messages"])
    if isinstance(idless["messages"], str)
    else idless["messages"]
  )
  assert "id" not in idless_messages[-1]


def test_connections_manage_reaches_a_ledgered_database(tmp_path):
  """The 2026-08-04 outage: a column added only to recorded 0001 never
  arrives on a database whose ledger already contains 0001. A numbered,
  schema-gated migration must add it — including on a hand-patched table."""
  eng = create_engine(f"sqlite:///{tmp_path / 'ledgered-apps.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "token_nonce VARCHAR(32), capability_contract JSON)"
    ))
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    # The ledger says every pre-0009 migration ran cleanly — exactly the
    # production state where the ORM expected a column the DB lacked.
    for version in (
      "0001_legacy_schema_convergence",
      "0002_chat_run_goal_objective",
      "0003_chat_run_root_identity",
      "0004_app_identity_required",
      "0005_connectors",
      "0006_connector_capability_identity",
      "0007_chat_has_messages",
      "0008_chat_search_documents",
    ):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-04 00:00:00')"
      ), {"version": version})

  run_migrations(eng)
  columns = {c["name"] for c in inspect(eng).get_columns("apps")}
  assert "connections_manage" in columns
  # Idempotent over the hand-patched production shape too.
  run_migrations(eng)
  assert "0009_app_connections_manage" in {
    entry["version"] for entry in schema_migration_history(eng)
  }


def test_connect_manage_reaches_a_fully_ledgered_database(tmp_path):
  """The append-only migration grants existing installs the new column."""
  eng = create_engine(f"sqlite:///{tmp_path / 'connect-manage.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "token_nonce VARCHAR(32), capability_contract JSON)"
    ))
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0016_app_connect_manage"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-23 00:00:00')"
      ), {"version": version})

  run_migrations(eng)
  columns = {column["name"] for column in inspect(eng).get_columns("apps")}
  assert "connect_manage" in columns
  run_migrations(eng)
  assert "0016_app_connect_manage" in {
    entry["version"] for entry in schema_migration_history(eng)
  }


def test_legacy_chat_models_pin_only_established_unselected_chats(
  tmp_path, monkeypatch,
):
  """0018 repairs invisible defaults without creating a new default path."""
  data_dir = tmp_path / "data"
  shared = data_dir / "shared"
  shared.mkdir(parents=True)
  (shared / "agent-settings.json").write_text(json.dumps({
    "model": "gpt-5.6-sol",
    "effort": "xhigh",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'legacy-chat-models.db'}")
  models.Base.metadata.create_all(eng)
  established = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
  ]
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner",
      hashed_password="hash",
      provider="codex",
    ))
    session.add(models.App(
      id=99, name="Test app", slug="test-app", source_dir="test-app",
    ))
    rows = [
      models.Chat(
        id="claude-source-old", title="Old Claude choice", provider="claude",
        messages=[], agent_settings_json={"model": "claude-sonnet-4-6"},
        activity_at=datetime(2026, 8, 20),
      ),
      models.Chat(
        id="claude-source-current", title="Current Claude choice",
        provider="claude", messages=[],
        agent_settings_json={"model": "claude-opus-4-8"},
        activity_at=datetime(2026, 8, 22),
      ),
      # A newer app-owned model is not evidence of the owner's picker choice.
      models.Chat(
        id="claude-app-source", title="App model", provider="claude",
        messages=[], agent_settings_json={"model": "claude-fable-5"},
        created_by_app_id=99, activity_at=datetime(2026, 8, 23),
      ),
      models.Chat(
        id="legacy-claude", title="Legacy Claude", provider="claude",
        messages=established, agent_settings_json=None,
      ),
      models.Chat(
        id="legacy-codex", title="Legacy Codex", provider="codex",
        messages=established,
        agent_settings_json={"effort": "high", "project_id": "alpha"},
      ),
      models.Chat(
        id="legacy-app", title="Legacy app", provider="claude",
        messages=established, created_by_app_id=99,
        agent_settings_json={"report_kind": "reflection"},
      ),
      models.Chat(
        id="empty-chat", title="First run", provider="codex",
        messages=[{"role": "user", "content": "not completed"}],
        agent_settings_json={"effort": "medium"},
      ),
      models.Chat(
        id="deleted-chat", title="Deleted", provider="codex",
        messages=established, agent_settings_json=None,
        deleted_at=datetime(2026, 8, 22),
      ),
      models.Chat(
        id="explicit-chat", title="Explicit", provider="codex",
        messages=established,
        agent_settings_json={"model": "gpt-5.5", "effort": "low"},
      ),
    ]
    session.add_all(rows)
    session.commit()

  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before(
      "0018_explicit_legacy_chat_models",
    ):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-23 00:00:00')"
      ), {"version": version})

  run_migrations(eng)
  with Session(eng) as session:
    settings = {
      row.id: row.agent_settings_json
      for row in session.query(models.Chat).all()
    }
  assert settings["legacy-claude"] == {"model": "claude-opus-4-8"}
  assert settings["legacy-codex"] == {
    "effort": "high",
    "project_id": "alpha",
    "model": "gpt-5.6-sol",
  }
  assert settings["legacy-app"] == {
    "report_kind": "reflection",
    "model": "claude-opus-4-8",
  }
  assert settings["empty-chat"] == {
    "effort": "medium", "model": "gpt-5.6-sol",
  }
  assert settings["deleted-chat"] is None
  assert settings["explicit-chat"] == {"model": "gpt-5.5", "effort": "low"}

  # Simulate a crash after the data commit but before the ledger insert. The
  # retry must no-op rather than revising any newly explicit conversation.
  with eng.begin() as conn:
    conn.execute(text(
      "DELETE FROM schema_migrations "
      "WHERE version = '0018_explicit_legacy_chat_models'"
    ))
  run_migrations(eng)
  with Session(eng) as session:
    assert session.get(models.Chat, "legacy-codex").agent_settings_json == {
      "effort": "high",
      "project_id": "alpha",
      "model": "gpt-5.6-sol",
    }


def test_legacy_chat_models_never_invent_a_provider_default(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  shared = data_dir / "shared"
  shared.mkdir(parents=True)
  (shared / "agent-settings.json").write_text(json.dumps({
    "model": "gpt-5.6-sol",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'no-invented-model.db'}")
  models.Base.metadata.create_all(eng)
  transcript = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
  ]
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner",
      hashed_password="hash",
      provider="codex",
    ))
    session.add_all([
      models.Chat(
        id="known-provider-choice", title="Codex", provider="codex",
        messages=transcript, agent_settings_json=None,
      ),
      models.Chat(
        id="no-provider-choice", title="Claude", provider="claude",
        messages=transcript, agent_settings_json=None,
      ),
    ])
    session.commit()

  migrations._pin_established_legacy_chat_models(eng)
  migrations._pin_established_legacy_chat_models(eng)

  with Session(eng) as session:
    assert session.get(
      models.Chat, "known-provider-choice",
    ).agent_settings_json == {"model": "gpt-5.6-sol"}
    assert session.get(
      models.Chat, "no-provider-choice",
    ).agent_settings_json is None


def test_legacy_chat_models_preserve_malformed_settings(tmp_path, monkeypatch):
  data_dir = tmp_path / "data"
  shared = data_dir / "shared"
  shared.mkdir(parents=True)
  (shared / "agent-settings.json").write_text(json.dumps({
    "model": "gpt-5.6-sol",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'malformed-settings.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner",
      hashed_password="hash",
      provider="codex",
    ))
    session.add(models.Chat(
      id="malformed-settings",
      title="Malformed settings",
      provider="codex",
      messages=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
      ],
      agent_settings_json=None,
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "UPDATE chats SET agent_settings_json = '{malformed' "
      "WHERE id = 'malformed-settings'"
    ))

  migrations._pin_established_legacy_chat_models(eng)

  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT agent_settings_json FROM chats WHERE id = 'malformed-settings'"
    )).scalar_one() == "{malformed"


def test_hosted_publication_reaches_a_fully_ledgered_private_app(tmp_path):
  """0013 adds snapshot fields and a closed public contract to old rows."""
  eng = create_engine(f"sqlite:///{tmp_path / 'ledgered-public-apps.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "token_nonce VARCHAR(32), capability_contract JSON)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(1, 'Old app', 'old-app', 'nonce', :contract)"
    ), {"contract": json.dumps({"schema": 4, "runtime": {}})})
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0013_app_hosted_publication"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-15 00:00:00')"
      ), {"version": version})

  run_migrations(eng)
  columns = {c["name"] for c in inspect(eng).get_columns("apps")}
  assert "public_enabled" not in columns
  assert "public_bundle_path" in columns
  with eng.connect() as conn:
    public_bundle, raw_contract = conn.execute(text(
      "SELECT public_bundle_path, capability_contract FROM apps WHERE id = 1"
    )).one()
  contract = json.loads(raw_contract) if isinstance(raw_contract, str) else raw_contract
  assert public_bundle is None
  assert contract["schema"] == 5
  assert contract["public"] == {"network": []}
  assert "0013_app_hosted_publication" in {
    entry["version"] for entry in schema_migration_history(eng)
  }


def test_hosted_publication_migrates_the_unmerged_live_flag_to_a_snapshot(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  compiled = tmp_path / "compiled"
  compiled.mkdir()
  module = b"export default function App(){return null}\n"
  digest = hashlib.sha256(module).hexdigest()
  installed_bundle = compiled / f"app-1-{digest}.js"
  installed_bundle.write_bytes(module)
  eng = create_engine(f"sqlite:///{tmp_path / 'flag-to-snapshot.db'}")
  contract = {"schema": 5, "public": {"network": []}}
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "token_nonce VARCHAR(32), compiled_path VARCHAR(512), "
      "source_commit VARCHAR(64), capability_contract JSON, "
      "share_manifest_url VARCHAR(1024), "
      "public_enabled BOOLEAN NOT NULL DEFAULT FALSE)"
    ))
    conn.execute(text(
      "INSERT INTO apps VALUES "
      "(1, 'Live app', 'live-app', 'nonce', :bundle, :commit, :contract, "
      ":manifest, TRUE)"
    ), {
      "bundle": str(installed_bundle),
      "commit": "a" * 40,
      "contract": json.dumps(contract),
      "manifest": "https://example.test/live-app/mobius.json",
    })
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0013_app_hosted_publication"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-15 00:00:00')"
      ), {"version": version})

  run_migrations(eng)

  columns = {c["name"] for c in inspect(eng).get_columns("apps")}
  assert "public_enabled" not in columns
  assert "share_manifest_url" not in columns
  with eng.connect() as conn:
    row = conn.execute(text(
      "SELECT published_manifest_url, public_bundle_path, "
      "public_name, public_bundle_digest, public_source_commit, "
      "public_access_contract, "
      "public_token_nonce "
      "FROM apps WHERE id = 1"
    )).one()
  assert row.published_manifest_url == "https://example.test/live-app/mobius.json"
  assert row.public_name == "Live app"
  assert Path(row.public_bundle_path).read_bytes() == module
  assert Path(row.public_bundle_path) != installed_bundle
  assert row.public_bundle_digest == digest
  assert row.public_source_commit == "a" * 40
  public_access = (
    json.loads(row.public_access_contract)
    if isinstance(row.public_access_contract, str)
    else row.public_access_contract
  )
  assert public_access == {
    "network": [],
    "storage": {"read": False, "write_prefix": None},
  }
  assert len(row.public_token_nonce) == 32


def test_connector_oauth_gcloud_migration_upgrades_legacy_rows_idempotently(
  tmp_path,
):
  """An existing OAuth grant gains Google fields without losing its mode.

  Deleting the ledger marker after the first run simulates a crash after the
  ALTER statements committed but before the migration was recorded. The retry
  must see the columns, preserve the legacy row, and complete normally.
  """
  eng = create_engine(f"sqlite:///{tmp_path / 'legacy-connector-oauth.db'}")
  previous_versions = (
    "0001_legacy_schema_convergence",
    "0002_chat_run_goal_objective",
    "0003_chat_run_root_identity",
    "0004_app_identity_required",
    "0005_connectors",
    "0006_connector_capability_identity",
    "0007_chat_has_messages",
    "0008_chat_search_documents",
    "0009_app_connections_manage",
    "0010_chat_pending_question_id",
    "0011_delegation_parent_wake",
  )
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE connector_oauth ("
      "connector_id INTEGER PRIMARY KEY, resource VARCHAR(2048) NOT NULL, "
      "issuer VARCHAR(512) NOT NULL, "
      "authorization_endpoint VARCHAR(2048) NOT NULL, "
      "token_endpoint VARCHAR(2048) NOT NULL, "
      "registration_endpoint VARCHAR(2048), "
      "revocation_endpoint VARCHAR(2048), "
      "scopes_advertised JSON NOT NULL, access_token_encrypted TEXT, "
      "refresh_token_encrypted TEXT, access_expires_at DATETIME, "
      "scopes_granted JSON NOT NULL, connected_at DATETIME)"
    ))
    conn.execute(text(
      "INSERT INTO connector_oauth "
      "(connector_id, resource, issuer, authorization_endpoint, "
      "token_endpoint, scopes_advertised, access_token_encrypted, "
      "refresh_token_encrypted, scopes_granted) VALUES "
      "(7, 'https://mcp.example/mcp', 'https://issuer.example', "
      "'https://issuer.example/auth', 'https://issuer.example/token', "
      "'[]', 'sealed-access', 'sealed-refresh', '[]')"
    ))
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in previous_versions:
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, '2026-08-06 00:00:00')"
      ), {"version": version})

  run_migrations(eng)
  with eng.begin() as conn:
    conn.execute(text(
      "DELETE FROM schema_migrations "
      "WHERE version = '0012_connector_oauth_gcloud'"
    ))
  run_migrations(eng)

  columns = {
    column["name"]: column
    for column in inspect(eng).get_columns("connector_oauth")
  }
  assert set((
    "auth_mode", "client_id", "client_secret_encrypted", "user_project",
  )).issubset(columns)
  assert columns["auth_mode"]["nullable"] is False
  assert columns["auth_mode"]["default"] is not None
  with eng.connect() as conn:
    row = conn.execute(text(
      "SELECT auth_mode, client_id, client_secret_encrypted, user_project, "
      "access_token_encrypted, refresh_token_encrypted "
      "FROM connector_oauth WHERE connector_id = 7"
    )).one()
  assert tuple(row) == (
    "browser", None, None, None, "sealed-access", "sealed-refresh",
  )
  assert "0012_connector_oauth_gcloud" in {
    entry["version"] for entry in schema_migration_history(eng)
  }


def test_mapped_schema_gaps_reports_missing_columns(tmp_path):
  from app.database import Base
  from app.schema_migrations import mapped_schema_gaps

  eng = create_engine(f"sqlite:///{tmp_path / 'parity.db'}")
  Base.metadata.create_all(bind=eng)
  assert mapped_schema_gaps(eng) == []
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE apps DROP COLUMN connections_manage"))
  assert "apps.connections_manage" in mapped_schema_gaps(eng)


def test_connectors_migration_preserves_preview_era_rows(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'preview-connectors.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(255), slug VARCHAR(128), "
      "source_dir VARCHAR(512))"
    ))
    conn.execute(text(
      "CREATE TABLE connectors ("
      "id INTEGER PRIMARY KEY, slug VARCHAR(64) NOT NULL UNIQUE, "
      "name VARCHAR(128) NOT NULL, url VARCHAR(2048) NOT NULL, "
      "auth_header VARCHAR(64), auth_value_encrypted TEXT, "
      "enabled BOOLEAN NOT NULL DEFAULT TRUE, tools_json JSON NOT NULL, "
      "est_tokens INTEGER NOT NULL DEFAULT 0, status VARCHAR(16) NOT NULL, "
      "status_detail TEXT, created_at DATETIME, last_checked_at DATETIME)"
    ))
    # Simulate a preview checkout that already recorded the original table
    # migration before immutable broker identities were added in 0006.
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    conn.execute(text(
      "INSERT INTO schema_migrations (version, applied_at) "
      "VALUES ('0005_connectors', '2026-08-03 00:00:00')"
    ))
    conn.execute(text(
      "INSERT INTO connectors ("
      "id, slug, name, url, auth_header, auth_value_encrypted, enabled, "
      "tools_json, est_tokens, status) VALUES ("
      "7, 'preview', 'Preview', 'https://mcp.example/mcp', "
      "'Authorization', 'encrypted-preview-key', TRUE, '[]', 0, 'ok')"
    ))

  run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    row = conn.execute(text(
      "SELECT slug, url, auth_value_encrypted, capability_id "
      "FROM connectors WHERE id = 7"
    )).one()
  assert tuple(row[:3]) == (
    "preview", "https://mcp.example/mcp", "encrypted-preview-key",
  )
  assert isinstance(row.capability_id, str) and len(row.capability_id) == 64
  assert "0005_connectors" in {
    entry["version"] for entry in schema_migration_history(eng)
  }
  assert "0006_connector_capability_identity" in {
    entry["version"] for entry in schema_migration_history(eng)
  }


def test_chat_message_summary_migration_backfills_legacy_transcripts(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'chat-message-summary.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE chats ("
      "id VARCHAR(64) PRIMARY KEY, title VARCHAR(255), messages JSON, "
      "updated_at DATETIME)"
    ))
    conn.execute(text(
      "INSERT INTO chats (id, title, messages) VALUES "
      "('empty', 'Empty', '[]'), "
      "('spaced-empty', 'Spaced empty', '[ ]'), "
      "('started', 'Started', '[{\"role\": \"user\"}]')"
    ))

  run_migrations(eng)
  run_migrations(eng)

  columns = {item["name"] for item in inspect(eng).get_columns("chats")}
  with eng.connect() as conn:
    values = conn.execute(text(
      "SELECT id, has_messages FROM chats ORDER BY id"
    )).all()
  assert "has_messages" in columns
  assert values == [("empty", 0), ("spaced-empty", 0), ("started", 1)]
  assert "0007_chat_has_messages" in {
    row["version"] for row in schema_migration_history(eng)
  }


def test_chat_search_migration_replaces_runtime_schema_and_uses_one_docs_index(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'chat-search-schema.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps (id INTEGER PRIMARY KEY, name VARCHAR(255))"
    ))
    conn.execute(text(
      "CREATE TABLE chat_search_docs ("
      "id INTEGER PRIMARY KEY, chat_id TEXT, msg_idx INTEGER, text TEXT)"
    ))
    conn.execute(text(
      "CREATE INDEX chat_search_docs_chat ON chat_search_docs (chat_id)"
    ))
    conn.execute(text(
      "CREATE VIRTUAL TABLE chat_search_fts USING fts5("
      "text, content='chat_search_docs', content_rowid='id')"
    ))
    conn.execute(text(
      "CREATE TRIGGER chat_search_docs_ai "
      "AFTER INSERT ON chat_search_docs BEGIN "
      "INSERT INTO chat_search_fts(rowid, text) VALUES (new.id, new.text); "
      "END"
    ))
    conn.execute(text(
      "CREATE TRIGGER chat_search_docs_ad "
      "AFTER DELETE ON chat_search_docs BEGIN "
      "INSERT INTO chat_search_fts(chat_search_fts, rowid, text) "
      "VALUES ('delete', old.id, old.text); END"
    ))
    conn.execute(text(
      "CREATE TABLE chat_search_state ("
      "chat_id TEXT PRIMARY KEY, indexed_updated_at TEXT)"
    ))
    conn.execute(text(
      "CREATE TABLE chat_search_meta (key TEXT PRIMARY KEY, value TEXT)"
    ))
    conn.execute(text(
      "INSERT INTO chat_search_docs (chat_id, msg_idx, text) "
      "VALUES ('runtime-chat', 0, 'discarded derived prose')"
    ))

  run_migrations(eng)
  run_migrations(eng)

  inspector = inspect(eng)
  tables = set(inspector.get_table_names())
  columns = {
    column["name"] for column in inspector.get_columns("chat_search_docs")
  }
  indexes = inspector.get_indexes("chat_search_docs")
  with eng.connect() as conn:
    doc_count = conn.execute(text(
      "SELECT COUNT(*) FROM chat_search_docs"
    )).scalar_one()
    triggers = {
      row[0] for row in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND name LIKE 'chat_search_docs_%'"
      ))
    }
    plan = " ".join(
      row[-1] for row in conn.execute(text(
        "EXPLAIN QUERY PLAN SELECT id, msg_idx, ts, role, text "
        "FROM chat_search_docs WHERE chat_id = 'runtime-chat' "
        "ORDER BY msg_idx"
      ))
    )

  assert {"chat_search_docs", "chat_search_state", "chat_search_fts"} <= tables
  assert "chat_search_meta" not in tables
  assert columns == {"id", "chat_id", "msg_idx", "ts", "role", "text"}
  assert doc_count == 0
  assert triggers == {"chat_search_docs_ai", "chat_search_docs_ad"}
  assert [
    (index["name"], index["column_names"], index["unique"])
    for index in indexes
  ] == [(
    "ix_chat_search_docs_chat_message",
    ["chat_id", "msg_idx"],
    1,
  )]
  assert "USING INDEX ix_chat_search_docs_chat_message" in plan
  assert "0008_chat_search_documents" in {
    row["version"] for row in schema_migration_history(eng)
  }


def test_chat_search_migration_emits_plain_postgres_documents_without_fts():
  statements = []

  class RecordingConnection:
    dialect = SimpleNamespace(name="postgresql")

    def begin(self):
      return self

    def __enter__(self):
      return self

    def __exit__(self, *_args):
      return False

    def execute(self, statement):
      statements.append(str(statement))

  migrations._create_chat_search_tables(RecordingConnection())

  emitted = "\n".join(statements)
  assert "DROP TABLE IF EXISTS chat_search_docs" in emitted
  assert "DROP TABLE IF EXISTS chat_search_meta" in emitted
  assert "id BIGSERIAL PRIMARY KEY" in emitted
  assert "CREATE TABLE chat_search_state" in emitted
  assert "ts BIGINT" in emitted
  assert emitted.count("CREATE UNIQUE INDEX") == 1
  assert "ON chat_search_docs (chat_id, msg_idx)" in emitted
  assert "VIRTUAL TABLE" not in emitted
  assert "CREATE TRIGGER" not in emitted


def test_chat_run_root_migration_backfills_existing_physical_runs(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'run-root.db'}")
  applied_at = datetime(2026, 8, 1)
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, name VARCHAR(128), "
      "slug VARCHAR(128), source_dir VARCHAR(512))"
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
  started_ms = int(started_at.replace(tzinfo=UTC).timestamp() * 1000)
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


def test_goal_plan_migration_adds_snapshot_and_revision_to_existing_runs(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'goal-plan.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE chat_runs DROP COLUMN goal_plan_json"))
    conn.execute(text("ALTER TABLE chat_runs DROP COLUMN goal_plan_revision"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0014_chat_run_goal_plan"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, :at)"
      ), {"version": version, "at": datetime(2026, 8, 18)})

  run_migrations(eng)
  run_migrations(eng)

  columns = {column["name"]: column for column in inspect(eng).get_columns("chat_runs")}
  assert "goal_plan_json" in columns
  assert "goal_plan_revision" in columns
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0014_chat_run_goal_plan'"
    )).scalar_one() == 1


def test_goal_identity_migration_preserves_distinct_historical_roots_and_index(
  tmp_path,
):
  eng = create_engine(f"sqlite:///{tmp_path / 'goal-identity.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Chat(id="goal-chat", title="Goal", messages=[]))
    session.add_all([
      models.ChatRun(
        id="planned", root_run_id="planned", chat_id="goal-chat",
        status="interrupted", provider="codex", goal_objective="Ship",
        goal_plan_json={"version": 1, "tasks": []},
        started_at=datetime(2026, 8, 18, 10),
      ),
      models.ChatRun(
        id="recovered", root_run_id="recovered", chat_id="goal-chat",
        status="running", provider="codex", goal_objective="Ship",
        started_at=datetime(2026, 8, 18, 11),
      ),
    ])
    session.commit()
  with eng.begin() as conn:
    conn.execute(text("DROP INDEX ix_chat_runs_goal_id"))
    conn.execute(text("ALTER TABLE chat_runs DROP COLUMN goal_id"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0015_chat_run_goal_identity"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 18)})

  run_migrations(eng)
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT id, goal_id FROM chat_runs ORDER BY started_at"
    )).all()
  assert rows == [("planned", "planned"), ("recovered", "recovered")]
  assert any(
    index["name"] == "ix_chat_runs_goal_id"
    for index in inspect(eng).get_indexes("chat_runs")
  )


def test_goal_identity_index_repair_preserves_recorded_0015_data(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'goal-index-repair.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Chat(id="goal-chat", title="Goal", messages=[]))
    session.add(models.ChatRun(
      id="historical", root_run_id="historical", chat_id="goal-chat",
      status="completed", provider="codex", goal_objective="Ship",
      goal_id="preserve-existing-identity",
      started_at=datetime(2026, 8, 19),
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text("DROP INDEX ix_chat_runs_goal_id"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before(
      "0027_chat_run_goal_identity_index",
    ):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 19)})

  run_migrations(eng)
  run_migrations(eng)

  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT goal_id FROM chat_runs WHERE id = 'historical'"
    )).scalar_one() == "preserve-existing-identity"
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0027_chat_run_goal_identity_index'"
    )).scalar_one() == 1
  assert any(
    index["name"] == "ix_chat_runs_goal_id"
    for index in inspect(eng).get_indexes("chat_runs")
  )


def test_agent_coordination_migration_copies_legacy_project_mail_once(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'agent-coordination.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Project(
      id="project-1", name="Project", project_type="blank",
      root_path="projects/project-1", template_snapshot_json={},
    ))
    session.add_all([
      models.Chat(id="sender", title="Sender", messages=[], project_id="project-1"),
      models.Chat(id="receiver", title="Receiver", messages=[], project_id="project-1"),
    ])
    session.flush()
    session.add(models.ProjectAgentMessage(
      id="legacy-message", project_id="project-1", from_chat_id="sender",
      to_chat_id="receiver", body="Preserve this note.",
      created_at=datetime(2026, 8, 31),
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text("DROP TABLE agent_coordination_messages"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0028_agent_coordination_rooms"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 31)})

  run_migrations(eng)
  run_migrations(eng)

  assert {
    "project_agent_messages", "agent_coordination_messages",
  }.issubset(inspect(eng).get_table_names())
  assert {"send_id", "send_target_key"}.issubset({
    column["name"]
    for column in inspect(eng).get_columns("agent_coordination_messages")
  })
  assert {
    "ix_agent_coordination_room_created",
    "ix_agent_coordination_messages_from_chat_id",
    "ix_agent_coordination_messages_to_chat_id",
  }.issubset({
    index["name"]
    for index in inspect(eng).get_indexes("agent_coordination_messages")
  })
  retry_index = next(
    index
    for index in inspect(eng).get_indexes("agent_coordination_messages")
    if index["name"] == "uq_agent_coordination_run_send_target"
  )
  assert bool(retry_index["unique"]) is True
  assert retry_index["column_names"] == [
    "from_run_id", "send_id", "send_target_key",
  ]
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT id, room_kind, room_id, from_chat_id, to_chat_id, kind, body "
      "FROM agent_coordination_messages"
    )).mappings().all()
  assert rows == [{
    "id": "legacy-message",
    "room_kind": "project",
    "room_id": "project-1",
    "from_chat_id": "sender",
    "to_chat_id": "receiver",
    "kind": "note",
    "body": "Preserve this note.",
  }]


def test_goal_dismissal_migration_adds_nullable_chat_pointer(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'goal-dismissal.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE chats DROP COLUMN dismissed_goal_id"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0024_chat_goal_dismissal"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 22)})

  run_migrations(eng)

  columns = {column["name"] for column in inspect(eng).get_columns("chats")}
  assert "dismissed_goal_id" in columns
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0024_chat_goal_dismissal'"
    )).scalar_one() == 1


def test_project_color_migration_adds_nullable_column_once(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'project-color.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE projects DROP COLUMN color"))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0023_project_color"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 25)})

  run_migrations(eng)
  run_migrations(eng)

  columns = {column["name"] for column in inspect(eng).get_columns("projects")}
  assert "color" in columns
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0023_project_color'"
    )).scalar_one() == 1


def test_active_chat_model_migrations_pin_lazy_drafts_and_scoped_rows(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  shared = data_dir / "shared"
  shared.mkdir(parents=True)
  (shared / "agent-settings.json").write_text(json.dumps({
    "model": "gpt-5.6-sol", "provider": "codex",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'active-models.db'}")
  models.Base.metadata.create_all(eng)
  transcript = [
    {"role": "user", "content": "review"},
    {"role": "assistant", "content": "done"},
  ]
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner", hashed_password="hash", provider="codex",
    ))
    session.add_all([
      models.Chat(
        id="actual-claude", title="Autopilot", provider="claude",
        messages=transcript, agent_settings_json={"drawer_hidden": False},
      ),
      models.Chat(
        id="pristine-owner-a", title="New chat", provider="claude",
        messages=[], agent_settings_json=None,
      ),
      models.Chat(
        id="pristine-owner-b", title="New chat", provider="claude",
        messages=[], agent_settings_json={"effort": "high"},
      ),
      models.Chat(
        id="empty-app", title="Panel", provider="claude", messages=[],
        created_by_app_id=7, agent_settings_json={"system_prompt": "Panel"},
      ),
      models.Chat(
        id="hidden-internal", title="New chat", provider="claude", messages=[],
        agent_settings_json={"drawer_hidden": True},
      ),
      models.Chat(
        id="live-owner", title="New chat", provider="claude", messages=[],
        live_assistant={"role": "assistant", "content": "working"},
        active_assistant_message_id="assistant-live",
        agent_settings_json=None,
      ),
      models.Chat(
        id="question-owner", title="New chat", provider="claude", messages=[],
        pending_question_id="question-1", agent_settings_json=None,
      ),
      models.Chat(
        id="linked-owner", title="New chat", provider="claude", messages=[],
        agent_settings_json=None,
      ),
      models.Chat(
        id="snapshotted-owner", title="New chat", provider="claude", messages=[],
        system_prompt_snapshot_id="prompt-snapshot", agent_settings_json=None,
      ),
      models.Chat(
        id="project-chat", title="New chat", provider="claude", messages=[],
        project_id="project-1", agent_settings_json=None,
      ),
      models.Chat(
        id="legacy-codex", title="Old", provider="codex",
        messages=transcript, agent_settings_json={"effort": "high"},
      ),
      models.Chat(
        id="explicit", title="Pinned", provider="codex", messages=transcript,
        agent_settings_json={"model": "gpt-5.5"},
      ),
      models.Chat(
        id="deleted", title="Deleted", provider="codex", messages=transcript,
        agent_settings_json=None, deleted_at=datetime(2026, 8, 29),
      ),
      models.Chat(
        id="malformed-settings", title="Broken", provider="claude",
        messages=transcript, agent_settings_json="{not-json",
      ),
    ])
    session.add(models.SystemPromptSnapshot(
      id="prompt-snapshot", content="frozen prompt",
    ))
    session.add(models.ChatSessionLink(
      provider="claude", session_id="historical-session", chat_id="linked-owner",
    ))
    session.add(models.ChatRun(
      id="actual-run", chat_id="actual-claude", status="completed",
      provider="claude", ended_at=datetime(2026, 8, 29),
      usage_json={
        "provider_model_usage": {
          "claude-haiku-4-5-20251001": {
            "inputTokens": 100, "outputTokens": 2,
          },
          "claude-opus-5[1m]": {
            "inputTokens": 10_000, "outputTokens": 500,
          },
        },
      },
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0037_explicit_active_chat_models"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 29)})

  run_migrations(eng)
  with Session(eng) as session:
    actual = session.get(models.Chat, "actual-claude")
    pristine_a = session.get(models.Chat, "pristine-owner-a")
    pristine_b = session.get(models.Chat, "pristine-owner-b")
    empty_app = session.get(models.Chat, "empty-app")
    hidden = session.get(models.Chat, "hidden-internal")
    live = session.get(models.Chat, "live-owner")
    question = session.get(models.Chat, "question-owner")
    linked = session.get(models.Chat, "linked-owner")
    snapshotted = session.get(models.Chat, "snapshotted-owner")
    project = session.get(models.Chat, "project-chat")
    legacy = session.get(models.Chat, "legacy-codex")
    explicit = session.get(models.Chat, "explicit")
    deleted = session.get(models.Chat, "deleted")
    malformed = session.get(models.Chat, "malformed-settings")
    assert actual.agent_settings_json == {
      "drawer_hidden": False, "model": "claude-opus-5",
    }
    assert pristine_a.provider == "claude"
    assert pristine_a.agent_settings_json == {"model": "claude-opus-4-8"}
    assert pristine_b.provider == "claude"
    assert pristine_b.agent_settings_json == {
      "effort": "high", "model": "claude-opus-4-8",
    }
    assert empty_app.agent_settings_json == {
      "system_prompt": "Panel", "model": "claude-opus-4-8",
    }
    assert hidden.agent_settings_json == {
      "drawer_hidden": True, "model": "claude-opus-4-8",
    }
    assert live.agent_settings_json == {"model": "claude-opus-4-8"}
    assert question.agent_settings_json == {"model": "claude-opus-4-8"}
    assert linked.agent_settings_json == {"model": "claude-opus-4-8"}
    assert snapshotted.agent_settings_json == {"model": "claude-opus-4-8"}
    assert project.agent_settings_json == {"model": "claude-opus-4-8"}
    assert legacy.agent_settings_json == {
      "effort": "high", "model": "gpt-5.6-sol",
    }
    assert explicit.agent_settings_json == {"model": "gpt-5.5"}
    assert deleted.agent_settings_json is None
    assert malformed.agent_settings_json == "{not-json"
    assert {
      row.id: row.provider for row in session.query(models.Chat).all()
    } == {
      "actual-claude": "claude",
      "pristine-owner-a": "claude",
      "pristine-owner-b": "claude",
      "empty-app": "claude",
      "hidden-internal": "claude",
      "live-owner": "claude",
      "question-owner": "claude",
      "linked-owner": "claude",
      "snapshotted-owner": "claude",
      "project-chat": "claude",
      "legacy-codex": "codex",
      "explicit": "codex",
      "deleted": "codex",
      "malformed-settings": "claude",
    }

  with eng.begin() as conn:
    conn.execute(text(
      "DELETE FROM schema_migrations "
      "WHERE version = '0037_explicit_active_chat_models'"
    ))
  run_migrations(eng)
  with Session(eng) as session:
    assert session.get(models.Chat, "actual-claude").agent_settings_json == {
      "drawer_hidden": False, "model": "claude-opus-5",
    }
    assert session.get(models.Chat, "pristine-owner-a").agent_settings_json == {
      "model": "claude-opus-4-8",
    }


def test_active_chat_model_migration_never_reassigns_queued_provider_state(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  (data_dir / "shared").mkdir(parents=True)
  (data_dir / "shared" / "agent-settings.json").write_text(json.dumps({
    "model": "gpt-5.6-sol", "provider": "codex",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'queued-chat.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner", hashed_password="hash", provider="codex",
    ))
    session.add(models.Chat(
      id="queued-claude", title="New chat", provider="claude", messages=[],
      pending_messages=[{"role": "user", "content": "queued"}],
      session_id="claude-session", agent_settings_json=None,
    ))
    session.add(models.ChatRun(
      id="queued-claude-run", chat_id="queued-claude", status="completed",
      provider="claude", ended_at=datetime(2026, 8, 29),
      usage_json={
        "provider_model_usage": {
          "claude-opus-4-8": {
            "inputTokens": 400, "outputTokens": 50,
          },
        },
      },
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0037_explicit_active_chat_models"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 29)})

  run_migrations(eng)

  with Session(eng) as session:
    queued = session.get(models.Chat, "queued-claude")
    assert queued.provider == "claude"
    assert queued.agent_settings_json == {"model": "claude-opus-4-8"}
    assert queued.session_id == "claude-session"
    assert queued.pending_messages == [{"role": "user", "content": "queued"}]


def test_active_chat_model_migration_honors_unknown_picker_model_provider_pair(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  (data_dir / "shared").mkdir(parents=True)
  (data_dir / "shared" / "agent-settings.json").write_text(json.dumps({
    "model": "future-catalog-model", "provider": "codex",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'unknown-model.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner", hashed_password="hash", provider="claude",
    ))
    session.add(models.Chat(
      id="codex-app-chat", title="Panel", provider="codex", messages=[],
      created_by_app_id=7, agent_settings_json=None,
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0037_explicit_active_chat_models"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 29)})

  run_migrations(eng)

  with Session(eng) as session:
    chat = session.get(models.Chat, "codex-app-chat")
    assert chat.provider == "codex"
    assert chat.agent_settings_json == {"model": "future-catalog-model"}


def test_post_explicit_model_repair_pins_all_later_gaps_without_provider_handoff(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  (data_dir / "shared").mkdir(parents=True)
  (data_dir / "shared" / "agent-settings.json").write_text(json.dumps({
    "model": "gpt-5.6-sol", "provider": "codex",
  }))
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'post-explicit-model-gaps.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner", hashed_password="hash", provider="codex",
    ))
    session.add_all([
      models.Chat(
        id="lazy-claude-a", title="New chat", provider="claude",
        messages=[], agent_settings_json=None,
      ),
      models.Chat(
        id="lazy-claude-b", title="New chat", provider="claude",
        messages=[], agent_settings_json={"effort": "high"},
      ),
      models.Chat(
        id="queued-claude", title="New chat", provider="claude", messages=[],
        pending_messages=[{"role": "user", "content": "queued"}],
        session_id="claude-session", agent_settings_json=None,
      ),
      models.Chat(
        id="actual-claude", title="Used", provider="claude",
        messages=[{"role": "user", "content": "review"}],
        agent_settings_json={"drawer_hidden": False},
      ),
      models.Chat(
        id="codex-app", title="Panel", provider="codex", messages=[],
        created_by_app_id=7, agent_settings_json={"system_prompt": "Panel"},
      ),
      models.Chat(
        id="explicit", title="Pinned", provider="codex", messages=[],
        agent_settings_json={"model": "gpt-5.5"},
      ),
      models.Chat(
        id="deleted", title="Deleted", provider="codex", messages=[],
        agent_settings_json=None, deleted_at=datetime(2026, 8, 29),
      ),
    ])
    session.add(models.ChatRun(
      id="actual-run", chat_id="actual-claude", status="completed",
      provider="claude", ended_at=datetime(2026, 8, 29),
      usage_json={
        "provider_model_usage": {
          "claude-opus-5[1m]": {
            "inputTokens": 20_000, "outputTokens": 500,
          },
        },
      },
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0038_repair_active_chat_model_gaps"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 29)})

  run_migrations(eng)

  with Session(eng) as session:
    lazy_a = session.get(models.Chat, "lazy-claude-a")
    lazy_b = session.get(models.Chat, "lazy-claude-b")
    queued = session.get(models.Chat, "queued-claude")
    actual = session.get(models.Chat, "actual-claude")
    app_chat = session.get(models.Chat, "codex-app")
    explicit = session.get(models.Chat, "explicit")
    deleted = session.get(models.Chat, "deleted")
    assert lazy_a.provider == "claude"
    assert lazy_a.agent_settings_json == {"model": "claude-opus-4-8"}
    assert lazy_b.provider == "claude"
    assert lazy_b.agent_settings_json == {
      "effort": "high", "model": "claude-opus-4-8",
    }
    assert queued.provider == "claude"
    assert queued.agent_settings_json == {"model": "claude-opus-4-8"}
    assert queued.pending_messages == [{"role": "user", "content": "queued"}]
    assert queued.session_id == "claude-session"
    assert actual.agent_settings_json == {
      "drawer_hidden": False, "model": "claude-opus-5",
    }
    assert app_chat.agent_settings_json == {
      "system_prompt": "Panel", "model": "gpt-5.6-sol",
    }
    assert explicit.agent_settings_json == {"model": "gpt-5.5"}
    assert deleted.agent_settings_json is None
    assert {
      row.id: row.provider for row in session.query(models.Chat).all()
    } == {
      "lazy-claude-a": "claude",
      "lazy-claude-b": "claude",
      "queued-claude": "claude",
      "actual-claude": "claude",
      "codex-app": "codex",
      "explicit": "codex",
      "deleted": "codex",
    }

  run_migrations(eng)
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0038_repair_active_chat_model_gaps'"
    )).scalar_one() == 1


def test_post_explicit_model_repair_preserves_only_genuine_first_install_chat(
  tmp_path, monkeypatch,
):
  data_dir = tmp_path / "data"
  (data_dir / "shared").mkdir(parents=True)
  (data_dir / "shared" / "agent-settings.json").write_text("{}")
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'post-explicit-model-first-chat.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    session.add(models.Owner(
      username="owner", hashed_password="hash", provider="claude",
    ))
    session.add(models.Chat(
      id="first-chat", title="New chat", provider="claude",
      messages=[], agent_settings_json=None,
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before("0038_repair_active_chat_model_gaps"):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 29)})

  run_migrations(eng)

  with Session(eng) as session:
    assert session.get(models.Chat, "first-chat").agent_settings_json is None


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
    migrations,
    "_SCHEMA_MIGRATIONS",
    (("9000_retry_contract", fail_once),),
  )

  with pytest.raises(RuntimeError, match="interrupted migration"):
    run_migrations(eng)
  assert schema_migration_history(eng) == []
  with pytest.raises(RuntimeError, match="interrupted migration"):
    run_migrations(eng)
  assert attempts == 2


def test_result_incorporation_migration_preserves_unknown_history(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'result-incorporation.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as session:
    app = models.App(
      slug="incorporation-migration", source_dir="/tmp/incorporation",
      name="Incorporation", description="", jsx_source="",
    )
    session.add(app)
    session.flush()
    session.add_all([
      models.Chat(id="incorporation-parent", messages=[]),
      models.Chat(
        id="incorporation-child", messages=[], created_by_app_id=app.id,
      ),
    ])
    session.flush()
    session.add(models.Delegation(
      id="incorporation-history", app_id=app.id,
      parent_chat_id="incorporation-parent", parent_root_run_id="root",
      task_key="history", child_chat_id="incorporation-child",
      provider="claude", scope="read", cwd="/tmp",
      prompt_sha256="0" * 64, notify_parent_on_complete=False,
      parent_woken_at=datetime(2026, 9, 9),
    ))
    session.commit()
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE delegations DROP COLUMN result_incorporated_at"
    ))
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version in _migration_versions_before(
      "0048_delegation_result_incorporation",
    ):
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 9, 9)})

  run_migrations(eng)
  run_migrations(eng)

  column = next(
    item for item in inspect(eng).get_columns("delegations")
    if item["name"] == "result_incorporated_at"
  )
  assert column["nullable"] is True
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT result_incorporated_at FROM delegations "
      "WHERE id = 'incorporation-history'"
    )).scalar_one() is None
    assert conn.execute(text(
      "SELECT COUNT(*) FROM schema_migrations "
      "WHERE version = '0048_delegation_result_incorporation'"
    )).scalar_one() == 1
