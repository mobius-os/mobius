"""Delegated-agent boundaries for owner-confirmed external effects."""

from __future__ import annotations

import hashlib

from app import auth as auth_mod, models
from app.delegations import RunPolicy, delegation_execution_token


def _create_chat(client, owner_auth, title: str) -> str:
  response = client.post("/api/chats", json={"title": title}, headers=owner_auth)
  assert response.status_code == 200, response.text
  return response.json()["id"]


def _external_control_auth(client, owner_token, db):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  child_id = _create_chat(client, owner_auth, "external-control-child")
  parent_id = _create_chat(client, owner_auth, "external-control-parent")
  top_level_id = _create_chat(client, owner_auth, "external-control-top-level")
  app = models.App(
    name="External controls",
    description="",
    slug="external-controls",
    source_dir="/tmp/mobius-tests/external-controls",
    jsx_source="export default () => null",
    token_nonce="external-controls-nonce",
    github_access=True,
    github_connect=True,
    connections_manage=True,
    connect_manage=True,
    capability_contract={
      "data": {
        "connect_manage": True,
        "identity_manage": True,
        "railway_manage": True,
      },
    },
  )
  db.add(app)
  db.flush()
  policy = RunPolicy(
    delegation_id="external-control-delegation",
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
    parent_chat_id=parent_id,
    parent_root_run_id="external-control-parent-root",
    task_key="external-control",
    child_chat_id=child_id,
    provider=policy.provider,
    model=policy.model,
    effort=policy.effort,
    scope=policy.scope,
    cwd=policy.cwd,
    prompt_sha256=hashlib.sha256(b"external control boundary").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="external-control-child-run",
    root_run_id="external-control-child-run",
    chat_id=child_id,
    status="running",
    provider="codex",
  ))
  db.add(models.ChatRun(
    id="external-control-top-level-run",
    root_run_id="external-control-top-level-run",
    chat_id=top_level_id,
    status="running",
    provider="codex",
  ))
  db.commit()

  owner = db.query(models.Owner).one()
  delegated = delegation_execution_token(
    db, policy, run_id="external-control-child-run",
  )
  top_level = auth_mod.create_agent_token(
    top_level_id,
    "external-control-top-level-run",
    owner.username,
    owner.token_epoch,
  )
  app_token = auth_mod.create_app_token(
    app.id,
    owner.username,
    owner.token_epoch,
    app.token_nonce,
  )
  return {
    "owner": owner_auth,
    "delegated": {"Authorization": f"Bearer {delegated}"},
    "top_level": {"Authorization": f"Bearer {top_level}"},
    "app": {"Authorization": f"Bearer {app_token}"},
  }, app.id


def test_delegated_bearer_cannot_delete_railway_deployment(
  client, owner_token, db, monkeypatch,
):
  from app.routes import identity

  auths, _app_id = _external_control_auth(client, owner_token, db)
  calls = []

  async def mutate(*args, **kwargs):
    calls.append((args, kwargs))
    return {"instance": {"id": "mob_example"}}

  monkeypatch.setattr(identity, "_railway_mutation", mutate)
  readable = client.get("/api/identity/railway", headers=auths["delegated"])
  assert readable.status_code == 200, readable.text
  response = client.delete(
    "/api/identity/railway/deployments/mob_example",
    headers=auths["delegated"],
  )
  assert response.status_code == 403, response.text
  assert calls == []


def test_delegated_bearer_cannot_change_github_credentials(
  client, owner_token, db, monkeypatch,
):
  from app.routes import github

  auths, _app_id = _external_control_auth(client, owner_token, db)
  calls = []
  monkeypatch.setattr(
    github.github_auth, "set_device_flow", lambda value: calls.append(value),
  )
  monkeypatch.setattr(
    github.github_auth, "clear_credentials", lambda: calls.append("clear"),
  )

  status = client.get("/api/github/status", headers=auths["delegated"])
  assert status.status_code == 403, status.text
  response = client.delete("/api/github/connect", headers=auths["delegated"])
  assert response.status_code == 403, response.text
  assert calls == []


def test_delegated_bearer_cannot_publish_reviewed_contribution(
  client, owner_token, db,
):
  auths, app_id = _external_control_auth(client, owner_token, db)
  response = client.post(
    f"/api/github/contributions/{app_id}/missing-record/submit",
    headers=auths["delegated"],
  )
  assert response.status_code == 403, response.text
  relayed = client.post(
    f"/api/contribution-relay/{app_id}/missing-record/submit",
    headers=auths["delegated"],
    json={"confirm_publication": True},
  )
  assert relayed.status_code == 403, relayed.text
  reply = client.post(
    f"/api/github/contributions/{app_id}/missing-record/reply",
    headers=auths["delegated"],
    json={"run_id": "external-control-child-run", "body": "Public reply"},
  )
  assert reply.status_code == 403, reply.text
  update = client.post(
    f"/api/github/contributions/{app_id}/missing-record/update",
    headers=auths["delegated"],
    json={
      "run_id": "external-control-child-run",
      "head_sha": "a" * 40,
      "diff_sha256": "b" * 64,
      "summary": "Public update",
    },
  )
  assert update.status_code == 403, update.text


def test_delegated_bearer_cannot_toggle_autopilot(
  client, owner_token, db,
):
  auths, app_id = _external_control_auth(client, owner_token, db)
  toggled = client.post(
    f"/api/github/contributions/{app_id}/missing-record/autopilot",
    headers=auths["delegated"],
    json={"enabled": True},
  )
  assert toggled.status_code == 403, toggled.text


def test_delegated_bearer_cannot_spawn_autopilot_round(
  client, owner_token, db,
):
  auths, app_id = _external_control_auth(client, owner_token, db)
  response = client.post(
    f"/api/github/contributions/{app_id}/missing-record/respond",
    headers=auths["delegated"],
    json={"attention": {"key": "review:example"}},
  )
  assert response.status_code == 403, response.text


def test_delegated_bearer_cannot_settle_autopilot_claim(
  client, owner_token, db,
):
  auths, app_id = _external_control_auth(client, owner_token, db)
  completed = client.post(
    f"/api/github/contributions/{app_id}/missing-record/complete",
    headers=auths["delegated"],
    json={"run_id": "external-control-child-run", "outcome": "done"},
  )
  assert completed.status_code == 403, completed.text

  escalated = client.post(
    f"/api/github/contributions/{app_id}/missing-record/escalate",
    headers=auths["delegated"],
    json={"run_id": "external-control-child-run", "message": "Need owner"},
  )
  assert escalated.status_code == 403, escalated.text


def test_delegated_bearer_cannot_add_connector(
  client, owner_token, db, monkeypatch,
):
  from app.routes import connectors

  auths, _app_id = _external_control_auth(client, owner_token, db)
  calls = []

  async def handshake(*args, **kwargs):
    calls.append(("handshake", args, kwargs))
    return {"name": "Example", "tools": [], "est_tokens": 0}

  monkeypatch.setattr(connectors.core, "handshake", handshake)
  added = client.post(
    "/api/connectors",
    headers=auths["delegated"],
    json={
      "url": "https://mcp.example/mcp",
      "name": "Example",
      "auth_header": "X-Api-Key",
      "auth_value": "secret-key",
    },
  )
  assert added.status_code == 403, added.text
  deleted = client.delete(
    "/api/connectors/1",
    headers={**auths["delegated"], "X-Mobius-Connector-Generation": "old"},
  )
  assert deleted.status_code == 403, deleted.text
  assert calls == []


def test_delegated_bearer_cannot_write_app_secret(
  client, owner_token, db,
):
  auths, app_id = _external_control_auth(client, owner_token, db)
  secret = client.put(
    f"/api/apps/{app_id}/secrets/provider-key",
    headers=auths["delegated"],
    json={"value": "private-value"},
  )
  assert secret.status_code == 403, secret.text
  deleted = client.delete(
    f"/api/apps/{app_id}/secrets/provider-key",
    headers=auths["delegated"],
  )
  assert deleted.status_code == 403, deleted.text


def test_delegated_bearer_cannot_create_paired_host(
  client, owner_token, db, monkeypatch,
):
  from app.routes import connect

  auths, _app_id = _external_control_auth(client, owner_token, db)
  calls = []
  monkeypatch.setattr(connect, "_save_host", lambda host: calls.append(host))
  host = client.post(
    "/api/connect/hosts",
    headers=auths["delegated"],
    json={"name": "Workstation"},
  )
  assert host.status_code == 403, host.text
  deleted = client.delete(
    "/api/connect/hosts/h_missing",
    headers=auths["delegated"],
  )
  assert deleted.status_code == 403, deleted.text
  pairing = client.get(
    "/api/connect/hosts/h_missing/pairing",
    headers=auths["delegated"],
  )
  assert pairing.status_code == 403, pairing.text
  assert calls == []


def test_owner_top_level_agent_and_scoped_app_keep_external_controls(
  client, owner_token, db, monkeypatch,
):
  from app.routes import github, identity

  auths, app_id = _external_control_auth(client, owner_token, db)

  async def mutate(*args, **kwargs):
    return {"instance": {"id": "mob_example"}}

  monkeypatch.setattr(identity, "_railway_mutation", mutate)
  monkeypatch.setattr(github.github_auth, "set_device_flow", lambda _value: None)
  monkeypatch.setattr(github.github_auth, "clear_credentials", lambda: None)

  for actor in ("owner", "top_level", "app"):
    railway = client.delete(
      "/api/identity/railway/deployments/mob_example",
      headers=auths[actor],
    )
    assert railway.status_code == 202, (actor, railway.text)

    disconnected = client.delete("/api/github/connect", headers=auths[actor])
    assert disconnected.status_code == 200, (actor, disconnected.text)

    github_status = client.get("/api/github/status", headers=auths[actor])
    assert github_status.status_code == 200, (actor, github_status.text)

    publication = client.post(
      f"/api/github/contributions/{app_id}/missing-record/submit",
      headers=auths[actor],
    )
    assert publication.status_code == 404, (actor, publication.text)

    relay = client.post(
      f"/api/contribution-relay/{app_id}/missing-record/submit",
      headers=auths[actor],
      json={"confirm_publication": True},
    )
    assert relay.status_code == 404, (actor, relay.text)


def test_owner_top_level_and_scoped_app_keep_review_control_boundaries(
  client, owner_token, db, monkeypatch,
):
  from app.routes import github

  auths, app_id = _external_control_auth(client, owner_token, db)
  calls = []
  monkeypatch.setattr(
    github.github_auth,
    "read_state",
    lambda: {"token": "connected", "login": "octocat"},
  )
  monkeypatch.setattr(
    github,
    "_gh",
    lambda _repo_path, *args, **_kwargs: calls.append(args) or "{}",
  )

  for actor in ("owner", "top_level", "app"):
    toggled = client.post(
      f"/api/github/contributions/{app_id}/missing-record/autopilot",
      headers=auths[actor],
      json={"enabled": True},
    )
    assert toggled.status_code == 404, (actor, toggled.text)

  for actor in ("owner", "top_level"):
    completed = client.post(
      f"/api/github/contributions/{app_id}/missing-record/complete",
      headers=auths[actor],
      json={"run_id": "missing-run", "outcome": "done"},
    )
    assert completed.status_code == 409, (actor, completed.text)

    escalated = client.post(
      f"/api/github/contributions/{app_id}/missing-record/escalate",
      headers=auths[actor],
      json={"run_id": "missing-run", "message": "Need owner"},
    )
    assert escalated.status_code == 409, (actor, escalated.text)

  service_respond = client.post(
    f"/api/github/contributions/{app_id}/missing-record/respond",
    headers=auths["app"],
    json={"attention": {}},
  )
  assert service_respond.status_code == 400, service_respond.text
  assert calls == []


def test_owner_top_level_agent_and_scoped_app_keep_scoped_connection_controls(
  client, owner_token, db, monkeypatch,
):
  from app.routes import connect, connectors

  auths, app_id = _external_control_auth(client, owner_token, db)

  async def handshake(*_args, **_kwargs):
    return {"name": "Example", "tools": [], "est_tokens": 0}

  saved_hosts = []
  monkeypatch.setattr(connectors.core, "handshake", handshake)
  monkeypatch.setattr(connect, "_save_host", saved_hosts.append)

  for actor in ("owner", "top_level", "app"):
    added = client.post(
      "/api/connectors",
      headers=auths[actor],
      json={
        "url": f"https://{actor}.example/mcp",
        "name": f"Example {actor}",
        "auth_header": "X-Api-Key",
        "auth_value": "secret-key",
      },
    )
    assert added.status_code == 201, (actor, added.text)

    secret = client.put(
      f"/api/apps/{app_id}/secrets/provider-key",
      headers=auths[actor],
      json={"value": f"private-{actor}"},
    )
    assert secret.status_code == 204, (actor, secret.text)

    host = client.post(
      "/api/connect/hosts",
      headers=auths[actor],
      json={"name": f"Workstation {actor}"},
    )
    assert host.status_code == 200, (actor, host.text)

  assert [host["name"] for host in saved_hosts] == [
    "Workstation owner",
    "Workstation top_level",
    "Workstation app",
  ]


def test_outbound_sharing_keeps_external_owner_control_boundary(
  client, owner_token, db, monkeypatch,
):
  from app import connect_outbound

  auths, _app_id = _external_control_auth(client, owner_token, db)
  calls = []

  async def create(label, command):
    calls.append(("create", label))
    return {"id": "o_0123456789abcdef"}

  async def revoke(profile_id):
    calls.append(("revoke", profile_id))

  monkeypatch.setattr(connect_outbound, "create_profile", create)
  monkeypatch.setattr(connect_outbound, "revoke_profile", revoke)
  body = {"label": "Shared access", "command": "pairing-command-data"}
  path = "/api/connect/outbound"
  created = client.post(path, headers=auths["delegated"], json=body)
  deleted = client.delete(path + "/o_0123456789abcdef", headers=auths["delegated"])
  assert created.status_code == 403, created.text
  assert deleted.status_code == 403, deleted.text
  assert calls == []

  for actor in ("owner", "top_level", "app"):
    created = client.post(path, headers=auths[actor], json=body)
    deleted = client.delete(path + "/o_0123456789abcdef", headers=auths[actor])
    assert created.status_code == 200, (actor, created.text)
    assert deleted.status_code == 200, (actor, deleted.text)
  assert len(calls) == 6
