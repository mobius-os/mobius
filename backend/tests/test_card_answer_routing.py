"""On-demand card routing remains opt-in, durable, and one-shot."""

from uuid import uuid4
import importlib.util
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from app import auth as auth_mod, card_answer_routing, models
from app.broadcast import create_broadcast, remove_broadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.chat_writer import StartTurn, get_writer
from app.database import SessionLocal
from app.memory_recall import EMPTY_RECALL_BINDING


def _target(db):
  target = models.Chat(
    id=str(uuid4()), title="Answerer", messages=[],
    agent_settings_json={"model": "claude-opus-4-8"},
  )
  db.add(target)
  db.commit()
  return target


def test_route_requires_opt_in_and_rejects_self(client, chat, db, auth):
  target = _target(db)
  url = f"/api/chats/{chat.id}/card-answerer"
  assert client.get(url, headers=auth).json() == {"answerer_chat_id": None}
  assert client.put(url, json={"answerer_chat_id": chat.id}, headers=auth).status_code == 422
  response = client.put(url, json={"answerer_chat_id": target.id}, headers=auth)
  assert response.status_code == 200, response.text
  assert response.json() == {"answerer_chat_id": target.id}
  assert client.put(url, json={"answerer_chat_id": None}, headers=auth).status_code == 200


def test_agent_can_configure_only_its_own_source_chat(client, chat, db, owner_token):
  other = _target(db)
  answerer = _target(db)
  owner = db.query(models.Owner).first()
  run_id = f"routing-auth-{uuid4()}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Configure answerer", "ts": 1},
  )).result(timeout=5)
  token = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=run_id,
  )
  headers = {"Authorization": f"Bearer {token}"}
  body = {"answerer_chat_id": answerer.id}
  assert client.put(
    f"/api/chats/{other.id}/card-answerer", json=body, headers=headers,
  ).status_code == 403
  assert client.put(
    f"/api/chats/{chat.id}/card-answerer", json=body, headers=headers,
  ).status_code == 200


def test_real_saved_question_is_routable_from_its_durable_marker(
  client, chat, db, auth,
):
  target = _target(db)
  assert client.put(
    f"/api/chats/{chat.id}/card-answerer",
    json={"answerer_chat_id": target.id}, headers=auth,
  ).status_code == 200
  run_id = f"routing-{uuid4()}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Ask a question", "ts": 1},
  )).result(timeout=5)
  sink = ChatEventSink(
    create_broadcast(chat.id), chat.id, run_token=run_id,
    recall_binding=EMPTY_RECALL_BINDING,
  )
  register_active_sink(chat.id, sink)
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=run_id,
    expires_delta=timedelta(minutes=5),
  )
  try:
    response = client.post(
      f"/api/chats/{chat.id}/question",
      headers={"Authorization": f"Bearer {token}"},
      json={"questions": [{
        "id": "next", "header": "Choice", "question": "Which path?",
        "options": [],
      }]},
    )
    assert response.status_code == 200, response.text
    question_id = response.json()["question_id"]
    assert (chat.id, question_id, target.id, target.provider) in (
      card_answer_routing._pending_deliveries()
    )
  finally:
    unregister_active_sink(chat.id, sink)
    remove_broadcast(chat.id)


@pytest.mark.asyncio
async def test_open_card_delivered_once_with_exact_identity_and_no_secret(
  client, chat, db, auth, monkeypatch,
):
  target = _target(db)
  chat.card_answerer_chat_id = target.id
  chat.pending_question_id = "question-1"
  chat.messages = [{
    "role": "assistant", "blocks": [{
      "type": "question", "question_id": "question-1",
      "questions": [{"id": "secure_input", "question": "Supply API key"}],
      "secure_input": {"title": "API key", "fields": [{"name": "token"}]},
    }],
  }]
  db.commit()
  calls = []

  async def fake_start(**kwargs):
    calls.append(kwargs)
    with SessionLocal() as other:
      row = other.get(models.Chat, target.id)
      row.messages = [{"role": "user", "source_work_id": kwargs["source_work_id"]}]
      other.commit()
    return True

  monkeypatch.setattr("app.chat_start.start_programmatic_chat_turn", fake_start)
  target.pending_messages = [{"role": "user", "content": "Earlier queued work"}]
  db.commit()
  await card_answer_routing.sweep_card_answer_routes()
  assert calls == []
  target.pending_messages = []
  db.commit()
  await card_answer_routing.sweep_card_answer_routes()
  await card_answer_routing.sweep_card_answer_routes()
  assert len(calls) == 1
  instruction = calls[0]["content"]
  assert chat.id in instruction and "question-1" in instruction
  assert "secure input" in instruction
  assert "authorized for this specific use" in instruction
  assert "Supply API key" not in instruction
  assert calls[0]["cid"]


def test_authorized_source_submit_helper_keeps_value_out_of_output(
  tmp_path, monkeypatch, capsys,
):
  script = Path(__file__).parents[1] / "scripts/secure-input.py"
  spec = importlib.util.spec_from_file_location("submit_saved_secure_input", script)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  source = tmp_path / "authorized-key"
  source.write_text("known-authorized-value", encoding="utf-8")
  monkeypatch.setenv("API_BASE_URL", "http://localhost")
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setattr("sys.argv", [
    str(script), "submit-saved", "--chat-id", "source-chat", "--request-id", "card-1",
    "--field-file", f"api_key={source}",
  ])
  seen = []

  def fake_post(url, payload, token):
    seen.append((url, deepcopy(payload), token))
    return 200, {"status": "consuming"}

  monkeypatch.setattr(module, "_post", fake_post)
  assert module.main() == 0
  assert seen[0][1]["fields"]["api_key"] == "known-authorized-value"
  assert "known-authorized-value" not in capsys.readouterr().out


def test_submit_helper_does_not_claim_cancelled_card_was_accepted(
  tmp_path, monkeypatch, capsys,
):
  script = Path(__file__).parents[1] / "scripts/secure-input.py"
  spec = importlib.util.spec_from_file_location("secure_input_helper_cancelled", script)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  source = tmp_path / "key"
  source.write_text("private-value", encoding="utf-8")
  monkeypatch.setenv("API_BASE_URL", "http://localhost")
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setattr("sys.argv", [
    str(script), "submit-saved", "--chat-id", "source-chat",
    "--request-id", "card-1", "--field-file", f"api_key={source}",
  ])
  monkeypatch.setattr(module, "_post", lambda *_: (200, {"status": "cancelled"}))
  assert module.main() == 1
  assert "private-value" not in capsys.readouterr().out
