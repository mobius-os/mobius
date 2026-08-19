import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import String, create_engine, inspect, text
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


def _migration_guard():
  script = Path(__file__).parents[1] / "scripts" / "check-schema-migrations.py"
  spec = importlib.util.spec_from_file_location("migration_guard", script)
  assert spec and spec.loader
  guard = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = guard
  spec.loader.exec_module(guard)
  return guard


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

  async def fake_compile(_app_id, source, *, out_path, source_path):
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
  ]
  assert second == first


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
    for version, _migration in migrations._SCHEMA_MIGRATIONS[:-3]:
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
    for version, _migration in migrations._SCHEMA_MIGRATIONS[:-3]:
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
  assert public_access == {"network": []}
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
    for version, _migration in migrations._SCHEMA_MIGRATIONS[:-2]:
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


def test_goal_identity_migration_backfills_plan_and_recovery_runs(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'goal-identity.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("DROP INDEX ix_chat_runs_goal_id"))
    conn.execute(text("ALTER TABLE chat_runs DROP COLUMN goal_id"))
    conn.execute(text(
      "INSERT INTO chats (id, title, title_locked, messages, pending_messages, "
      "uploads, provider, created_at, updated_at) "
      "VALUES ('c1', 'Goal', 0, '[]', '[]', '[]', 'codex', :at, :at)"
    ), {"at": datetime(2026, 8, 18)})
    conn.execute(text(
      "INSERT INTO chat_runs "
      "(id, root_run_id, chat_id, status, provider, goal_objective, "
      "goal_plan_json, goal_plan_revision, started_at) VALUES "
      "('planned', 'planned', 'c1', 'interrupted', 'codex', 'Ship', "
      ":plan, 1, :first), "
      "('recovered', 'recovered', 'c1', 'running', 'codex', 'Ship', "
      "NULL, 0, :second)"
    ), {
      "first": datetime(2026, 8, 18, 10),
      "second": datetime(2026, 8, 18, 11),
      "plan": json.dumps({"version": 1, "tasks": []}),
    })
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    ))
    for version, _migration in migrations._SCHEMA_MIGRATIONS[:-1]:
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :at)"
      ), {"v": version, "at": datetime(2026, 8, 18)})

  run_migrations(eng)
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT id, goal_id FROM chat_runs ORDER BY started_at"
    )).all()
  assert rows == [("planned", "planned"), ("recovered", "planned")]


def test_published_schema_migration_history_is_unique_ordered_and_immutable():
  """Published migrations are history; current work always appends."""
  script = Path(__file__).parents[1] / "scripts" / "check-schema-migrations.py"
  completed = subprocess.run(
    [sys.executable, str(script)],
    text=True,
    capture_output=True,
    check=False,
  )
  assert completed.returncode == 0, completed.stderr
  assert "append-only migrations verified" in completed.stdout


def test_published_history_cannot_be_rehashed_in_place():
  """Changing code and its checked-in hash together still rewrites history."""
  guard = _migration_guard()

  published = {"0001_initial": "old", "0002_add_field": "same"}
  assert guard.append_only_error(published, {
    "0001_initial": "new",
    "0002_add_field": "same",
  }) == "published migration 0001_initial changed"
  assert guard.append_only_error(published, {
    **published,
    "0003_next": "added",
  }) is None


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
