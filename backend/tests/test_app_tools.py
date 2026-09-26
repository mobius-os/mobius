"""Agent tools contributed by installed apps, and the moment of each call."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import app_tools, auth as auth_mod, models
from app.app_capabilities import contract_from_app_state, contract_from_manifest
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from app.platform_tools import codex_turn_mcp_config


LOG_TOOL = {
  "name": "log_friction",
  "description": "Record what made this work harder than it should have been.",
  "input_schema": {
    "type": "object",
    "properties": {"friction": {"type": "string"}},
    "required": ["friction"],
  },
}


def _manifest(**over):
  manifest = {
    "id": "reflection",
    "name": "Reflection",
    "version": "1.0.0",
    "description": "Learns from friction",
    "entry": "index.jsx",
    "source_files": ["service.py"],
    "service": {"entry": "service.py"},
    "tools": [LOG_TOOL],
  }
  manifest.update(over)
  return manifest


def _contract(tools=(LOG_TOOL,), *, service=True):
  manifest = {"tools": list(tools)}
  if service:
    manifest["service"] = {"id": "reflection", "entry": "service.py"}
  return contract_from_manifest(manifest)


def _app(db, slug="reflection", *, contract=None, deleted=False):
  from datetime import datetime

  row = models.App(
    name=slug.title(), slug=slug, description="", jsx_source="",
    source_dir=f"/tmp/apps/{slug}",
    capability_contract=contract if contract is not None else _contract(),
    deleted_at=datetime(2026, 1, 1) if deleted else None,
  )
  db.add(row)
  db.commit()
  return row


def _run(db, *, chat_id="chat-1", run_id="run-1", provider="claude"):
  if db.get(models.Chat, chat_id) is None:
    db.add(models.Chat(id=chat_id, title="Chat", messages=[]))
  db.add(models.ChatRun(id=run_id, chat_id=chat_id, provider=provider))
  db.commit()


def _agent_auth(db, chat_id="chat-1", run_id="run-1"):
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id, owner.username, owner.token_epoch,
    run_id=run_id, expires_delta=timedelta(minutes=5),
  )
  return {"Authorization": f"Bearer {token}"}


# ── Manifest declaration ─────────────────────────────────────────────────


def test_manifest_tools_are_served_by_the_apps_own_service():
  validate_manifest_contract(_manifest())
  with pytest.raises(ManifestContractError, match="requires a `service`"):
    validate_manifest_contract(_manifest(service=None, source_files=[]))


@pytest.mark.parametrize("tools", [
  "log_friction",
  [{**LOG_TOOL, "name": "Log-Friction"}],
  [{**LOG_TOOL, "command": ["python3", "x.py"]}],
  [{**LOG_TOOL, "description": " "}],
  [{**LOG_TOOL, "input_schema": {"type": "string"}}],
  [LOG_TOOL, LOG_TOOL],
])
def test_manifest_rejects_malformed_tool_declarations(tools):
  with pytest.raises(ManifestContractError, match="tools"):
    validate_manifest_contract(_manifest(tools=tools))


# ── Reviewed contract ────────────────────────────────────────────────────


def test_reviewed_contract_carries_complete_tool_declarations():
  contract = _contract()
  assert contract["agent"]["tools"] == [LOG_TOOL]
  assert "system_app" not in contract


def test_local_contract_rebuild_preserves_tools_unless_the_package_revokes_them(db):
  app = _app(db)
  assert contract_from_app_state(app)["agent"]["tools"] == [LOG_TOOL]
  assert contract_from_app_state(app, tools=[])["agent"]["tools"] == []


# ── Listing ──────────────────────────────────────────────────────────────


def test_live_apps_with_a_service_contribute_tools_under_their_slug(db):
  _app(db, "reflection")
  _app(db, "gone", deleted=True)
  _app(db, "no-service", contract=_contract(service=False))
  _app(db, "night-owl")

  listed = [tool.exposed_name for tool in app_tools.live_app_tools(db)]

  assert listed == ["reflection_log_friction", "night_owl_log_friction"]


def test_colliding_exposed_names_keep_the_older_install(db):
  first = _app(db, "night-owl")
  _app(db, "night_owl")

  tools = app_tools.live_app_tools(db)

  assert [(tool.exposed_name, tool.app_id) for tool in tools] == [
    ("night_owl_log_friction", first.id),
  ]


# ── Call moment ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider, meta, expected", [
  ("claude", {"claudecode/toolUseId": "toolu_1", "progressToken": 2}, "toolu_1"),
  ("codex", {"itemId": "ctc_1", "callId": "exec-1", "threadId": "t"}, "ctc_1"),
  ("claude", {"itemId": "ctc_1"}, None),
  ("codex", None, None),
])
def test_call_moment_records_the_providers_own_call_id(db, provider, meta, expected):
  _run(db, provider=provider)

  moment = app_tools.call_moment(db, chat_id="chat-1", run_id="run-1", meta=meta)

  assert moment == {
    "chat_id": "chat-1", "run_id": "run-1",
    "provider": provider, "call_id": expected,
  }


def test_call_moment_never_borrows_another_chats_run(db):
  _run(db, chat_id="other", run_id="run-1", provider="claude")
  moment = app_tools.call_moment(
    db, chat_id="chat-1", run_id="run-1",
    meta={"claudecode/toolUseId": "toolu_1"},
  )
  assert moment["provider"] is None and moment["call_id"] is None


# ── Routes ───────────────────────────────────────────────────────────────


def test_listing_requires_the_current_agent_run(client, auth, db):
  _app(db)
  _run(db)
  assert client.get("/api/agent/app-tools/", headers=auth).status_code == 403

  response = client.get("/api/agent/app-tools/", headers=_agent_auth(db))

  assert response.status_code == 200
  assert response.json()["tools"] == [{
    "name": "reflection_log_friction",
    "description": LOG_TOOL["description"],
    "inputSchema": LOG_TOOL["input_schema"],
  }]


def test_call_runs_the_apps_service_with_arguments_and_moment(
  client, auth, db, monkeypatch,
):
  app = _app(db)
  _run(db, provider="claude")
  calls = []

  async def fake_invoke(target, owner, envelope, *, timeout_seconds, lane):
    calls.append((target.id, envelope, timeout_seconds))
    return 200, "Logged.", {}, None

  monkeypatch.setattr(app_tools.app_services, "invoke_service", fake_invoke)

  response = client.post(
    "/api/agent/app-tools/call",
    headers=_agent_auth(db),
    json={
      "name": "reflection_log_friction",
      "arguments": {"friction": "retried a flaky command"},
      "meta": {"claudecode/toolUseId": "toolu_9"},
    },
  )

  assert response.status_code == 200, response.text
  assert response.json() == {"result": "Logged.", "is_error": False}
  (app_id, envelope, timeout), = calls
  assert app_id == app.id
  assert timeout == app_tools.TOOL_TIMEOUT_SECONDS
  assert envelope["method"] == "POST"
  assert envelope["path"] == "/tools/log_friction"
  assert envelope["public"] is False
  assert envelope["body"] == {
    "arguments": {"friction": "retried a flaky command"},
    "call": {
      "chat_id": "chat-1", "run_id": "run-1",
      "provider": "claude", "call_id": "toolu_9",
    },
  }
  assert envelope["actor"] == {
    "scope": "owner", "app_id": None, "app_slug": None,
    "delegated": False, "access": "write",
  }


@pytest.mark.parametrize("scope", ["read", "write"])
def test_call_tells_the_app_a_helper_called_and_whether_it_may_write(
  client, auth, db, monkeypatch, scope,
):
  app = _app(db)
  for chat_id in ("parent", "child"):
    db.add(models.Chat(id=chat_id, title=chat_id, messages=[]))
  db.add(models.Delegation(
    id="helper", app_id=app.id, parent_chat_id="parent",
    parent_root_run_id="parent-run", task_key="helper", child_chat_id="child",
    provider="claude", scope=scope, cwd="/data", prompt_sha256="0" * 64,
  ))
  db.commit()
  _run(db, chat_id="child", run_id="child-run")
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    "child", owner.username, owner.token_epoch, run_id="child-run",
    delegation_id="helper", delegation_chat="child",
  )
  actors = []

  async def fake_invoke(_target, _owner, envelope, *, timeout_seconds, lane):
    actors.append(envelope["actor"])
    return 200, "Logged.", {}, None

  monkeypatch.setattr(app_tools.app_services, "invoke_service", fake_invoke)
  response = client.post(
    "/api/agent/app-tools/call",
    headers={"Authorization": f"Bearer {token}"},
    json={"name": "reflection_log_friction", "arguments": {"friction": "x"}},
  )

  assert response.status_code == 200, response.text
  assert actors == [{
    "scope": "owner", "app_id": None, "app_slug": None,
    "delegated": True, "access": scope,
  }]


def test_app_rejection_is_a_tool_error_not_a_transport_failure(
  client, auth, db, monkeypatch,
):
  _app(db)
  _run(db)

  async def fake_invoke(*_args, **_kwargs):
    return 422, {"detail": "friction must not be empty"}, {}, None

  monkeypatch.setattr(app_tools.app_services, "invoke_service", fake_invoke)
  response = client.post(
    "/api/agent/app-tools/call", headers=_agent_auth(db),
    json={"name": "reflection_log_friction", "arguments": {}},
  )

  assert response.json() == {
    "result": "friction must not be empty", "is_error": True,
  }


def test_unknown_or_uninstalled_tool_is_not_found(client, auth, db):
  _app(db, deleted=True)
  _run(db)
  response = client.post(
    "/api/agent/app-tools/call", headers=_agent_auth(db),
    json={"name": "reflection_log_friction", "arguments": {}},
  )
  assert response.status_code == 404


# ── Provider wiring ──────────────────────────────────────────────────────


def test_codex_pre_approves_app_tools_by_exact_name():
  config = codex_turn_mcp_config(
    None, control_enabled=True, app_tool_names=("reflection_log_friction",),
  )
  approvals = config["mcp_servers"]["mobius_control"]["tools"]
  assert approvals["reflection_log_friction"] == {"approval_mode": "approve"}


# ── Activity cards and limits ────────────────────────────────────────────


def test_an_activity_may_be_triggered_by_a_declared_tool():
  validate_manifest_contract(_manifest(agent_activities={
    "lookup": {"tool": "log_friction", "running_label": "Logging"},
  }))
  with pytest.raises(ManifestContractError, match="must name one of the app's"):
    validate_manifest_contract(_manifest(agent_activities={
      "lookup": {"tool": "missing", "running_label": "Logging"},
    }))
  with pytest.raises(ManifestContractError, match="distinct tools"):
    validate_manifest_contract(_manifest(agent_activities={
      "a": {"tool": "log_friction", "running_label": "Logging"},
      "b": {"tool": "log_friction", "running_label": "Logging"},
    }))


def test_tool_calls_run_in_their_own_unserialized_lane(client, auth, db, monkeypatch):
  _app(db)
  _run(db)
  lanes = []

  async def fake_invoke(target, owner, envelope, *, timeout_seconds, lane):
    lanes.append(lane)
    return 200, "ok", {}, None

  monkeypatch.setattr(app_tools.app_services, "invoke_service", fake_invoke)
  client.post(
    "/api/agent/app-tools/call", headers=_agent_auth(db),
    json={"name": "reflection_log_friction", "arguments": {}},
  )
  assert lanes == ["tools"]


def test_every_layer_waits_longer_than_the_one_inside_it():
  from app import platform_tools

  control = platform_tools.claude_control_servers(enabled=True)["mobius_control"]
  codex = codex_turn_mcp_config(None, control_enabled=True)["mcp_servers"]["mobius_control"]
  assert app_tools.TOOL_TIMEOUT_SECONDS >= 300
  assert platform_tools.CONTROL_TOOL_TIMEOUT_SECONDS > app_tools.TOOL_TIMEOUT_SECONDS
  assert codex["tool_timeout_sec"] == platform_tools.CONTROL_TOOL_TIMEOUT_SECONDS
  assert control["timeout"] == platform_tools.CONTROL_TOOL_TIMEOUT_SECONDS * 1000


def test_every_app_response_shows_the_reviewed_tools(client, auth, db):
  app = _app(db)
  shown = client.get(f"/api/apps/{app.id}", headers=auth).json()
  assert shown["agent_tools"] == [LOG_TOOL]
  listed = {row["id"]: row for row in client.get("/api/apps/", headers=auth).json()}
  assert listed[app.id]["agent_tools"] == [LOG_TOOL]
