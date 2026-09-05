"""Delegated agents cannot exercise controls that require owner confirmation."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from fastapi.responses import JSONResponse

from app import auth as auth_mod, models, self_reminders
from app.delegations import RunPolicy, delegation_execution_token


def _create_chat(client, owner_auth, title: str) -> str:
  response = client.post("/api/chats", json={"title": title}, headers=owner_auth)
  assert response.status_code == 200, response.text
  return response.json()["id"]


def _authorization_context(client, owner_token, db, tmp_path):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_ids = {
    name: _create_chat(client, owner_auth, f"Control boundary {name}")
    for name in ("child", "parent", "foreign", "top-level")
  }
  app = models.App(
    name="Common",
    description="",
    slug="common",
    source_dir=str(tmp_path / "common"),
    jsx_source="export default () => null",
    token_nonce="destructive-control-app-nonce",
    manage_apps=True,
    github_access=True,
  )
  db.add(app)
  db.flush()
  policy = RunPolicy(
    delegation_id="destructive-control-delegation",
    app_id=app.id,
    provider="codex",
    model=None,
    effort=None,
    scope="write",
    cwd="/data",
  )
  db.add(models.Delegation(
    id=policy.delegation_id,
    app_id=app.id,
    parent_chat_id=chat_ids["parent"],
    parent_root_run_id="destructive-control-parent-root",
    task_key="destructive-control",
    child_chat_id=chat_ids["child"],
    provider=policy.provider,
    model=policy.model,
    effort=policy.effort,
    scope=policy.scope,
    cwd=policy.cwd,
    prompt_sha256=hashlib.sha256(b"destructive control boundary").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="destructive-control-child-run",
    root_run_id="destructive-control-child-run",
    chat_id=chat_ids["child"],
    status="running",
    provider="codex",
  ))
  db.add(models.ChatRun(
    id="destructive-control-top-run",
    root_run_id="destructive-control-top-run",
    chat_id=chat_ids["top-level"],
    status="running",
    provider="codex",
  ))
  db.commit()

  owner = db.query(models.Owner).one()
  delegated_token = delegation_execution_token(
    db, policy, run_id="destructive-control-child-run",
  )
  top_level_token = auth_mod.create_agent_token(
    chat_ids["top-level"],
    "destructive-control-top-run",
    owner.username,
    owner.token_epoch,
  )
  app_token = auth_mod.create_access_token({
    "sub": owner.username,
    "scope": "app",
    "app_id": app.id,
    "app_nonce": app.token_nonce,
  }, token_epoch=owner.token_epoch)
  return {
    "owner": owner_auth,
    "delegated": {"Authorization": f"Bearer {delegated_token}"},
    "top_level": {"Authorization": f"Bearer {top_level_token}"},
    "app": {"Authorization": f"Bearer {app_token}"},
    "chat_ids": chat_ids,
    "app_row": app,
  }


def _request(client, auth, method, path, body=None, extra_headers=None):
  return client.request(
    method,
    path,
    json=body,
    headers={**auth, **(extra_headers or {})},
  )


def test_real_delegated_bearer_is_rejected_across_owner_control_surface(
  client, owner_token, db, tmp_path, monkeypatch,
):
  from app.routes import common as common_routes
  from app.routes import community as community_routes

  context = _authorization_context(client, owner_token, db, tmp_path)

  async def fake_request(*_args, **_kwargs):
    return JSONResponse({"ok": True})

  async def fake_existing_publication(*_args, **_kwargs):
    return JSONResponse({"ok": True})

  async def disconnected_profile(_db, _principal):
    return {
      "identity": {"name": "", "handle": ""},
      "profile": None,
      "account_error": None,
    }

  monkeypatch.setattr(community_routes, "_request", fake_request)
  monkeypatch.setattr(
    community_routes,
    "_publish_existing_github_revision",
    fake_existing_publication,
  )
  monkeypatch.setattr(common_routes, "_refresh_profile_cache", disconnected_profile)
  monkeypatch.setattr(common_routes, "_load_identity", lambda: {"joined_at": None})
  monkeypatch.setattr(common_routes, "_save_identity", lambda _identity: None)

  idempotency = {"Idempotency-Key": "owner-control-0001"}
  cases = [
    ("POST", "/api/projects/missing/invites", {"role": "viewer"}, None),
    ("DELETE", "/api/projects/missing/invites/missing", None, None),
    ("PATCH", "/api/projects/missing/members/missing", {"role": "viewer"}, None),
    ("DELETE", "/api/projects/missing/members/missing", None, None),
    ("DELETE", "/api/projects/missing", None, None),
    ("POST", "/api/projects/missing/git/push", {"confirmed": True}, None),
    ("POST", "/api/apps/999/publish", {}, None),
    ("DELETE", "/api/apps/999/publish", None, None),
    ("POST", "/api/apps/999/runtime-capabilities/accept", {"accept_digest": "0" * 64}, None),
    ("PATCH", "/api/apps/999", {"cross_app_access": "read"}, None),
    ("PUT", "/api/apps/999/hosted-publication", None, None),
    ("DELETE", "/api/apps/999/hosted-publication", None, None),
    ("DELETE", "/api/apps/999", None, None),
    ("DELETE", "/api/apps/999/data", None, None),
    (
      "POST",
      "/api/community/publications/github",
      {
        "app_id": 999,
        "repository_name": "control-test",
        "confirm_source_public": True,
      },
      idempotency,
    ),
    (
      "POST",
      "/api/community/apps",
      {"repository": "owner/repo", "commit_sha": "0" * 40},
      idempotency,
    ),
    (
      "PUT",
      "/api/community/apps/app:1234/rating",
      {"value": 5, "revision_id": "revision:1234"},
      idempotency,
    ),
    (
      "POST",
      "/api/community/apps/app:1234/revisions/revision:1234/comments",
      {"body": "Reviewed comment"},
      idempotency,
    ),
    (
      "POST",
      "/api/community/editorial/assets",
      {"mime_type": "image/png", "data_base64": "AAAA"},
      idempotency,
    ),
    (
      "PUT",
      "/api/community/editorial/spotlight",
      {"items": [{"app_id": "control-app"}]},
      idempotency,
    ),
    ("POST", "/api/common/join", None, None),
    ("PUT", "/api/common/me", {"bio": "Updated"}, None),
    ("POST", "/api/common/send", {"to": "", "text": "Hello"}, None),
    ("POST", "/api/common/publish", {"text": ""}, None),
    ("POST", "/api/common/like", {"post_id": "invalid"}, None),
    ("POST", "/api/common/reply", {"post_id": "invalid", "text": "Reply"}, None),
    ("POST", "/api/common/groups", {"name": "", "members": []}, None),
    ("POST", "/api/common/groups/invalid/send", {"text": "Hello"}, None),
    (
      "POST",
      "/api/common/groups/invalid/members",
      {"host": "invalid"},
      None,
    ),
    (
      "POST",
      "/api/common/objects/invalid/invites",
      {"role": "viewer"},
      None,
    ),
    ("DELETE", "/api/common/objects/invalid/members/peer.example", None, None),
    ("DELETE", "/api/common/objects/invalid", None, None),
    ("POST", "/api/common/objects/invalid/invalid/leave", None, None),
    (
      "POST",
      "/api/common/objects/invitations/invalid/invalid/decline",
      None,
      None,
    ),
    ("POST", "/api/common/objects", {"app": "", "doc": {}}, None),
    (
      "POST",
      "/api/common/objects/join",
      {"app": "common", "invite": "invalid"},
      None,
    ),
    (
      "PUT",
      "/api/common/objects/invalid/invalid/state",
      {"doc": {}, "expected_version": 1},
      None,
    ),
    (
      "POST",
      "/api/notifications/send",
      {"title": "Delegated notification", "body": "Must not send"},
      None,
    ),
    ("POST", "/api/notifications/read-all", None, None),
    ("DELETE", "/api/notifications", None, None),
    (
      "POST",
      "/api/self-reminders",
      {"chat_id": "missing", "note": "Later", "due_in_seconds": 60},
      None,
    ),
    ("DELETE", "/api/self-reminders/missing", None, None),
  ]

  failures = []
  for method, path, body, extra_headers in cases:
    response = _request(
      client,
      context["delegated"],
      method,
      path,
      body,
      extra_headers,
    )
    if response.status_code != 403 or response.json().get("detail") not in {
      "Delegated agents cannot perform owner-confirmed controls.",
      "Only an owner token can access this endpoint.",
    }:
      failures.append((method, path, response.status_code, response.text))
  assert failures == []


def test_delegated_app_bearer_cannot_patch_owner_managed_app_metadata(
  client, owner_token, db, tmp_path,
):
  context = _authorization_context(client, owner_token, db, tmp_path)
  app_id = context["app_row"].id

  renamed = client.patch(
    f"/api/apps/{app_id}",
    json={"name": "Delegated rename", "pinned": True},
    headers=context["delegated"],
  )
  assert renamed.status_code == 403, renamed.text

  for trust_change in (
    {"cross_app_access": "read"},
    {"share_with_apps": "read"},
    {"chat_log_access": "summary"},
    {"published_manifest_url": ""},
    {"manage_skills": False},
  ):
    blocked = client.patch(
      f"/api/apps/{app_id}",
      json=trust_change,
      headers=context["delegated"],
    )
    assert blocked.status_code == 403, (trust_change, blocked.text)

  db.expire_all()
  row = db.get(models.App, app_id)
  assert row.name == "Common"
  assert row.pinned_at is None
  assert row.cross_app_access == "none"
  assert row.share_with_apps == "none"
  assert row.chat_log_access == "none"
  assert row.published_manifest_url is None
  assert row.manage_skills is False

  top_level = client.patch(
    f"/api/apps/{app_id}",
    json={"cross_app_access": "read"},
    headers=context["top_level"],
  )
  assert top_level.status_code == 200, top_level.text
  owner = client.patch(
    f"/api/apps/{app_id}",
    json={"share_with_apps": "read"},
    headers=context["owner"],
  )
  assert owner.status_code == 200, owner.text


def test_intended_app_principals_keep_their_existing_control_paths(
  client, owner_token, db, tmp_path, monkeypatch,
):
  from app.routes import community as community_routes

  context = _authorization_context(client, owner_token, db, tmp_path)
  app_id = context["app_row"].id

  community_calls = []

  async def fake_request(method, path, **kwargs):
    community_calls.append((method, path, kwargs))
    return JSONResponse({"ok": True})

  async def fake_existing_publication(*_args, **_kwargs):
    community_calls.append(("POST", "existing-publication", {}))
    return JSONResponse({"ok": True})

  monkeypatch.setattr(community_routes, "_request", fake_request)
  monkeypatch.setattr(
    community_routes,
    "_publish_existing_github_revision",
    fake_existing_publication,
  )

  invalid_dm = client.post(
    "/api/common/send",
    json={"to": "", "text": "Hello"},
    headers=context["app"],
  )
  assert invalid_dm.status_code == 400, invalid_dm.text

  no_site = client.post(
    f"/api/apps/{app_id}/publish",
    json={},
    headers=context["app"],
  )
  assert no_site.status_code == 400, no_site.text

  missing_managed_app = client.delete(
    "/api/apps/999/data",
    headers=context["app"],
  )
  assert missing_managed_app.status_code == 404, missing_managed_app.text

  rated = client.put(
    "/api/community/apps/app:1234/rating",
    json={"value": 5, "revision_id": "revision:1234"},
    headers={**context["app"], "Idempotency-Key": "owner-control-0002"},
  )
  assert rated.status_code == 200, rated.text

  published = client.post(
    "/api/community/apps",
    json={"repository": "owner/repo", "commit_sha": "0" * 40},
    headers={**context["app"], "Idempotency-Key": "owner-control-0003"},
  )
  assert published.status_code == 200, published.text
  assert [call[:2] for call in community_calls] == [
    ("PUT", "/v1/community/apps/app:1234/rating"),
    ("POST", "existing-publication"),
  ]


def test_owner_top_level_and_app_principals_keep_common_object_controls(
  client, owner_token, db, tmp_path, monkeypatch,
):
  from app.routes import common as common_routes
  from app.routes import common_objects as object_routes

  context = _authorization_context(client, owner_token, db, tmp_path)
  peer_host = "peer.example.com"
  remote_oid = "b" * 32

  class FakeResponse:
    status_code = 200

    def json(self):
      return {
        "object": {
          "app": "common",
          "kind": "board",
          "label": "Joined object",
          "members": {common_routes._own_host(): {"role": "editor"}},
        },
        "doc": {"value": "remote"},
      }

  class FakeAsyncClient:
    def __init__(self, *_args, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return False

    async def post(self, *_args, **_kwargs):
      return FakeResponse()

  monkeypatch.setattr(object_routes.httpx, "AsyncClient", FakeAsyncClient)

  app_created = client.post(
    "/api/common/objects",
    json={
      "app": "common",
      "kind": "board",
      "label": "App object",
      "doc": {"value": 1},
    },
    headers=context["app"],
  )
  assert app_created.status_code == 200, app_created.text
  app_oid = app_created.json()["id"]

  app_write = client.put(
    f"/api/common/objects/{common_routes._own_host()}/{app_oid}/state",
    json={"doc": {"value": 2}, "expected_version": 1},
    headers=context["app"],
  )
  assert app_write.status_code == 200, app_write.text
  assert app_write.json() == {"status": "ok", "version": 2}

  owner_created = client.post(
    "/api/common/objects",
    json={"app": "common", "doc": {"value": "owner"}},
    headers=context["owner"],
  )
  assert owner_created.status_code == 200, owner_created.text

  top_level_created = client.post(
    "/api/common/objects",
    json={"app": "common", "doc": {"value": "top-level"}},
    headers=context["top_level"],
  )
  assert top_level_created.status_code == 200, top_level_created.text

  invitation_oid = "a" * 32
  invitation_path = object_routes._invitation_path(peer_host, invitation_oid)
  invitation_path.write_text('{"app":"common"}')
  declined = client.post(
    f"/api/common/objects/invitations/{peer_host}/{invitation_oid}/decline",
    headers=context["app"],
  )
  assert declined.status_code == 200, declined.text
  assert not invitation_path.exists()

  joined = client.post(
    "/api/common/objects/join",
    json={
      "app": "common",
      "invite": f"{remote_oid}@{peer_host}#{'s' * 24}",
    },
    headers=context["app"],
  )
  assert joined.status_code == 200, joined.text
  assert joined.json()["status"] == "joined"


def test_owner_top_level_and_app_principals_keep_notification_controls(
  client, owner_token, db, tmp_path,
):
  context = _authorization_context(client, owner_token, db, tmp_path)

  app_sent = client.post(
    "/api/notifications/send",
    json={"title": "App update", "body": "Ready"},
    headers=context["app"],
  )
  assert app_sent.status_code == 200, app_sent.text

  top_level_sent = client.post(
    "/api/notifications/send",
    json={"title": "Agent update", "body": "Ready"},
    headers=context["top_level"],
  )
  assert top_level_sent.status_code == 200, top_level_sent.text

  read_all = client.post(
    "/api/notifications/read-all",
    headers=context["owner"],
  )
  assert read_all.status_code == 200, read_all.text
  assert read_all.json() == {"updated": 2}

  cleared = client.delete(
    "/api/notifications",
    headers=context["owner"],
  )
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"deleted": 2}


def test_project_push_does_not_reach_git_for_delegated_bearer(
  client, owner_token, db, tmp_path, monkeypatch,
):
  from app.routes import projects as project_routes

  context = _authorization_context(client, owner_token, db, tmp_path)
  pushed = []
  monkeypatch.setattr(
    project_routes,
    "_live_project",
    lambda _db, project_id: SimpleNamespace(id=project_id),
  )
  monkeypatch.setattr(project_routes, "_project_root", lambda _project: tmp_path)
  monkeypatch.setattr(
    project_routes,
    "_require_project_github_connection",
    lambda: None,
  )
  monkeypatch.setattr(
    project_routes.project_git,
    "push_project",
    lambda _root, expected: pushed.append(expected) or {"head": "pushed"},
  )

  delegated = client.post(
    "/api/projects/project-1/git/push",
    json={"confirmed": True, "expected_head": "expected"},
    headers=context["delegated"],
  )
  assert delegated.status_code == 403, delegated.text
  assert pushed == []

  top_level = client.post(
    "/api/projects/project-1/git/push",
    json={"confirmed": True, "expected_head": "expected"},
    headers=context["top_level"],
  )
  assert top_level.status_code == 200, top_level.text
  assert pushed == ["expected"]


def test_self_reminder_create_and_cancel_require_nondelegated_control(
  client, owner_token, db, tmp_path,
):
  context = _authorization_context(client, owner_token, db, tmp_path)
  parent_chat_id = context["chat_ids"]["parent"]

  delegated_create = client.post(
    "/api/self-reminders",
    json={
      "chat_id": parent_chat_id,
      "note": "Injected child reminder",
      "due_in_seconds": 3600,
    },
    headers=context["delegated"],
  )
  assert delegated_create.status_code == 403, delegated_create.text
  assert all(
    record["note"] != "Injected child reminder"
    for record in self_reminders.list_pending(parent_chat_id)
  )

  created = client.post(
    "/api/self-reminders",
    json={
      "chat_id": parent_chat_id,
      "note": "Top-level reminder",
      "due_in_seconds": 3600,
    },
    headers=context["top_level"],
  )
  assert created.status_code == 201, created.text
  reminder_id = created.json()["id"]

  delegated_cancel = client.delete(
    f"/api/self-reminders/{reminder_id}",
    headers=context["delegated"],
  )
  assert delegated_cancel.status_code == 403, delegated_cancel.text
  assert any(
    record["id"] == reminder_id and record["status"] == "pending"
    for record in self_reminders.list_pending(parent_chat_id)
  )

  owner_cancel = client.delete(
    f"/api/self-reminders/{reminder_id}",
    headers=context["owner"],
  )
  assert owner_cancel.status_code == 200, owner_cancel.text
  assert owner_cancel.json()["status"] == "cancelled"
