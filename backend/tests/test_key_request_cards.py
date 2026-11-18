"""An agent's key request is an ordinary saved owner card in its own chat.

The card waits for the owner like every other card, and it only records an
answer the owner already gave the vault through the owner-only key routes.
"""
import uuid
from datetime import timedelta

import pytest

from app import auth as auth_mod, chat_writer, models, transcript_rows
from app.broadcast import create_broadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.chat_writer import Barrier, StartTurn, get_writer
from app.database import SessionLocal
from app.timeutil import now_naive_utc
from tests.test_keys_routes import DUMMY, nudges, vault  # noqa: F401 (fixtures)

APOD = {"host": "api.nasa.gov", "methods": ["GET"], "paths": ["/planetary/apod"]}
ASK = {"key": "nasa", "reason": "Fetch today's picture", "sites": [APOD],
       "message": "Allow it on the card and I'll fetch it."}


@pytest.fixture
def start_agent_run(db):
  """Starts an agent turn in a chat; returns that agent's auth headers."""
  sinks = []

  def start(chat):
    run_id = f"key-{chat.id}-{len(sinks)}"
    get_writer().submit(StartTurn(
      chat_id=chat.id, run_token=run_id,
      user_msg={"role": "user", "content": "Try my key", "ts": 1},
    )).result(timeout=5)
    sink = ChatEventSink(create_broadcast(chat.id), chat.id, run_token=run_id)
    register_active_sink(chat.id, sink)
    sinks.append((chat.id, sink))
    owner = db.query(models.Owner).first()
    token = auth_mod.create_agent_token(
      chat_id=chat.id, owner_username=owner.username,
      token_epoch=owner.token_epoch, run_id=run_id,
      expires_delta=timedelta(minutes=5),
    )
    return {"Authorization": f"Bearer {token}"}
  yield start
  for chat_id, sink in sinks:
    unregister_active_sink(chat_id, sink)


@pytest.fixture
def agent_run(chat, start_agent_run):
  return start_agent_run(chat)


@pytest.fixture
def other_chat(db, chat):
  other = chat_writer.create_chat(
    id=str(uuid.uuid4()), title="Other chat", messages=[],
    agent_settings_json={"model": "claude-opus-4-8"},
  )
  db.add(other)
  db.commit()
  db.refresh(other)
  return other


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
  assert receipt["answer"] == {"status": "pending", "key": "nasa", "id": "agent-nasa",
                               "sites": [APOD], "reason": ASK["reason"]}
  marker, [card] = _card(chat.id)
  # The same waiting marker every saved card sets, so the chat waits for the owner.
  assert marker == receipt["question_id"] == card["question_id"]
  assert card["key_request"] == {"id": "agent-nasa", "key": "nasa", "reason": ASK["reason"], "sites": [APOD]}
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


def test_another_chats_waiting_request_never_shows_as_this_agents_card(
  client, chat, other_chat, auth, start_agent_run, vault,
):
  """A request already waiting answers for later asks for its key, so an ask
  that joins another chat's request saves no card here, where it would read
  as this agent's own; the agent hears what is waiting and asks again later."""
  collect = {"host": "collect.example.net", "methods": ["POST"], "paths": ["/*"]}
  waiting = client.post(f"/api/chats/{other_chat.id}/key-request", headers=start_agent_run(other_chat),
                        json={**ASK, "reason": "Upload", "sites": [collect]})
  assert waiting.json()["state"] == "waiting_for_owner"
  asked = client.post(f"/api/chats/{chat.id}/key-request", headers=start_agent_run(chat), json=ASK)
  assert asked.status_code == 200, asked.text
  receipt = asked.json()
  with SessionLocal() as db:
    title = db.get(models.Chat, other_chat.id).title
  assert receipt["state"] == "answered_without_card" and f'the chat "{title}"' in receipt["note"]
  assert receipt["waiting_chat_id"] == other_chat.id
  assert receipt["answer"] == {"status": "pending", "key": "nasa", "id": "agent-nasa",
                               "sites": [collect], "reason": "Upload"}
  marker, cards = _card(chat.id)
  assert marker is None and cards == []


def test_the_agent_hears_which_chat_holds_the_waiting_card(client, chat, other_chat, start_agent_run, vault):
  """The note names where the owner answers: that chat's card, and Settings ›
  Keys, which lists every waiting request."""
  client.post(f"/api/chats/{other_chat.id}/key-request", headers=start_agent_run(other_chat), json=ASK)
  with SessionLocal() as db:
    db.get(models.Chat, other_chat.id).title = "Weather"
    db.commit()
  receipt = client.post(f"/api/chats/{chat.id}/key-request", headers=start_agent_run(chat), json=ASK).json()
  assert receipt["state"] == "answered_without_card"
  assert receipt["waiting_chat_id"] == other_chat.id
  assert 'the chat "Weather"' in receipt["note"] and "Settings › Keys" in receipt["note"]


def _delete_while_the_vault_was_unreachable(chat_id):
  """Tombstone a chat without the vault hearing of it, as when deleting it
  could not reach the vault."""
  with SessionLocal() as db:
    db.get(models.Chat, chat_id).deleted_at = now_naive_utc()
    db.commit()


def test_a_request_a_deleted_chat_left_behind_waits_in_settings(
  client, chat, other_chat, auth, start_agent_run, vault,
):
  """A deleted chat's request the vault never heard about is never stranded:
  Settings › Keys lists it, saying its chat is gone, and the next ask for its
  key is sent there."""
  collect = {"host": "collect.example.net", "methods": ["POST"], "paths": ["/*"]}
  vault.request_for_agents({"key": "nasa", "reason": "Upload", "sites": [collect]}, actor=f"agent:{other_chat.id}")
  _delete_while_the_vault_was_unreachable(other_chat.id)
  receipt = client.post(f"/api/chats/{chat.id}/key-request", headers=start_agent_run(chat), json=ASK).json()
  assert receipt["state"] == "answered_without_card" and receipt["waiting_chat_id"] is None
  assert "in Settings › Keys" in receipt["note"] and "the chat" not in receipt["note"]
  assert _card(chat.id) == (None, [])
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  assert request["chat_id"] == other_chat.id and request["chat_title"] is None


def test_a_request_whose_turn_was_stopped_waits_in_settings_and_its_chat_can_ask_again(
  client, chat, other_chat, auth, start_agent_run, vault,
):
  """Stopping the turn settles its card but not the request: Settings › Keys
  still lists it, under its chat, and an ask from that chat shows it again."""
  asked = client.post(f"/api/chats/{chat.id}/key-request", headers=start_agent_run(chat), json=ASK)
  assert asked.json()["state"] == "waiting_for_owner"
  assert client.post("/api/chat/stop", headers=auth, json={"chat_id": chat.id}).status_code == 200
  assert _card(chat.id)[0] is None
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  with SessionLocal() as db:
    title = db.get(models.Chat, chat.id).title or "Untitled chat"
  assert (request["id"], request["status"], request["chat_title"]) == ("agent-nasa", "pending", title)
  elsewhere = client.post(f"/api/chats/{other_chat.id}/key-request", headers=start_agent_run(other_chat), json=ASK).json()
  assert elsewhere["state"] == "answered_without_card" and "in Settings › Keys" in elsewhere["note"]
  again = client.post(f"/api/chats/{chat.id}/key-request", headers=start_agent_run(chat), json=ASK).json()
  assert again["state"] == "waiting_for_owner"
  assert _card(chat.id)[1][-1]["key_request"]["id"] == "agent-nasa"


def test_a_request_that_names_no_chat_is_answered_in_settings(client, chat, agent_run, vault):
  vault.request_for_agents({"key": "nasa", "reason": "Upload", "sites": [APOD]}, actor="owner")
  receipt = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK).json()
  assert receipt["waiting_chat_id"] is None and "in Settings › Keys" in receipt["note"]


def test_deleting_the_asking_chat_withdraws_its_request_so_another_chat_can_ask(
  client, chat, other_chat, auth, start_agent_run, vault,
):
  """Its card was the only place to answer it, and a waiting request answers
  for every later ask for its key: left behind, no chat could ask again."""
  asked = client.post(f"/api/chats/{chat.id}/key-request", headers=start_agent_run(chat), json=ASK)
  assert asked.json()["state"] == "waiting_for_owner"
  vault.request_for_agents({"key": "maps", "reason": "Map", "sites": [APOD]}, actor=f"agent:{chat.id}")
  vault.deny_request("agent-maps")
  assert client.delete(f"/api/chats/{chat.id}", headers=auth).status_code == 204
  requests = client.get("/api/keys", headers=auth).json()["requests"]
  # The waiting ask is gone; the owner's refusal of another key stands.
  assert [(r["id"], r["status"]) for r in requests] == [("agent-maps", "denied")]
  images = {"host": "images-api.nasa.gov", "methods": ["GET"], "paths": ["/*"]}
  receipt = client.post(f"/api/chats/{other_chat.id}/key-request", headers=start_agent_run(other_chat),
                        json={**ASK, "sites": [images]}).json()
  assert receipt["state"] == "waiting_for_owner"
  [card] = _card(other_chat.id)[1]
  assert card["key_request"]["sites"] == [images]


def test_deleting_a_chat_never_waits_on_a_stopped_vault(client, chat, auth, monkeypatch, vault):
  from app import key_vault

  async def stopped(*args, **kwargs):
    raise key_vault.VaultUnavailable("The key vault is not running.")
  vault.request_for_agents({"key": "nasa", "reason": "x", "sites": [APOD]}, actor=f"agent:{chat.id}")
  monkeypatch.setattr(key_vault, "vault_call", stopped)
  assert client.delete(f"/api/chats/{chat.id}", headers=auth).status_code == 204


def test_asking_again_in_the_same_chat_shows_the_stored_request(client, chat, auth, agent_run, vault):
  """The card is written from the request the vault stored, never from the new ask."""
  client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK)
  other = {**ASK, "reason": "Something else", "sites": [{"host": "images-api.nasa.gov", "methods": ["GET"], "paths": ["/*"]}]}
  receipt = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=other).json()
  [card] = [c for c in _card(chat.id)[1] if c["question_id"] == receipt["question_id"]]
  assert card["questions"][0]["question"] == "Let your agent send **nasa** to **api.nasa.gov**?"
  assert card["key_request"] == {"id": "agent-nasa", "key": "nasa", "reason": ASK["reason"], "sites": [APOD]}


def test_a_request_removed_without_allowing_it_takes_not_now_and_refuses_allow(client, chat, auth, agent_run, vault):
  """Removing a request in Settings grants nothing: the card can still be
  answered, but only truthfully."""
  qid = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK).json()["question_id"]
  assert client.delete("/api/keys/requests/agent-nasa", headers=auth).status_code in (200, 204)
  refused = _answer(client, chat, auth, qid, "allow")
  assert refused.status_code == 409 and "Nothing was allowed for this request" in refused.text
  assert _answer(client, chat, auth, qid, "deny").status_code == 202


def test_a_key_allowed_elsewhere_takes_allow_and_refuses_not_now(client, chat, auth, agent_run, vault):
  """The owner may allow the key on its page instead of on the card."""
  qid = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json=ASK).json()["question_id"]
  vault.put_key("nasa", {"value": DUMMY, "inject": {"type": "query", "name": "api_key"},
                         "grants": {"apps": [], "agents": True, "sites": [APOD]}})
  refused = _answer(client, chat, auth, qid, "deny")
  assert refused.status_code == 409 and "Choose Allow" in refused.text
  assert _answer(client, chat, auth, qid, "allow").status_code == 202


def test_a_reason_longer_than_the_vault_keeps_is_refused_rather_than_cut(client, chat, agent_run, vault):
  asked = client.post(f"/api/chats/{chat.id}/key-request", headers=agent_run, json={**ASK, "reason": "x" * 301})
  assert asked.status_code == 422


def test_deleting_a_project_withdraws_its_chats_waiting_requests(client, auth, vault):
  project = client.post("/api/projects", headers=auth, json={"name": "Site", "template_id": "blank"}).json()
  project_chat = client.post(f"/api/projects/{project['id']}/chats", headers=auth, json={
    "title": "Site plan", "recovery_request_id": f"{project['id']}:Site plan",
  }).json()
  vault.request_for_agents({"key": "nasa", "reason": "x", "sites": [APOD]}, actor=f"agent:{project_chat['id']}")
  assert client.delete(f"/api/projects/{project['id']}", headers=auth).status_code == 204
  assert vault.list_requests() == []


def test_a_project_withdraws_its_chats_requests_together_after_its_lifecycle_lock(
  client, auth, monkeypatch, vault,
):
  """A vault that hangs then costs one timeout, not one per chat, and never
  holds the project's deletion or recovery."""
  import asyncio

  from app import key_vault
  from app.project_retention import serialize_project_lifecycle
  project = client.post("/api/projects", headers=auth, json={"name": "Site", "template_id": "blank"}).json()
  chat_ids = {client.post(f"/api/projects/{project['id']}/chats", headers=auth, json={
    "title": title, "recovery_request_id": f"{project['id']}:{title}",
  }).json()["id"] for title in ("Plan", "Copy")}
  started, released = [], []
  everyone_asked = asyncio.Event()

  async def slow_vault(chat_id, deletion=None):
    started.append(chat_id)
    if len(started) == len(chat_ids):
      everyone_asked.set()
    # Only reachable if no chat's withdrawal waits for another's to finish.
    await asyncio.wait_for(everyone_asked.wait(), timeout=2)
    async with asyncio.timeout(2):
      async with serialize_project_lifecycle(project["id"]):
        released.append(chat_id)
  monkeypatch.setattr(key_vault, "forget_chat", slow_vault)
  assert client.delete(f"/api/projects/{project['id']}", headers=auth).status_code == 204
  assert set(released) == chat_ids


@pytest.mark.parametrize("deleted", ["chat", "project"])
def test_a_withdrawal_reaching_the_vault_after_recovery_leaves_what_the_chat_asks_again(
  client, auth, chat, monkeypatch, start_agent_run, vault, deleted,
):
  """A withdrawal names the deletion's time, and recovery calls nothing. When
  it reaches the vault after the chat is back and has asked again, joining
  its own request, that request stays, and its new card can Allow."""
  from app import key_vault
  late = []

  async def delayed(chat_id, deleted_at):
    late.append((chat_id, deleted_at))
  monkeypatch.setattr(key_vault, "forget_chat", delayed)
  if deleted == "project":
    project = client.post("/api/projects", headers=auth, json={"name": "Site", "template_id": "blank"}).json()
    asker = client.post(f"/api/projects/{project['id']}/chats", headers=auth, json={
      "title": "Site plan", "recovery_request_id": f"{project['id']}:Site plan",
    }).json()["id"]
    target = f"/api/projects/{project['id']}"
  else:
    asker, target = chat.id, f"/api/chats/{chat.id}"
  vault.request_for_agents({"key": "nasa", "reason": "x", "sites": [APOD]}, actor=f"agent:{asker}")
  assert client.delete(target, headers=auth).status_code == 204

  async def no_vault_on_recovery(*args, **kwargs):
    raise AssertionError("recovery called the vault")
  with monkeypatch.context() as quiet:
    quiet.setattr(key_vault, "vault_call", no_vault_on_recovery)
    assert client.post(f"{target}/recover", headers=auth).status_code == 200
  with SessionLocal() as db:
    recovered = db.get(models.Chat, asker)
  qid = client.post(f"/api/chats/{asker}/key-request", headers=start_agent_run(recovered),
                    json={**ASK, "sites": [APOD], "reason": "x"}).json()["question_id"]
  [(chat_id, deleted_at)] = late
  # The withdrawal lands now, after the chat is back and asked again.
  assert vault.forget_chat(chat_id, before=deleted_at.isoformat()) == 0
  assert [r["id"] for r in vault.list_requests()] == ["agent-nasa"]
  assert client.post("/api/keys/requests/agent-nasa/approve", headers=auth, json={
    "value": DUMMY, "sites": [APOD], "ticket": vault.list_requests()[0]["ticket"],
  }).status_code == 200
  with SessionLocal() as db:
    owner_chat = db.get(models.Chat, asker)
  assert _answer(client, owner_chat, auth, qid, "allow").status_code == 202


def test_a_card_that_could_not_be_saved_leaves_no_request_behind(client, chat, other_chat, start_agent_run, vault):
  """The vault records the ask before the card is saved. If the turn stopped
  in between, no card can answer the request, so it is withdrawn, and
  another chat's ask gets its own card rather than a pointer to none."""
  from app.chat_event_sink import get_active_sink
  headers = start_agent_run(chat)
  unregister_active_sink(chat.id, get_active_sink(chat.id))
  stale = client.post(f"/api/chats/{chat.id}/key-request", headers=headers, json=ASK)
  assert stale.status_code == 409, stale.text
  assert _card(chat.id) == (None, []) and vault.list_requests() == []
  receipt = client.post(f"/api/chats/{other_chat.id}/key-request", headers=start_agent_run(other_chat), json=ASK).json()
  assert receipt["state"] == "waiting_for_owner"
  assert [r["chat_id"] for r in vault.list_requests()] == [other_chat.id]


def test_a_card_whose_save_failed_midway_leaves_no_request_behind(client, chat, monkeypatch, start_agent_run, vault):
  """A save that fails while publishing (an uncertain save) withdraws the
  request only when the chat shows no card for it."""
  from app.chat_event_sink import ChatEventSink
  headers = start_agent_run(chat)

  async def broken(self, *args, **kwargs):
    raise RuntimeError("disk full")
  monkeypatch.setattr(ChatEventSink, "publish_question", broken)
  failed = client.post(f"/api/chats/{chat.id}/key-request", headers=headers, json=ASK)
  assert failed.status_code == 503, failed.text
  assert vault.list_requests() == []
