"""Security contract for transient-value input and sealed consumption."""

import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys


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


def test_sealed_consumer_discards_stdout_and_stderr(capsys):
  script_path = (
    Path(__file__).resolve().parents[1] / "scripts" / "secure-input.py"
  )
  spec = importlib.util.spec_from_file_location("secure_input_helper", script_path)
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)

  secret = "private value+/with spaces"
  values = {"password": secret}
  command = [
    sys.executable,
    "-c",
    (
      "import base64,json,sys,urllib.parse; "
      "v=json.load(sys.stdin)['password']; "
      "print(v); print(urllib.parse.quote(v,safe='')); "
      "print(base64.b64encode(v.encode()).decode()); "
      "print(v, file=sys.stderr)"
    ),
  ]
  assert helper._run_consumer(command, values) == 0
  captured = capsys.readouterr()
  assert captured.out == ""
  assert captured.err == ""


def test_sealed_consumer_keeps_the_two_minute_runtime_limit(monkeypatch):
  script_path = (
    Path(__file__).resolve().parents[1] / "scripts" / "secure-input.py"
  )
  spec = importlib.util.spec_from_file_location("secure_input_helper", script_path)
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)

  observed = {}

  def run(command, **kwargs):
    observed["command"] = command
    observed.update(kwargs)
    return type("Completed", (), {"returncode": 0})()

  monkeypatch.setattr(helper.subprocess, "run", run)
  assert helper._run_consumer(["consumer"], {"password": "private"}) == 0
  assert observed["timeout"] == 120
  assert observed["stdout"] is helper.subprocess.DEVNULL
  assert observed["stderr"] is helper.subprocess.DEVNULL


def test_consumer_outcomes_are_predefined():
  script_path = (
    Path(__file__).resolve().parents[1] / "scripts" / "secure-input.py"
  )
  spec = importlib.util.spec_from_file_location("secure_input_helper", script_path)
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)

  assert helper._consumer_outcome("run", 0) == (
    True, 0, "Secure input was consumed without exposing its values.",
  )
  assert helper._consumer_outcome("run", 19) == (
    False, 19, "The sealed consumer failed; submitted values were discarded.",
  )
  assert helper._consumer_outcome("owner-credentials", 5) == (
    False, 1, "Current password is incorrect.",
  )
  assert helper._consumer_outcome("owner-credentials", 73) == (
    False, 1, "Credentials could not be changed.",
  )


def test_sealed_consumer_exception_settles_without_reflecting_values(
  monkeypatch, capsys,
):
  script_path = (
    Path(__file__).resolve().parents[1] / "scripts" / "secure-input.py"
  )
  spec = importlib.util.spec_from_file_location("secure_input_helper", script_path)
  helper = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(helper)

  secret = "consumer-exception-secret"
  monkeypatch.setattr(sys, "argv", [
    "secure-input.py",
    "run",
    "--title", "Private connection",
    "--field", "password:password:Password",
    "--", "consumer",
  ])
  monkeypatch.setattr(
    helper,
    "_request_and_consume",
    lambda _spec: ("request-id", "capability", {"password": secret}),
  )

  def fail_consumer(_command, _values):
    raise RuntimeError(f"consumer failed with {secret}")

  settled = []
  monkeypatch.setattr(helper, "_run_consumer", fail_consumer)
  monkeypatch.setattr(
    helper,
    "_settle",
    lambda request_id, capability, **outcome: settled.append(
      (request_id, capability, outcome),
    ),
  )

  assert helper.main() == 1
  output = capsys.readouterr().out
  assert secret not in output
  assert output == "Secure input failed; submitted values were discarded.\n"
  assert settled == [(
    "request-id",
    "capability",
    {
      "ok": False,
      "message": "The sealed consumer failed; submitted values were discarded.",
    },
  )]


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
