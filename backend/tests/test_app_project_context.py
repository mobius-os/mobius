"""Per-project scoping for app-attributed chats (feature 135).

When an app opens a chat scoped to ONE of its projects, it stores a slug
`project_id` in the chat's agent_settings_json. `_build_app_context` reads it
and points APP_STORAGE_DIR at projects/<project_id>/ (so files/, files-index,
etc. resolve under that project, not the shared app root) and exposes
APP_PROJECT_ID + an "Active project" context line. These cover the scoping and
its strict slug validation — a project_id is used as a path component.
"""

import os

from app import models
from app.chat_context import _build_app_context

_DATA_DIR = os.environ.get("DATA_DIR", "/tmp")


def _app_chat(db, *, project_id=None):
  app = models.App(
    slug="test-app-project-context-20",
    source_dir="/tmp/mobius-tests/test-app-project-context-20",
    name="studio", description="t",
    jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  settings = {"project_id": project_id} if project_id else None
  chat = models.Chat(
    id=f"proj-chat-{project_id or 'none'}-{app.id}",
    title="p", messages=[],
    created_by_app_id=app.id,
    agent_settings_json=settings,
  )
  db.add(chat)
  db.commit()
  return app, chat


def test_project_scopes_storage_dir_and_env(db):
  app, chat = _app_chat(db, project_id="alpha-1")
  block, env = _build_app_context(db, chat.id, _DATA_DIR)
  assert block is not None
  assert env["APP_STORAGE_DIR"].endswith(f"apps/{app.id}/projects/alpha-1")
  assert env["APP_PROJECT_ID"] == "alpha-1"
  assert "Active project: alpha-1" in block
  assert "projects/alpha-1/" in block


def test_no_project_uses_app_root(db):
  app, chat = _app_chat(db, project_id=None)
  block, env = _build_app_context(db, chat.id, _DATA_DIR)
  assert block is not None
  assert env["APP_STORAGE_DIR"].endswith(f"apps/{app.id}")
  assert not env["APP_STORAGE_DIR"].rstrip("/").endswith("projects")
  assert "APP_PROJECT_ID" not in env
  assert "Active project" not in block


def test_malformed_project_id_rejected(db):
  # A traversal-shaped project_id must never become a path component.
  app, chat = _app_chat(db, project_id=None)
  chat.agent_settings_json = {"project_id": "../../etc"}
  db.add(chat)
  db.commit()
  block, env = _build_app_context(db, chat.id, _DATA_DIR)
  assert block is not None
  assert env["APP_STORAGE_DIR"].endswith(f"apps/{app.id}")
  assert "APP_PROJECT_ID" not in env
  assert ".." not in env["APP_STORAGE_DIR"]


def test_non_app_chat_has_no_context(db):
  chat = models.Chat(
    id="owner-proj-chat", title="x", messages=[],
    agent_settings_json={"project_id": "alpha"},
  )
  db.add(chat)
  db.commit()
  block, env = _build_app_context(db, chat.id, _DATA_DIR)
  assert block is None
  assert env == {}


def test_build_chat_with_one_linked_app_gets_exact_identity(db):
  chat = models.Chat(id="builder-one", title="build", messages=[])
  db.add(chat)
  db.commit()
  app = models.App(
    name="Duplicate name", description="", jsx_source="",
    slug="duplicate-name-2", source_dir="/data/apps/duplicate-name-2",
    chat_id=chat.id,
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  block, env = _build_app_context(db, chat.id, _DATA_DIR)

  assert block is not None
  assert "App names are not unique" in block
  assert f'"app_id":{app.id}' in block
  assert env["APP_ID"] == str(app.id)
  assert env["APP_SOURCE_DIR"] == "/data/apps/duplicate-name-2"
  assert env["APP_STORAGE_DIR"].endswith(f"/apps/{app.id}")
  assert env["CHAT_APPS_JSON"] in block


def test_build_chat_with_multiple_apps_never_guesses_one_app(db):
  chat = models.Chat(id="builder-many", title="build", messages=[])
  db.add(chat)
  db.commit()
  apps = [
    models.App(
      name="Same", description="", jsx_source="", slug=f"same-{index}",
      source_dir=f"/data/apps/same-{index}", chat_id=chat.id,
    )
    for index in (1, 2)
  ]
  db.add_all(apps)
  db.commit()

  block, env = _build_app_context(db, chat.id, _DATA_DIR)

  assert block is not None
  assert env.keys() == {"CHAT_APPS_JSON"}
  assert all(f'"app_id":{app.id}' in block for app in apps)


def test_deleted_linked_app_is_not_injected(db):
  from datetime import UTC, datetime

  chat = models.Chat(id="builder-deleted", title="build", messages=[])
  db.add(chat)
  db.commit()
  db.add(models.App(
    name="Gone", description="", jsx_source="", slug="gone",
    source_dir="/data/apps/gone", chat_id=chat.id,
    deleted_at=datetime.now(UTC).replace(tzinfo=None),
  ))
  db.commit()

  block, env = _build_app_context(db, chat.id, _DATA_DIR)

  assert block is None
  assert env == {}
