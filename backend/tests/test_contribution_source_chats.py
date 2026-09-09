"""Project source discovery exposes names, never a broader chat read grant."""
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import event

from app import auth as tokens, models
from app.chat_writer import ReplaceTranscript, get_writer
from app.config import get_settings
from app.database import engine


def patch(path, body="+secret source contents"):
  return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n{body}\n"


def chat(db, chat_id, paths=(), stamp=1770000000000, **extra):
  deleted_at = extra.pop("deleted_at", None)
  row = models.Chat(id=chat_id, title=f"Title {chat_id}", **extra)
  db.add(row)
  db.commit()
  blocks = [{"type": "tool", "tool": "Edit", "tool_use_id": f"{chat_id}-{i}",
             "edit_preview": {"diff": patch(path)}} for i, path in enumerate(paths)]
  messages = [{"role": "user", "content": "PRIVATE TRANSCRIPT"},
              {"role": "assistant", "ts": stamp, "blocks": blocks}]
  assert get_writer().submit(ReplaceTranscript(chat_id=chat_id, messages=messages)).result(30)
  if deleted_at is not None:
    row.deleted_at = deleted_at
    db.commit()
  return row


@pytest.fixture
def setup(db, owner_token):
  app = models.App(id=80, name="Contribute", slug="contribute", source_dir="/data/apps/contribute",
                   github_access=True, token_nonce="contribute-nonce")
  source = models.App(id=81, name="Garden", slug="garden", source_dir="/data/apps/garden")
  other = models.App(id=82, name="Other", slug="other", source_dir="/data/apps/other",
                     github_access=True, token_nonce="other-nonce")
  db.add_all([app, source, other]); db.commit()
  owner = db.query(models.Owner).one()
  def headers(app_id=80):
    row = db.get(models.App, app_id)
    return {"Authorization": "Bearer " + tokens.create_app_token(
      app_id, owner.username, owner.token_epoch, row.token_nonce,
    )}
  return headers


def read(client, headers, project="app:81", app_id=80):
  return client.get(f"/api/github/contributions/{app_id}/source-chats",
                    params={"project_key": project}, headers=headers)


def test_discovers_only_declared_project_edits_newest_first(client, db, setup):
  chat(db, "old", ["/data/apps/garden/index.jsx"], 1770000000000)
  chat(db, "new", ["/data/apps/garden/flower.js", "/data/platform/README.md"], 1770000001000)
  chat(db, "prefix", ["/data/apps/garden-other/index.jsx"])
  chat(db, "private", ["/data/shared/memory/note.md"])
  chat(db, "deleted", ["/data/apps/garden/index.jsx"], deleted_at=datetime(2026, 1, 1))
  chat(db, "plain")
  result = read(client, setup())
  assert result.status_code == 200, result.text
  assert result.json() == {"chats": [
    {"chat_id": "new", "title": "Title new", "last_edit_at": "2026-02-02T02:40:01Z"},
    {"chat_id": "old", "title": "Title old", "last_edit_at": "2026-02-02T02:40:00Z"},
  ]}
  assert "PRIVATE" not in result.text and "secret source" not in result.text
  assert "/data/" not in result.text and "diff" not in result.text
  assert [c["chat_id"] for c in read(client, setup(), "platform").json()["chats"]] == ["new"]


def test_complete_sidecar_headers_own_membership_not_diff_body(client, db, setup):
  row = chat(db, "sidecar")
  get_writer().submit(ReplaceTranscript(chat_id=row.id, messages=[{
    "role": "assistant", "ts": 1770000000000, "blocks": [{"type": "tool", "tool_use_id": "full",
      "edit_preview": {"diff": patch("/data/apps/other/a.js", "+/data/apps/garden/decoy.js"),
                       "full_id": "full", "truncated": True}}],
  }])).result(30)
  db.add(models.ToolOutput(chat_id=row.id, tool_use_id="full", output=patch("/data/apps/garden/real.js")))
  db.commit()
  result = read(client, setup()).json()
  assert [c["chat_id"] for c in result["chats"]] == ["sidecar"]
  assert read(client, setup(), "app:82").json() == {"chats": []}


def delegation(db, child, parent, **extra):
  row = models.Delegation(id=f"delegation-{child}", app_id=80, parent_chat_id=parent,
    child_chat_id=child, parent_root_run_id="parent-run", task_key=child,
    provider="codex", model="test", scope="read", cwd="/data",
    prompt_sha256="a" * 64, startup_prompt="Test task",
    **extra)
  db.add(row); db.commit()


def test_delegated_edits_resolve_to_source_home_and_deleted_homes_stay_hidden(client, db, setup):
  chat(db, "parent")
  chat(db, "child", ["/data/apps/garden/file.js"])
  delegation(db, "child", "parent")
  assert [c["chat_id"] for c in read(client, setup()).json()["chats"]] == ["parent"]
  db.get(models.Chat, "parent").deleted_at = datetime(2026, 1, 1)
  db.commit()
  assert read(client, setup()).json() == {"chats": []}


def test_server_source_work_provenance_survives_without_transcript_edits(client, db, setup):
  chat(db, "source"); chat(db, "worker")
  delegation(db, "worker", "source", source_work_context_app_id=80,
    source_work_envelope={"project_roots": ["/data/apps/garden"]})
  assert read(client, setup()).json() == {"chats": [
    {"chat_id": "source", "title": "Title source", "last_edit_at": None},
  ]}
  assert read(client, setup(82), app_id=82).json() == {"chats": []}


def test_app_editable_ledger_cannot_authorize_unrelated_chat_titles(client, db, setup):
  import json
  chat(db, "unrelated")
  root = Path(get_settings().data_dir) / "apps/80/contributions"
  root.mkdir(parents=True, exist_ok=True)
  (root / "fabricated.json").write_text(json.dumps({"id": "fabricated", "type": "pr",
    "status": "prepared", "chat_id": "unrelated", "plan": {"source_repo_path": "/data/apps/garden"}}))
  assert read(client, setup()).json() == {"chats": []}


@pytest.mark.parametrize("project", ["/data/platform", "app:../81", "app:081", "repo:owner/name", "app:9999"])
def test_caller_paths_and_unknown_projects_are_not_accepted(client, setup, project):
  assert read(client, setup(), project).status_code == 404


def test_missing_permission_and_foreign_contribution_store_fail_closed(client, db, setup):
  assert read(client, setup(82)).status_code == 403
  db.get(models.App, 80).github_access = False; db.commit()
  assert read(client, setup()).status_code == 403


def test_discovery_does_not_hydrate_unrelated_chat_payloads(client, db, setup):
  chat(db, "plain")
  selected = []
  def capture(conn, cursor, statement, parameters, context, executemany):
    if statement.lstrip().startswith("SELECT") and "chats.messages" in statement:
      selected.append(statement)
  event.listen(engine, "before_cursor_execute", capture)
  try:
    assert read(client, setup()).json() == {"chats": []}
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  assert len(selected) == 1
  assert "LIKE" in selected[0] and "chat_live_assistants.snapshot" in selected[0]
  assert "chats.pending_messages" not in selected[0]


def test_discovery_includes_live_edit_without_historical_preview(client, db, setup):
  from app.chat_writer import update_live_assistant
  row = chat(db, "live-only")
  assert update_live_assistant(db, row.id, {
    "id": "live-edit", "role": "assistant", "ts": 1770000000000,
    "blocks": [{"type": "tool", "tool_use_id": "live-tool",
                "edit_preview": {"diff": patch("/data/apps/garden/live.js")}}],
  })
  assert [c["chat_id"] for c in read(client, setup()).json()["chats"]] == ["live-only"]
