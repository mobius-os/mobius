"""Security contract for transient-value input and sealed consumption."""

import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def _create_request(client, auth, chat, *, mode="sealed"):
  from app.broadcast import create_broadcast

  bc = create_broadcast(chat.id)
  response = client.post(
    f"/api/secure-inputs/{chat.id}",
    headers=auth,
    json={
      "mode": mode,
      "title": "Private connection",
      "description": "Values bypass model context.",
      "fields": [
        {"name": "username", "label": "Username", "type": "text"},
        {"name": "password", "label": "Password", "type": "password"},
      ],
    },
  )
  assert response.status_code == 200
  created = response.json()
  assert "expires_in" not in created
  return bc, created


def test_sealed_values_never_enter_events_status_or_chat(
  client, auth, chat, db,
):
  from app import secure_inputs

  username = "private-owner@example.test"
  password = "Correct Horse Battery Staple 779!"
  bc, created = _create_request(client, auth, chat)
  request_id = created["request_id"]
  capability = created["capability"]

  public_wire = json.dumps(bc.event_log)
  assert username not in public_wire
  assert password not in public_wire
  assert "capability" not in public_wire
  assert bc.event_log[-1]["fields"] == [
    {"name": "username", "label": "Username", "type": "text", "autocomplete": "off"},
    {"name": "password", "label": "Password", "type": "password", "autocomplete": "off"},
  ]

  submitted = client.post(
    f"/api/secure-inputs/{chat.id}/{request_id}/submit",
    headers=auth,
    json={"fields": {"username": username, "password": password}},
  )
  assert submitted.status_code == 200
  pending = secure_inputs.get_request(request_id)
  assert pending.status == "filled"

  wrong = client.post(
    f"/api/secure-inputs/{request_id}/wait",
    json={"capability": "wrong-capability"},
  )
  assert wrong.status_code == 404
  ready = client.post(
    f"/api/secure-inputs/{request_id}/wait",
    json={"capability": capability},
  )
  assert ready.json() == {"status": "filled", "result": None}
  assert username not in ready.text
  assert password not in ready.text

  consumed = client.post(
    f"/api/secure-inputs/{request_id}/consume",
    json={"capability": capability},
  )
  assert consumed.status_code == 200
  assert consumed.json()["fields"] == {
    "username": username,
    "password": password,
  }
  assert secure_inputs.get_request(request_id).values is None
  second = client.post(
    f"/api/secure-inputs/{request_id}/consume",
    json={"capability": capability},
  )
  assert second.status_code == 409
  assert username not in second.text
  assert password not in second.text

  settled = client.post(
    f"/api/secure-inputs/{request_id}/settle",
    json={
      "capability": capability,
      "ok": True,
      "message": "Connection updated.",
    },
  )
  assert settled.status_code == 200
  assert username not in json.dumps(bc.event_log)
  assert password not in json.dumps(bc.event_log)
  db.refresh(chat)
  assert chat.messages == []
  assert chat.pending_messages == []


def test_pending_secure_input_projects_one_generic_owner_input_state(
  client, auth, chat, monkeypatch,
):
  from app import secure_inputs

  owner_input_events = []
  monkeypatch.setattr(
    secure_inputs,
    "publish_owner_input_changed",
    lambda chat_id, input_kind: owner_input_events.append({
      "chat_id": chat_id,
      "input_kind": input_kind,
    }),
  )

  _, created = _create_request(client, auth, chat)
  assert owner_input_events == [{
    "chat_id": chat.id,
    "input_kind": "secure_input",
  }]
  assert secure_inputs.pending_chat_ids() == frozenset({chat.id})

  listed = client.get("/api/chats", headers=auth)
  row = next(item for item in listed.json() if item["id"] == chat.id)
  assert row["owner_input_kind"] == "secure_input"
  assert row["pending_question_id"] is None
  assert "capability" not in json.dumps(row)

  submitted = client.post(
    f"/api/secure-inputs/{chat.id}/{created['request_id']}/submit",
    headers=auth,
    json={
      "fields": {
        "username": "private-owner@example.test",
        "password": "still-never-list-this",
      },
    },
  )
  assert submitted.status_code == 200
  assert owner_input_events[-1] == {
    "chat_id": chat.id,
    "input_kind": None,
  }
  assert secure_inputs.pending_chat_ids() == frozenset()

  listed = client.get("/api/chats", headers=auth)
  row = next(item for item in listed.json() if item["id"] == chat.id)
  assert row["owner_input_kind"] is None

  # Settlement after submission does not repeat the already-cleared shell
  # transition and trigger another cache reconciliation.
  secure_inputs.cancel_request(secure_inputs.get_request(created["request_id"]))
  assert [event["input_kind"] for event in owner_input_events] == [
    "secure_input", None,
  ]

  # Cancelling while the card itself is still pending does clear the marker.
  _, second = _create_request(client, auth, chat)
  cancelled = client.post(
    f"/api/secure-inputs/{second['request_id']}/cancel",
    json={"capability": second["capability"]},
  )
  assert cancelled.status_code == 200
  assert [event["input_kind"] for event in owner_input_events] == [
    "secure_input", None, "secure_input", None,
  ]


def test_failed_prompt_publish_does_not_leave_an_invisible_open_request(
  client, auth, chat, monkeypatch,
):
  from app import secure_inputs
  from app.broadcast import create_broadcast

  create_broadcast(chat.id)
  monkeypatch.setattr(secure_inputs, "get_broadcast", lambda _chat_id: None)

  response = client.post(
    f"/api/secure-inputs/{chat.id}",
    headers=auth,
    json={
      "mode": "sealed",
      "title": "Private connection",
      "description": "Values bypass model context.",
      "fields": [{
        "name": "password",
        "label": "Password",
        "type": "password",
      }],
    },
  )

  assert response.status_code == 503
  assert secure_inputs.pending_chat_ids() == frozenset()


def test_sink_builds_a_persistable_prompt_only_receipt(client, auth, chat):
  from app.broadcast import create_broadcast
  from app.chat_event_sink import (
    ChatEventSink, register_active_sink, unregister_active_sink,
  )
  from app.events import build_assistant_message
  from app.memory_recall import EMPTY_RECALL_BINDING

  username = "receipt-private-owner@example.test"
  password = "receipt-private-password-8831"
  bc = create_broadcast(chat.id)
  sink = ChatEventSink(
    bc, chat.id, recall_binding=EMPTY_RECALL_BINDING,
  )
  register_active_sink(chat.id, sink)
  try:
    response = client.post(
      f"/api/secure-inputs/{chat.id}",
      headers=auth,
      json={
        "mode": "sealed",
        "title": "Update sign-in",
        "description": "Values bypass model context.",
        "fields": [
          {"name": "username", "label": "Username", "type": "text"},
          {"name": "password", "label": "Password", "type": "password"},
        ],
      },
    )
    assert response.status_code == 200
    created = response.json()
    request_id = created["request_id"]

    submitted = client.post(
      f"/api/secure-inputs/{chat.id}/{request_id}/submit",
      headers=auth,
      json={"fields": {"username": username, "password": password}},
    )
    assert submitted.status_code == 200
    consumed = client.post(
      f"/api/secure-inputs/{request_id}/consume",
      json={"capability": created["capability"]},
    )
    assert consumed.status_code == 200
    consumed.json()["fields"].clear()
    settled = client.post(
      f"/api/secure-inputs/{request_id}/settle",
      json={
        "capability": created["capability"],
        "ok": True,
        "message": "Sign-in updated.",
      },
    )
    assert settled.status_code == 200

    message = build_assistant_message(sink.assistant_blocks)
    wire = json.dumps(message)
    assert username not in wire
    assert password not in wire
    assert created["capability"] not in wire
    receipt = next(
      block for block in message["blocks"]
      if block.get("type") == "secure_input"
    )
    assert receipt == {
      "type": "secure_input",
      "request_id": request_id,
      "mode": "sealed",
      "title": "Update sign-in",
      "description": "Values bypass model context.",
      "fields": [
        {
          "name": "username",
          "label": "Username",
          "type": "text",
          "autocomplete": "off",
        },
        {
          "name": "password",
          "label": "Password",
          "type": "password",
          "autocomplete": "off",
        },
      ],
      "status": "completed",
    }
  finally:
    unregister_active_sink(chat.id, sink)


def test_receipt_reducer_whitelists_metadata_even_from_malformed_event():
  from app.events import build_assistant_message, finalize_blocks, process_event

  secret = "must-not-cross-receipt-boundary"
  blocks = []
  assert process_event({
    "type": "secure_input_request",
    "request_id": "receipt-1",
    "mode": "sealed",
    "title": "Private prompt",
    "description": "Prompt metadata only.",
    "fields": [{
      "name": "password",
      "label": "Password",
      "type": "password",
      "autocomplete": "off",
      "value": secret,
    }],
    "values": {"password": secret},
  }, blocks)
  assert process_event({
    "type": "secure_input_settled",
    "request_id": "receipt-1",
    "status": "completed",
    "result": {"secret": secret},
  }, blocks)

  persisted = json.dumps(build_assistant_message(blocks))
  assert secret not in persisted
  assert "value" not in blocks[0]["fields"][0]
  assert "values" not in blocks[0]
  assert "result" not in blocks[0]
  assert not process_event({
    "type": "secure_input_filled",
    "request_id": "receipt-1",
  }, blocks)
  assert blocks[0]["status"] == "completed"

  pending = [{
    "type": "secure_input",
    "request_id": "interrupted-receipt",
    "fields": [],
    "status": "pending",
  }]
  finalize_blocks(pending)
  assert pending[0]["status"] == "expired"


def test_reveal_requires_card_confirmation_and_redacts_mobius_copy(
  client, auth, chat,
):
  from app.secure_inputs import (
    REVEAL_END, REVEAL_REDACTION, build_reveal_envelope,
    redact_reveal_markers,
  )

  # A submitted value may contain marker-looking text. The paired random nonce
  # must keep that text from ending the scrub envelope early.
  secret = f"debug-only{REVEAL_END}{'0' * 32}>>>private-value"
  _, created = _create_request(client, auth, chat, mode="reveal")
  request_id = created["request_id"]
  rejected = client.post(
    f"/api/secure-inputs/{chat.id}/{request_id}/submit",
    headers=auth,
    json={
      "fields": {"username": "private", "password": secret},
      "reveal_confirmed": False,
    },
  )
  assert rejected.status_code == 400
  assert secret not in rejected.text

  accepted = client.post(
    f"/api/secure-inputs/{chat.id}/{request_id}/submit",
    headers=auth,
    json={
      "fields": {"username": "private", "password": secret},
      "reveal_confirmed": True,
    },
  )
  assert accepted.status_code == 200

  envelope = build_reveal_envelope(secret)
  raw = f"before\n{envelope}\nafter"
  redacted = redact_reveal_markers(raw)
  assert secret not in redacted
  assert REVEAL_REDACTION in redacted
  assert redacted.startswith("before")
  assert redacted.endswith("after")

  truncated = redact_reveal_markers(f"x{envelope[:-12]}")
  assert secret not in truncated
  assert truncated.endswith(REVEAL_REDACTION)


def test_reveal_marker_is_scrubbed_before_sink_broadcast_and_reduction():
  from app.chat_event_sink import ChatEventSink
  from app.memory_recall import EMPTY_RECALL_BINDING
  from app.secure_inputs import REVEAL_REDACTION, build_reveal_envelope

  class Bus:
    def __init__(self):
      self.events = []

    def publish(self, event):
      self.events.append(dict(event))

  secret = "provider-only-debug-value"
  bus = Bus()
  sink = ChatEventSink(
    bus, "", recall_binding=EMPTY_RECALL_BINDING,
  )
  sink.publish({
    "type": "tool_start",
    "tool": "Bash",
    "tool_use_id": "secure-reveal",
  })
  event = {
    "type": "tool_output",
    "content": build_reveal_envelope(secret),
    "tool_use_id": "secure-reveal",
  }
  sink.publish(event)

  wire = json.dumps(bus.events, ensure_ascii=False)
  blocks = json.dumps(sink.assistant_blocks, ensure_ascii=False)
  assert secret not in wire
  assert secret not in blocks
  assert REVEAL_REDACTION in wire
  assert REVEAL_REDACTION in blocks


def test_invalid_submission_never_reflects_secret(client, auth, chat):
  secret = "must-not-echo-even-on-error"
  _, created = _create_request(client, auth, chat)
  response = client.post(
    f"/api/secure-inputs/{chat.id}/{created['request_id']}/submit",
    headers=auth,
    json={"fields": {"unexpected": secret}},
  )
  assert response.status_code == 400
  assert secret not in response.text


def test_cancel_clears_memory_values(client, auth, chat):
  from app import secure_inputs

  _, created = _create_request(client, auth, chat)
  request_id = created["request_id"]
  client.post(
    f"/api/secure-inputs/{chat.id}/{request_id}/submit",
    headers=auth,
    json={"fields": {"username": "u", "password": "p"}},
  )
  assert secure_inputs.get_request(request_id).values is not None
  cancelled = client.post(
    f"/api/secure-inputs/{request_id}/cancel",
    json={"capability": created["capability"]},
  )
  assert cancelled.status_code == 200
  pending = secure_inputs.get_request(request_id)
  assert pending.status == "cancelled"
  assert pending.values is None


def test_pending_request_stays_open_without_an_expiry(monkeypatch):
  from app import secure_inputs

  pending, _ = secure_inputs.create_request(
    chat_id="patient-chat",
    mode="sealed",
    title="No rush",
    description="",
    fields=[{
      "name": "password",
      "label": "Password",
      "type": "password",
      "autocomplete": "off",
    }],
  )

  assert "expires_in" not in pending.public_event()
  monkeypatch.setattr(
    secure_inputs.time,
    "monotonic",
    lambda: pending.created_at + (365 * 24 * 60 * 60),
  )
  secure_inputs._cleanup()

  assert pending.status == "pending"
  assert secure_inputs.get_request(pending.request_id) is pending


@pytest.mark.parametrize("action", ["submit", "cancel", "stop"])
def test_secure_input_wait_parks_without_a_deadline_until_owner_action(
  monkeypatch, action,
):
  from app import secure_inputs
  from app.routes.secure_inputs import wait_for_secure_input

  async def scenario():
    pending, capability = secure_inputs.create_request(
      chat_id="patient-wait-chat",
      mode="sealed",
      title="No rush",
      description="",
      fields=[{"name": "password", "label": "Password", "type": "password"}],
    )

    async def body():
      return {"capability": capability}

    async def timed_wait(*args, **kwargs):
      raise AssertionError("Human input must not use a timed wait")

    monkeypatch.setattr(asyncio, "wait_for", timed_wait)
    waiter = asyncio.create_task(wait_for_secure_input(
      pending.request_id, SimpleNamespace(json=body),
    ))
    try:
      await asyncio.sleep(0)
      assert not waiter.done()
      # Advance the registry's clock, not asyncio's, without waiting a year.
      monkeypatch.setattr(secure_inputs, "time", SimpleNamespace(
        monotonic=lambda: pending.created_at + 365 * 24 * 60 * 60,
      ))
      secure_inputs._cleanup()
      await asyncio.sleep(0)
      assert pending.status == "pending"
      assert not waiter.done()

      if action == "submit":
        secure_inputs.fill_request(pending, {"password": "wait-secret-canary"})
      elif action == "cancel":
        secure_inputs.cancel_request(pending)
      else:
        secure_inputs.cancel_chat(pending.chat_id)

      result = await waiter
      assert result["status"] == ("filled" if action == "submit" else "cancelled")
      assert "wait-secret-canary" not in json.dumps(result)
      assert capability not in json.dumps(result)
      assert secure_inputs.pending_chat_ids() == frozenset()
    finally:
      if not waiter.done():
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
          await waiter

  asyncio.run(scenario())


def test_secure_input_helper_wait_has_no_socket_deadline(monkeypatch):
  script_path = Path(__file__).resolve().parents[1] / "scripts" / "secure-input.py"
  spec = importlib.util.spec_from_file_location("secure_input_helper", script_path)
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)
  monkeypatch.setenv("API_BASE_URL", "http://local.test")
  monkeypatch.setenv("AGENT_TOKEN", "test-token")
  monkeypatch.setenv("CHAT_ID", "patient-chat")

  calls = []
  responses = iter([
    {"request_id": "request-id", "capability": "one-use-capability"},
    {"status": "filled", "result": None},
    {"fields": {"password": "secret-canary"}},
  ])

  class Response:
    status = 200

    def __enter__(self):
      return self

    def __exit__(self, *_args):
      pass

    def read(self):
      return json.dumps(next(responses)).encode()

  def urlopen(request, *, timeout):
    calls.append((request.full_url, timeout))
    return Response()

  monkeypatch.setattr(helper.urllib.request, "urlopen", urlopen)
  request_id, capability, values = helper._request_and_consume({"title": "No rush"})
  assert request_id == "request-id"
  assert capability == "one-use-capability"
  assert values == {"password": "secret-canary"}
  values.clear()
  assert calls == [
    ("http://local.test/api/secure-inputs/patient-chat", 35),
    ("http://local.test/api/secure-inputs/request-id/wait", None),
    ("http://local.test/api/secure-inputs/request-id/consume", 35),
  ]


def test_filled_values_expire_without_another_request(monkeypatch):
  from app import secure_inputs

  monkeypatch.setattr(secure_inputs, "FILLED_TTL_SECONDS", 0.01)

  async def scenario():
    pending, _ = secure_inputs.create_request(
      chat_id="expiry-chat",
      mode="sealed",
      title="Short lived",
      description="",
      fields=[{
        "name": "password",
        "label": "Password",
        "type": "password",
        "autocomplete": "off",
      }],
    )
    secure_inputs.fill_request(pending, {"password": "transient-canary"})
    await asyncio.sleep(0.03)
    assert pending.status == "expired"
    assert pending.values is None

  asyncio.run(scenario())


def test_sealed_helper_saves_and_exits_without_waiting_or_receiving_values(monkeypatch, capsys):
  spec = importlib.util.spec_from_file_location("secure_input_helper", Path(__file__).resolve().parents[1] / "scripts/secure-input.py")
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)
  monkeypatch.setenv("API_BASE_URL", "http://local.test")
  monkeypatch.setenv("AGENT_TOKEN", "test-token")
  monkeypatch.setenv("CHAT_ID", "patient-chat")
  monkeypatch.setattr(sys, "argv", ["secure-input.py", "run", "--title", "Private connection",
    "--field", "password:password:Password", "--", "consumer"])
  calls = []
  receipt = {"state": "waiting_for_owner", "question_id": "saved-1", "next_action": "End now"}
  def post(url, payload, token=None, **kwargs):
    calls.append((url, payload))
    return 200, receipt
  monkeypatch.setattr(helper, "_post", post)
  assert helper.main() == 0
  assert json.loads(capsys.readouterr().out) == receipt
  assert len(calls) == 1 and calls[0][0].endswith("/patient-chat/saved")
  assert calls[0][1]["command"] == ["consumer"]
  assert "environment" not in calls[0][1]
  assert "capability" not in calls[0][1]


def test_sealed_helper_failed_save_does_not_echo_server_or_exception_data(monkeypatch, capsys):
  spec = importlib.util.spec_from_file_location("secure_input_helper", Path(__file__).resolve().parents[1] / "scripts/secure-input.py")
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)
  monkeypatch.setattr(sys, "argv", ["secure-input.py", "run", "--title", "Private connection",
    "--field", "password:password:Password", "--", "consumer"])
  def fail(*args):
    raise RuntimeError("private-untrusted-error-canary")
  monkeypatch.setattr(helper, "_request_saved", fail)
  assert helper.main() == 1
  output = capsys.readouterr().out
  assert "private-untrusted-error-canary" not in output
  assert "save was not confirmed" in output


def test_owner_credentials_consumer_changes_login_without_printing_values(
  owner_token, db, monkeypatch, capsys,
):
  from app import auth, models

  script_path = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "update-owner-credentials.py"
  )
  spec = importlib.util.spec_from_file_location(
    "update_owner_credentials", script_path,
  )
  consumer = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(consumer)

  new_username = "safer-owner"
  new_password = "a private replacement passphrase"
  monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
    "current_password": "testpassword123",
    "new_username": new_username,
    "new_password": new_password,
    "confirm_password": new_password,
  })))

  assert consumer.main() == 0
  output = capsys.readouterr().out
  assert new_username not in output
  assert new_password not in output
  assert output == ""

  db.expire_all()
  owner = db.query(models.Owner).one()
  assert owner.username == new_username
  assert owner.token_epoch == 1
  assert auth.verify_password(new_password, owner.hashed_password)
  assert not auth.verify_password("testpassword123", owner.hashed_password)
