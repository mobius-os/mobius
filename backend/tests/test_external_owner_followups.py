"""Delegated children cannot cross owner-confirmed app lifecycle seams."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.responses import JSONResponse
import pytest

from app import auth as auth_mod, models
from app.config import get_settings
from app.delegations import RunPolicy, delegation_execution_token


def _create_chat(client, auth, title: str) -> str:
  response = client.post("/api/chats", json={"title": title}, headers=auth)
  assert response.status_code == 200, response.text
  return response.json()["id"]


def _owner_control_context(client, owner_token, db):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  child_id = _create_chat(client, owner_auth, "follow-up delegated child")
  parent_id = _create_chat(client, owner_auth, "follow-up parent")
  top_level_id = _create_chat(client, owner_auth, "follow-up top level")
  source_dir = Path(get_settings().data_dir) / "apps" / "owner-control-followup"
  app = models.App(
    name="Owner control follow-up",
    description="",
    slug="owner-control-followup",
    source_dir=str(source_dir),
    jsx_source="export default () => null",
    token_nonce="owner-control-followup-nonce",
    manage_apps=True,
    github_access=True,
  )
  db.add(app)
  db.flush()
  policy = RunPolicy(
    delegation_id="owner-control-followup-delegation",
    app_id=app.id,
    provider="codex",
    model=None,
    effort=None,
    scope="read",
    cwd="/data",
  )
  db.add(models.Delegation(
    id=policy.delegation_id,
    app_id=app.id,
    parent_chat_id=parent_id,
    parent_root_run_id="owner-control-followup-parent-root",
    task_key="owner-control-followup",
    child_chat_id=child_id,
    provider=policy.provider,
    model=policy.model,
    effort=policy.effort,
    scope=policy.scope,
    cwd=policy.cwd,
    prompt_sha256=hashlib.sha256(b"owner control follow-up").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="owner-control-followup-child-run",
    root_run_id="owner-control-followup-child-run",
    chat_id=child_id,
    status="running",
    provider="codex",
  ))
  db.add(models.ChatRun(
    id="owner-control-followup-top-run",
    root_run_id="owner-control-followup-top-run",
    chat_id=top_level_id,
    status="running",
    provider="codex",
  ))
  db.commit()

  owner = db.query(models.Owner).one()
  delegated = delegation_execution_token(
    db, policy, run_id="owner-control-followup-child-run",
  )
  top_level = auth_mod.create_agent_token(
    top_level_id,
    owner.username,
    owner.token_epoch,
    run_id="owner-control-followup-top-run",
  )
  app_token = auth_mod.create_app_token(
    app.id,
    owner.username,
    owner.token_epoch,
    app.token_nonce,
  )
  return {
    "app_id": app.id,
    "source_dir": str(source_dir),
    "owner": owner_auth,
    "delegated": {"Authorization": f"Bearer {delegated}"},
    "top_level": {"Authorization": f"Bearer {top_level}"},
    "app": {"Authorization": f"Bearer {app_token}"},
  }


def _requests(context):
  app_id = context["app_id"]
  source_dir = context["source_dir"]
  return [
    ("POST", "/api/apps/install", {}),
    (
      "POST",
      f"/api/apps/{app_id}/conflict-resolver-chat",
      {"resolution_policy": "preserve_local"},
    ),
    (
      "POST",
      "/api/apps/resolve-update/policy",
      {"source_dir": source_dir, "policy": "preserve_local"},
    ),
    (
      "POST",
      "/api/apps/resolve-update",
      {"source_dir": source_dir},
    ),
    ("POST", f"/api/apps/{app_id}/recover", None),
    (
      "POST",
      f"/api/github/contributions/{app_id}/for-chat/missing-chat/work",
      {"intent": "prepare"},
    ),
    (
      "POST",
      f"/api/github/contributions/{app_id}/for-chat/missing-chat/work/stop",
      None,
    ),
    (
      "POST",
      f"/api/github/contributions/{app_id}/missing-record/connect-app",
      None,
    ),
  ]


@pytest.mark.parametrize(
  "request_index",
  range(8),
  ids=(
    "store-install",
    "conflict-resolver-chat",
    "resolution-policy",
    "resolved-update-promotion",
    "app-recovery",
    "contribution-work-start",
    "contribution-work-stop",
    "merged-publication-connect",
  ),
)
def test_real_delegated_bearer_cannot_cross_remaining_owner_controls(
  client, owner_token, db, request_index,
):
  context = _owner_control_context(client, owner_token, db)

  method, path, body = _requests(context)[request_index]
  response = client.request(
    method, path, headers=context["delegated"], json=body,
  )
  assert response.status_code == 403, (path, response.text)


def test_real_delegated_bearer_cannot_report_external_community_install(
  client, owner_token, db, monkeypatch,
):
  from app.routes import community

  context = _owner_control_context(client, owner_token, db)
  calls = []

  async def request(*args, **kwargs):
    calls.append((args, kwargs))
    return JSONResponse({"ok": True})

  monkeypatch.setattr(community, "_request", request)
  response = client.post(
    "/api/community/apps/app_public_1234/revisions/rev_public_1234/installs",
    headers={
      **context["delegated"],
      "Idempotency-Key": "store:install:owner-control-followup",
    },
    json={"local_app_id": f"app:{context['app_id']}:owner-control-followup"},
  )
  assert response.status_code == 403, response.text
  assert calls == []


def test_existing_owner_top_level_and_app_authority_still_reaches_routes(
  client, owner_token, db, monkeypatch,
):
  from app.routes import community

  context = _owner_control_context(client, owner_token, db)

  async def request(*_args, **_kwargs):
    return JSONResponse({"ok": True})

  monkeypatch.setattr(community, "_request", request)

  for actor in ("owner", "top_level"):
    for method, path, body in _requests(context):
      response = client.request(method, path, headers=context[actor], json=body)
      assert response.status_code != 403, (actor, path, response.text)

  app_allowed_paths = [
    _requests(context)[0],
    _requests(context)[1],
    _requests(context)[4],
    _requests(context)[7],
  ]
  for method, path, body in app_allowed_paths:
    response = client.request(method, path, headers=context["app"], json=body)
    assert response.status_code != 403, (path, response.text)

  for actor in ("owner", "top_level", "app"):
    response = client.post(
      "/api/community/apps/app_public_1234/revisions/rev_public_1234/installs",
      headers={
        **context[actor],
        "Idempotency-Key": f"store:install:{actor}:owner-control-followup",
      },
      json={"local_app_id": f"app:{context['app_id']}:owner-control-followup"},
    )
    assert response.status_code == 200, (actor, response.text)
