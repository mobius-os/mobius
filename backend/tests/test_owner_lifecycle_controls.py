"""Delegated-agent boundaries for owner-confirmed lifecycle controls."""

from __future__ import annotations

import hashlib
import uuid

from app import auth as auth_mod, models
from app.delegations import RunPolicy, delegation_execution_token
from app.timeutil import now_naive_utc


def _create_chat(client, owner_auth, title: str) -> str:
  response = client.post("/api/chats", json={"title": title}, headers=owner_auth)
  assert response.status_code == 200, response.text
  return response.json()["id"]


def _replace_transcript(chat_id: str, messages: list[dict]) -> None:
  from app.chat_writer import ReplaceTranscript, get_writer

  ack = get_writer().submit(ReplaceTranscript(
    chat_id=chat_id, run_token="", messages=messages,
  ))
  assert ack.result(timeout=30) is True


def _append_pending(chat_id: str, message: dict) -> None:
  from app.chat_writer import AppendPending, get_writer

  ack = get_writer().submit(AppendPending(
    chat_id=chat_id,
    run_token="lifecycle-boundary-fixture",
    user_msg=message,
  ))
  result = ack.result(timeout=30)
  assert result["stored"]["cid"] == message["cid"]


def test_owner_input_gate_keeps_plain_owner_and_exact_chat_embed(
  db, owner_token,
):
  from app.deps import Principal, require_owner_input_principal

  owner = db.query(models.Owner).one()
  require_owner_input_principal(Principal(owner=owner, app_id=None))
  require_owner_input_principal(Principal(
    owner=owner,
    app_id=42,
    scope="chat_embed",
    chat_id="exact-chat",
    embed_instance_id="exact-frame",
  ))


def _delegated_and_top_level_auth(client, owner_token, db):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_ids = {
    name: _create_chat(client, owner_auth, name)
    for name in ("child", "parent", "foreign", "top-level")
  }
  app = models.App(
    name="Lifecycle boundary", description="", slug="lifecycle-boundary-app",
    source_dir="/tmp/mobius-tests/lifecycle-boundary-app",
    jsx_source="export default () => null",
    token_nonce="lifecycle-boundary-nonce",
  )
  db.add(app)
  db.flush()
  policy = RunPolicy(
    delegation_id="lifecycle-boundary-delegation", app_id=app.id,
    provider="codex", model=None, effort=None, scope="write", cwd="/data",
  )
  db.add(models.Delegation(
    id=policy.delegation_id,
    app_id=app.id,
    parent_chat_id=chat_ids["parent"],
    parent_root_run_id="lifecycle-boundary-parent-root",
    task_key="lifecycle-boundary",
    child_chat_id=chat_ids["child"],
    provider=policy.provider,
    model=policy.model,
    effort=policy.effort,
    scope=policy.scope,
    cwd=policy.cwd,
    prompt_sha256=hashlib.sha256(b"check lifecycle boundary").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="lifecycle-boundary-child-run",
    root_run_id="lifecycle-boundary-child-run",
    chat_id=chat_ids["child"],
    status="running",
    provider="codex",
  ))
  db.add(models.ChatRun(
    id="lifecycle-boundary-top-run",
    root_run_id="lifecycle-boundary-top-run",
    chat_id=chat_ids["top-level"],
    status="running",
    provider="codex",
  ))
  db.commit()

  owner = db.query(models.Owner).one()
  delegated_token = delegation_execution_token(
    db, policy, run_id="lifecycle-boundary-child-run",
  )
  top_level_token = auth_mod.create_agent_token(
    chat_ids["top-level"],
    "lifecycle-boundary-top-run",
    owner.username,
    owner.token_epoch,
  )
  return (
    chat_ids,
    {"Authorization": f"Bearer {delegated_token}"},
    {"Authorization": f"Bearer {top_level_token}"},
  )


def test_delegated_bearer_cannot_enter_chat_lifecycle_controls(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_ids, delegated_auth, _ = _delegated_and_top_level_auth(
    client, owner_token, db,
  )

  provider_switch = {
    "switch_id": "delegated-switch",
    "provider": "codex",
    "agent_settings_json": {"model": "gpt-5.4", "effort": "high"},
  }
  for target in ("child", "parent", "foreign"):
    chat_id = chat_ids[target]
    responses = [
      client.put(
        f"/api/chats/{chat_id}",
        json={"messages": [{"role": "user", "content": "replace"}]},
        headers=delegated_auth,
      ),
      client.patch(
        f"/api/chats/{chat_id}",
        json={
          "provider": "codex",
          "agent_settings_json": {"model": "gpt-5.4", "effort": "high"},
          "auto_resume_on_limit": True,
        },
        headers=delegated_auth,
      ),
      client.post(
        f"/api/chats/{chat_id}/provider-switch",
        json=provider_switch,
        headers=delegated_auth,
      ),
      client.post(f"/api/chats/{chat_id}/compact", headers=delegated_auth),
      client.delete(f"/api/chats/{chat_id}", headers=delegated_auth),
    ]
    assert [response.status_code for response in responses] == [403] * 5

  deleted_id = str(uuid.uuid4())
  deleted = models.Chat(
    id=deleted_id,
    title="Owner-deleted chat",
    provider="claude",
    deleted_at=now_naive_utc(),
  )
  db.add(deleted)
  db.commit()
  recovery = client.post(
    f"/api/chats/{deleted_id}/recover", headers=delegated_auth,
  )
  assert recovery.status_code == 403, recovery.text

  db.expire_all()
  assert db.get(models.Chat, deleted_id).deleted_at is not None
  for target in ("child", "parent", "foreign"):
    row = db.get(models.Chat, chat_ids[target])
    assert row.deleted_at is None
    assert not any(
      message.get("content") == "replace"
      for message in row.messages
      if isinstance(message, dict)
    )

  # Plain owner behavior remains unchanged on the same protected recovery path.
  recovered = client.post(f"/api/chats/{deleted_id}/recover", headers=owner_auth)
  assert recovered.status_code == 200, recovered.text


def test_delegated_bearer_cannot_impersonate_owner_input(
  client, owner_token, db,
):
  from app import secure_inputs

  chat_ids, delegated_auth, _top_level_auth = _delegated_and_top_level_auth(
    client, owner_token, db,
  )
  parent_id = chat_ids["parent"]
  _replace_transcript(parent_id, [{
    "role": "assistant",
    "blocks": [{
      "type": "question",
      "question_id": "owner-choice",
      "questions": [{"id": "confirm", "question": "Proceed?"}],
    }],
  }])

  answered = client.post(
    f"/api/chats/{parent_id}/question-answers",
    json={"question_id": "owner-choice", "answers": {"confirm": "yes"}},
    headers=delegated_auth,
  )
  assert answered.status_code == 403, answered.text
  answered_through_send = client.post(
    f"/api/chats/{parent_id}/messages",
    json={
      "content": "Proceed",
      "hidden": True,
      "question_id": "owner-choice",
      "answers": {"confirm": "yes"},
    },
    headers=delegated_auth,
  )
  assert answered_through_send.status_code == 403, answered_through_send.text

  created_card = client.post(
    f"/api/secure-inputs/{parent_id}",
    json={
      "mode": "sealed",
      "title": "Private value",
      "fields": [{"name": "value", "label": "Value", "type": "text"}],
    },
    headers=delegated_auth,
  )
  assert created_card.status_code == 403, created_card.text

  pending, capability = secure_inputs.create_request(
    chat_id=parent_id,
    mode="sealed",
    title="Private value",
    description="",
    fields=[{
      "name": "value", "label": "Value", "type": "text",
      "autocomplete": "off",
    }],
  )
  supplied = client.post(
    f"/api/secure-inputs/{parent_id}/{pending.request_id}/submit",
    json={"fields": {"value": "child-authored"}},
    headers=delegated_auth,
  )
  assert supplied.status_code == 403, supplied.text
  assert pending.status == "pending"
  assert pending.values is None

  # The capability-authenticated local helper still owns cancellation; the
  # delegated-owner gate does not widen into that exact, one-way control plane.
  cancelled = client.post(
    f"/api/secure-inputs/{pending.request_id}/cancel",
    json={"capability": capability},
  )
  assert cancelled.status_code == 200, cancelled.text
  assert cancelled.json()["status"] == "cancelled"

  db.expire_all()
  question = db.get(models.Chat, parent_id).messages[0]["blocks"][0]
  assert "answers" not in question


def test_delegated_bearer_cannot_send_edit_or_cancel_owner_messages(
  client, owner_token, db,
):
  from app.broadcast import create_broadcast

  chat_ids, delegated_auth, _top_level_auth = _delegated_and_top_level_auth(
    client, owner_token, db,
  )
  for target in ("child", "parent", "foreign"):
    _append_pending(chat_ids[target], {
      "role": "user",
      "content": f"queued-{target}",
      "cid": f"pending-{target}",
    })
    create_broadcast(chat_ids[target])

  for target in ("child", "parent", "foreign"):
    chat_id = chat_ids[target]
    sent = client.post(
      f"/api/chats/{chat_id}/messages",
      json={"content": f"forged-{target}", "cid": f"forged-{target}"},
      headers=delegated_auth,
    )
    edited = client.patch(
      f"/api/chats/{chat_id}/pending/pending-{target}",
      json={"content": f"edited-{target}"},
      headers=delegated_auth,
    )
    cancelled = client.delete(
      f"/api/chats/{chat_id}/pending/pending-{target}",
      headers=delegated_auth,
    )
    assert [sent.status_code, edited.status_code, cancelled.status_code] == [
      403, 403, 403,
    ]

  db.expire_all()
  for target in ("child", "parent", "foreign"):
    pending = db.get(models.Chat, chat_ids[target]).pending_messages
    assert len(pending) == 1
    assert pending[0]["content"] == f"queued-{target}"
    assert pending[0]["cid"] == f"pending-{target}"


def test_delegated_bearer_cannot_enter_host_or_platform_lifecycle(
  client, owner_token, db, monkeypatch, tmp_path,
):
  from app import deployment_control
  from app.routes import admin as admin_routes
  from app.routes import platform as platform_routes

  _chat_ids, delegated_auth, _ = _delegated_and_top_level_auth(
    client, owner_token, db,
  )
  app_id = db.query(models.App).filter_by(
    slug="lifecycle-boundary-app",
  ).one().id
  calls = []

  async def restart(*args, **kwargs):
    calls.append(("restart", args, kwargs))

  async def prepare(cutover_id):
    calls.append(("prepare-cutover", cutover_id))
    return {"status": "prepared"}

  async def rebuild():
    calls.append(("rebuild",))
    return {"state": "queued"}

  async def reviewed_rebuild(**kwargs):
    calls.append(("reviewed-rebuild", kwargs))
    return {"state": "queued"}

  async def apply(_db, **kwargs):
    calls.append(("apply", kwargs))
    return {"state": "restart_needed"}

  monkeypatch.setattr(admin_routes, "restart_this_worker", restart)
  monkeypatch.setattr(admin_routes, "prepare_container_cutover", prepare)
  monkeypatch.setattr(platform_routes, "restart_this_worker", restart)
  monkeypatch.setattr(deployment_control, "request_rebuild", rebuild)
  monkeypatch.setattr(
    deployment_control, "request_reviewed_rebuild", reviewed_rebuild,
  )
  monkeypatch.setattr(
    deployment_control,
    "replacement_ready_path",
    lambda _operation_id: tmp_path / "ready",
  )
  monkeypatch.setattr(
    "app.routes.platform.platform_update.apply_platform_update", apply,
  )
  monkeypatch.setattr(
    "app.restart_ledger.authorized_cutover_challenge", lambda _value: True,
  )

  plan = {
    "plan_id": "a" * 64,
    "current_sha": "1" * 40,
    "target_sha": "2" * 40,
    "image_digest": "sha256:" + "d" * 64,
  }
  requests = [
    client.post("/api/admin/sign-out-everywhere", headers=delegated_auth),
    client.post("/api/admin/restart", headers=delegated_auth),
    client.post(
      "/api/admin/restart/prepare-cutover",
      json={"cutover_id": "cutover-12345678"},
      headers=delegated_auth,
    ),
    client.post("/api/admin/rebuild", headers=delegated_auth),
    client.post(
      "/api/admin/rebuild/prepare",
      json={"operation_id": "operation-12345678"},
      headers=delegated_auth,
    ),
    client.post("/api/platform/apply", json=plan, headers=delegated_auth),
    client.post("/api/platform/rebuild", json=plan, headers=delegated_auth),
    client.post(
      "/api/platform/conflict-resolver-chat", headers=delegated_auth,
    ),
    client.post("/api/platform/restart", headers=delegated_auth),
    client.post("/api/settings", json={}, headers=delegated_auth),
    client.post(
      "/api/auth/install-pass",
      json={"slug": "lifecycle-boundary-app"},
      headers=delegated_auth,
    ),
    client.post(
      "/api/auth/app-token", json={"app_id": app_id},
      headers=delegated_auth,
    ),
    client.post(
      "/api/auth/app-job-token", json={"app_id": app_id},
      headers=delegated_auth,
    ),
    client.post("/api/auth/provider/login", headers=delegated_auth),
    client.post(
      "/api/auth/provider/code", json={"code": "unused"},
      headers=delegated_auth,
    ),
    client.post("/api/auth/provider/codex/login", headers=delegated_auth),
    client.post("/api/auth/shell-install-pass", headers=delegated_auth),
    client.post(
      "/api/auth/shell-install-pass/revoke", headers=delegated_auth,
    ),
  ]

  assert [response.status_code for response in requests] == [403] * len(requests)
  assert calls == []
  db.expire_all()
  assert db.query(models.Owner).one().token_epoch == 0


def test_plain_owner_and_top_level_agent_keep_lifecycle_control(
  client, owner_token, db, monkeypatch,
):
  from app.routes import admin as admin_routes

  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_ids, _delegated_auth, top_level_auth = _delegated_and_top_level_auth(
    client, owner_token, db,
  )
  app_id = db.query(models.App).filter_by(
    slug="lifecycle-boundary-app",
  ).one().id
  calls = []

  async def restart():
    calls.append("restart")

  monkeypatch.setattr(admin_routes, "restart_this_worker", restart)

  for headers in (owner_auth, top_level_auth):
    response = client.post("/api/admin/restart", headers=headers)
    assert response.status_code == 200, response.text
  assert calls == ["restart", "restart"]

  settings = client.post("/api/settings", json={}, headers=top_level_auth)
  assert settings.status_code == 200, settings.text
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=top_level_auth,
  )
  assert app_token.status_code == 200, app_token.text

  replacement = client.put(
    f"/api/chats/{chat_ids['foreign']}",
    json={"messages": [{"role": "user", "content": "owner-approved"}]},
    headers=top_level_auth,
  )
  assert replacement.status_code == 200, replacement.text
  db.expire_all()
  assert db.get(models.Chat, chat_ids["foreign"]).messages == [
    {"role": "user", "content": "owner-approved"}
  ]

  from app.broadcast import create_broadcast

  foreign = db.get(models.Chat, chat_ids["foreign"])
  foreign.provider = "codex"
  foreign.agent_settings_json = {"model": "gpt-5.6-sol"}
  db.commit()
  create_broadcast(chat_ids["foreign"])
  visible_message = client.post(
    f"/api/chats/{chat_ids['foreign']}/messages",
    json={"content": "top-level agent follow-up", "cid": "top-level-send"},
    headers=top_level_auth,
  )
  assert visible_message.status_code == 202, visible_message.text

  blocked_answer = client.post(
    f"/api/chats/{chat_ids['foreign']}/messages",
    json={
      "content": "yes",
      "hidden": True,
      "answers": {"confirm": "yes"},
      "question_id": "not-the-agent's-choice",
    },
    headers=top_level_auth,
  )
  assert blocked_answer.status_code == 403, blocked_answer.text

  create_broadcast(chat_ids["top-level"])
  secure_card = client.post(
    f"/api/secure-inputs/{chat_ids['top-level']}",
    json={
      "mode": "sealed",
      "title": "Top-level request",
      "fields": [{"name": "value", "label": "Value", "type": "text"}],
    },
    headers=top_level_auth,
  )
  assert secure_card.status_code == 200, secure_card.text
  request_id = secure_card.json()["request_id"]
  agent_supply = client.post(
    f"/api/secure-inputs/{chat_ids['top-level']}/{request_id}/submit",
    json={"fields": {"value": "agent-value"}},
    headers=top_level_auth,
  )
  assert agent_supply.status_code == 403, agent_supply.text
  owner_supply = client.post(
    f"/api/secure-inputs/{chat_ids['top-level']}/{request_id}/submit",
    json={"fields": {"value": "owner-value"}},
    headers=owner_auth,
  )
  assert owner_supply.status_code == 200, owner_supply.text
