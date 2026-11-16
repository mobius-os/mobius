"""An agent's key request is an ordinary saved owner card in its own chat.

The card waits for the owner like every other card, and it only records an
answer the owner already gave the vault through the owner-only key routes.
"""
from datetime import timedelta

import pytest

from app import auth as auth_mod, models, transcript_rows
from app.broadcast import create_broadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.chat_writer import Barrier, StartTurn, get_writer
from app.database import SessionLocal
from tests.test_keys_routes import DUMMY, nudges, vault  # noqa: F401 (fixtures)

APOD = {"host": "api.nasa.gov", "methods": ["GET"], "paths": ["/planetary/apod"]}
ASK = {"key": "nasa", "reason": "Fetch today's picture", "sites": [APOD],
       "message": "Allow it on the card and I'll fetch it."}


@pytest.fixture
def agent_run(chat, db):
  run_id = f"key-{chat.id}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Try my key", "ts": 1},
  )).result(timeout=5)
  sink = ChatEventSink(create_broadcast(chat.id), chat.id, run_token=run_id)
  register_active_sink(chat.id, sink)
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=run_id,
    expires_delta=timedelta(minutes=5),
  )
  yield {"Authorization": f"Bearer {token}"}
  unregister_active_sink(chat.id, sink)


def _card(chat_id):
  get_writer().submit(Barrier()).result(timeout=5)
  with SessionLocal() as db:
    row = db.get(models.Chat, chat_id)
    blocks = [b for m in transcript_rows.history(row) for b in m.get("blocks") or []]
    return row.pending_question_id, [b for b in blocks if b["type"] == "question"]


def _answer(client, chat, auth, qid, choice):
  label, option = {"allow": ("Allow", "allow"), "deny": ("Not now", "deny")}[choice]
  return client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": f"- key: {label}", "hidden": True, "answers": {"key": label},
    "selected_options": {"key": [option]}, "question_id": qid,
  })


def test_a_new_key_ask_saves_a_waiting_key_card_in_the_agents_chat(client, chat, agent_run, vault, nudges):
  asked = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK)
  assert asked.status_code == 200, asked.text
  receipt = asked.json()
  assert receipt["state"] == "waiting_for_owner"
  assert receipt["answer"] == {"status": "pending", "key": "nasa", "id": "agent-nasa"}
  marker, [card] = _card(chat.id)
  # The same waiting marker every saved card sets, so the chat waits for the owner.
  assert marker == receipt["question_id"] == card["question_id"]
  assert card["key_request"] == {"id": "agent-nasa", "key": "nasa", "reason": ASK["reason"]}
  assert card["message"] == ASK["message"]
  [question] = card["questions"]
  assert question["question"] == "Let your agent send **nasa** to **api.nasa.gov**?"
  assert [option["id"] for option in question["options"]] == ["allow", "deny"]
  assert nudges == [{"type": "key_requests_changed"}]


def test_allow_is_recorded_only_after_the_owner_allowed_it_in_the_vault(client, chat, auth, agent_run, vault):
  qid = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK).json()["question_id"]
  early = _answer(client, chat, auth, qid, "allow")
  assert early.status_code == 409 and "Reload Möbius and answer the card again" in early.text
  assert _card(chat.id)[0] == qid
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  approved = client.post("/api/keys/requests/agent-nasa/approve", headers=auth, json={
    "ticket": request["ticket"], "sites": request["sites"], "value": DUMMY,
    "inject": {"type": "query", "name": "api_key"},
  })
  assert approved.status_code == 200, approved.text
  assert _answer(client, chat, auth, qid, "allow").status_code == 202
  marker, [card] = _card(chat.id)
  assert marker is None and card["answers"] == {"key": "Allow"}


def test_not_now_matches_a_denied_request_and_typed_replies_are_refused(client, chat, auth, agent_run, vault):
  qid = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK).json()["question_id"]
  typed = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "- key: sure", "hidden": True, "answers": {"key": "sure"}, "question_id": qid,
  })
  assert typed.status_code == 409 and "Allow or Not now" in typed.text
  # The vault still waits, so a Not now the owner never gave is refused too.
  assert _answer(client, chat, auth, qid, "deny").status_code == 409
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  denied = client.post("/api/keys/requests/agent-nasa/deny", headers=auth, json={"ticket": request["ticket"]})
  assert denied.status_code == 200, denied.text
  assert _answer(client, chat, auth, qid, "allow").status_code == 409
  assert _answer(client, chat, auth, qid, "deny").status_code == 202


def test_a_key_the_agent_may_already_use_there_answers_without_a_card(client, chat, agent_run, vault):
  vault.put_key("nasa", {"value": DUMMY, "inject": {"type": "query", "name": "api_key"},
                         "grants": {"apps": [], "agents": True, "sites": [APOD]}})
  asked = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK)
  assert asked.status_code == 200, asked.text
  assert asked.json() == {"state": "answered_without_card", "answer": {"status": "granted", "key": "nasa"}}
  marker, cards = _card(chat.id)
  assert marker is None and cards == []


def test_only_the_current_agent_run_asks_by_card(client, chat, auth, vault):
  refused = client.post(f"/api/chats/{chat.id}/key-request", headers=auth, json=ASK)
  assert refused.status_code == 403
