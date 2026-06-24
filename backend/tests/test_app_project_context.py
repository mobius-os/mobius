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
from app.chat import _build_app_context

_DATA_DIR = os.environ.get("DATA_DIR", "/tmp")


def _app_chat(db, *, project_id=None):
  app = models.App(
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
