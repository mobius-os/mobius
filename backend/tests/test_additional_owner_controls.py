"""Regression coverage for owner controls omitted from the lifecycle inventory."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app import app_git, auth, models
from app.config import get_settings
from app.delegations import RunPolicy, delegation_execution_token


@pytest.fixture
def controls(db, owner_token, tmp_path):
  """Mint owner, app, top-level, and live delegated identities for one app."""
  owner = db.query(models.Owner).one()
  source_dir = Path(get_settings().data_dir) / "apps" / "owner-controls"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")

  app = models.App(
    name="Owner controls", description="", slug="owner-controls",
    source_dir=str(source_dir), jsx_source="export default () => null",
    token_nonce="owner-controls-nonce", manage_skills=True,
  )
  db.add(app)
  db.flush()
  app_git.ensure_repo(source_dir)
  app_git.commit_local(source_dir, "Accept owner-control fixture")
  app.source_commit = app_git.head_sha(source_dir, app_git.LOCAL_BRANCH)
  from app.applied_app_runtime import prepare_runtime, publish_runtime
  publish_runtime(app, prepare_runtime(source_dir, app.source_commit))

  chats = {
    name: models.Chat(
      id=f"owner-controls-{name}", title=name, messages=[],
      created_by_app_id=app.id if name == "child" else None,
    )
    for name in ("child", "parent", "top-level")
  }
  db.add_all(chats.values())
  db.add_all([
    models.Delegation(
      id="owner-controls-delegation", app_id=app.id,
      parent_chat_id=chats["parent"].id,
      parent_root_run_id="owner-controls-parent-root",
      task_key="owner-controls", child_chat_id=chats["child"].id,
      provider="codex", model=None, effort=None, scope="write", cwd="/data",
      prompt_sha256=hashlib.sha256(b"owner controls").hexdigest(),
    ),
    models.ChatRun(
      id="owner-controls-child-run", root_run_id="owner-controls-child-run",
      chat_id=chats["child"].id, status="running", provider="codex",
    ),
    models.ChatRun(
      id="owner-controls-parent-run", root_run_id="owner-controls-parent-root",
      chat_id=chats["parent"].id, status="running", provider="codex",
    ),
    models.ChatRun(
      id="owner-controls-top-run", root_run_id="owner-controls-top-run",
      chat_id=chats["top-level"].id, status="running", provider="codex",
    ),
  ])
  db.commit()

  delegated = delegation_execution_token(db, RunPolicy(
    delegation_id="owner-controls-delegation", app_id=app.id,
    provider="codex", model=None, effort=None, scope="write", cwd="/data",
  ), run_id="owner-controls-child-run")
  assert delegated is not None
  top_level = auth.create_agent_token(
    chats["top-level"].id, owner.username, owner.token_epoch,
    run_id="owner-controls-top-run",
  )
  child_agent = auth.create_agent_token(
    chats["child"].id, owner.username, owner.token_epoch,
    run_id="owner-controls-child-run",
  )
  app_token = auth.create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
  )
  return {
    "owner": {"Authorization": f"Bearer {owner_token}"},
    "delegated": {"Authorization": f"Bearer {delegated}"},
    "top_level": {"Authorization": f"Bearer {top_level}"},
    "child_agent": {"Authorization": f"Bearer {child_agent}"},
    "app": {"Authorization": f"Bearer {app_token}"},
    "app_row": app,
    "chat_ids": {name: chat.id for name, chat in chats.items()},
  }


def test_skills_owner_control_rejects_delegation_but_keeps_skills_app(
  client, controls, monkeypatch,
):
  from app.routes import skills as skills_routes

  async def fake_fetch(*_args, **_kwargs):
    return b"# controlled skill\n"

  monkeypatch.setattr(skills_routes.install, "_http_get", fake_fetch)
  install = lambda name, headers: client.post("/api/skills/install", headers=headers, json={
    "url": f"https://skills.invalid/{name}.md", "name": name,
  })

  assert install("child-skill", controls["delegated"]).status_code == 403
  assert install("remove-me", controls["app"]).status_code == 201
  blocked_delete = client.delete(
    "/api/skills/remove-me", headers=controls["delegated"],
  )
  assert blocked_delete.status_code == 403
  assert client.delete(
    "/api/skills/remove-me", headers=controls["top_level"],
  ).status_code == 200
  assert install("owner-skill", controls["owner"]).status_code == 201


def test_schedule_owner_controls_reject_delegation_but_keep_exact_app(
  client, controls, monkeypatch,
):
  from app import app_cron, app_jobs

  launches, schedules = [], []
  monkeypatch.setattr(
    app_jobs, "launch_app_job",
    lambda app_id, job_path, source_dir: launches.append((app_id, job_path, source_dir)),
  )
  monkeypatch.setattr(
    app_cron, "register_cron",
    lambda slug, cron, job_path, app_id=None, **kwargs: schedules.append(
      (slug, cron, job_path, app_id, kwargs),
    ),
  )
  app_id = controls["app_row"].id
  update = {"cron": "15 7 * * *", "job": "fetch.sh"}

  assert client.post(
    f"/api/apps/{app_id}/run-job", headers=controls["delegated"],
  ).status_code == 403
  assert client.post(
    f"/api/apps/{app_id}/schedule", headers=controls["delegated"], json=update,
  ).status_code == 403
  assert client.post(
    f"/api/apps/{app_id}/run-job", headers=controls["top_level"],
  ).status_code == 202
  assert client.post(
    f"/api/apps/{app_id}/schedule", headers=controls["app"], json=update,
  ).status_code == 200
  assert client.post(
    f"/api/apps/{app_id}/run-job", headers=controls["owner"],
  ).status_code == 202
  assert len(launches) == 2
  assert len(schedules) == 1


def test_push_owner_control_rejects_delegation_but_keeps_app_and_owner(
  client, controls,
):
  body = {
    "endpoint": "https://push.invalid/owner-controls",
    "keys": {"p256dh": "first", "auth": "first"},
  }
  assert client.post(
    "/api/push/subscribe", headers=controls["delegated"], json=body,
  ).status_code == 403
  assert client.post(
    "/api/push/subscribe", headers=controls["app"], json=body,
  ).status_code == 201
  body["keys"] = {"p256dh": "second", "auth": "second"}
  assert client.post(
    "/api/push/subscribe", headers=controls["top_level"], json=body,
  ).status_code == 201
  assert client.post(
    "/api/push/subscribe", headers=controls["owner"], json=body,
  ).status_code == 201
  assert client.request(
    "DELETE",
    "/api/push/subscribe", headers=controls["delegated"],
    json={"endpoint": body["endpoint"]},
  ).status_code == 403
  assert client.request(
    "DELETE",
    "/api/push/subscribe", headers=controls["owner"],
    json={"endpoint": body["endpoint"]},
  ).status_code == 204


def test_screen_browser_surfaces_require_human_owner_not_agent(
  client, controls,
):
  child_id = controls["chat_ids"]["child"]
  started = {
    "appId": controls["app_row"].id,
    "chatId": child_id,
    "route": f"/chat/{child_id}",
  }
  # Both a live delegated bearer and an ordinary top-level agent bearer are
  # owner-scoped JWTs, so this checks the human/browser distinction directly.
  assert client.post(
    "/api/screen-control/sessions", headers=controls["delegated"], json=started,
  ).status_code == 403
  assert client.post(
    "/api/screen-control/sessions", headers=controls["top_level"], json=started,
  ).status_code == 403
  session = client.post(
    "/api/screen-control/sessions", headers=controls["owner"], json=started,
  )
  assert session.status_code == 200, session.text
  session_id = session.json()["sessionId"]

  # Every browser-side endpoint keeps the same human-only contract. The event
  # probe uses a missing id so an accidental successful SSE authorization never
  # leaves this synchronous test waiting on a live stream.
  for headers in (controls["delegated"], controls["top_level"]):
    assert client.get(
      "/api/screen-control/sessions/missing/events", headers=headers,
    ).status_code == 403
    assert client.post(
      "/api/screen-control/sessions/missing/responses", headers=headers,
      json={"commandId": "command", "ok": True},
    ).status_code == 403
    assert client.delete(
      f"/api/screen-control/sessions/{session_id}", headers=headers,
    ).status_code == 403
  assert client.get(
    "/api/screen-control/sessions/missing/events", headers=controls["owner"],
  ).status_code == 404

  # The separately-owned exact-chat agent status route remains an agent path.
  assert client.get(
    f"/api/screen-control/chats/{child_id}", headers=controls["child_agent"],
  ).status_code == 200
  assert client.delete(
    f"/api/screen-control/sessions/{session_id}", headers=controls["owner"],
  ).status_code == 204


def test_project_owner_mailbox_rejects_delegation_but_agent_mailbox_remains(
  client, controls, db,
):
  project = client.post(
    "/api/projects", headers=controls["owner"],
    json={"name": "Owner mailbox", "template_id": "blank"},
  )
  assert project.status_code == 200, project.text
  project_id = project.json()["id"]
  for name in ("child", "parent"):
    db.get(models.Chat, controls["chat_ids"][name]).project_id = project_id
  db.commit()
  body = {
    # A child could formerly lie about this sender while using its inherited
    # owner JWT; the agent mailbox stamps its sender from the run instead.
    "sender_chat_id": controls["chat_ids"]["parent"],
    "recipients": [controls["chat_ids"]["child"]],
    "body": "Forged parent note",
  }
  assert client.post(
    f"/api/projects/{project_id}/agent-messages",
    headers=controls["delegated"], json=body,
  ).status_code == 403
  assert client.post(
    f"/api/projects/{project_id}/agent-messages",
    headers=controls["top_level"], json=body,
  ).status_code == 200
