"""Stop only interrupts: the owner's queued messages wait for their next send.

Stop holds the queue instead of clearing it, so nothing the owner queued can
be lost (no client resend can be refused or dropped) and nothing starts behind
their back. An empty send runs the whole queue; a new message runs first and
the queue follows it.
"""
import asyncio

import pytest

from app import chat as chat_mod
from app import chat_queue, models


OWNER = {"_owner_input_at": "2020-01-01T10:00:00+00:00"}


def queue(db, chat, *rows):
  chat.pending_messages = [
    {"role": "user", "content": content, "cid": cid, "ts": index + 1, **OWNER}
    for index, (cid, content) in enumerate(rows)
  ]
  db.commit()


def pending(db, chat):
  db.expire_all()
  return [(row["cid"], row.get("held")) for row in db.get(models.Chat, chat.id).pending_messages]


@pytest.fixture
def scheduled(monkeypatch):
  from app.routes import chats_stream
  starts = []
  record = lambda **kwargs: starts.append(kwargs) or True  # noqa: E731
  monkeypatch.setattr(chats_stream, "_schedule_continuation", record)
  monkeypatch.setattr(chat_mod, "_schedule_continuation", record)
  return starts


def stop(client, auth, chat):
  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)
  assert response.status_code == 200, response.text
  return response.json()


def send(client, auth, chat, **body):
  return client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)


def test_stop_holds_the_queue_and_nothing_starts_it(client, auth, db, chat, scheduled):
  queue(db, chat, ("a", "first"), ("b", "second"))

  assert stop(client, auth, chat)["cleared_pending_cids"] == []
  assert pending(db, chat) == [("a", True), ("b", True)]

  # The idle-queue sweep would start an old unattended queue; a held one waits.
  assert asyncio.run(chat_mod.sweep_idle_pending_chats(db)) == []
  assert scheduled == []
  assert pending(db, chat) == [("a", True), ("b", True)]


def test_an_empty_send_runs_the_whole_held_queue_as_one_turn(client, auth, db, chat, scheduled):
  queue(db, chat, ("a", "first"), ("b", "second"))
  stop(client, auth, chat)

  response = send(client, auth, chat, content="", continuation="queue")

  assert response.status_code == 202, response.text
  body = response.json()
  assert body["status"] == "started"
  assert body["message"]["content"] == "first\nsecond"
  assert body["message"]["_consumed_cids"] == ["a", "b"]
  assert len(scheduled) == 1
  assert pending(db, chat) == []


def test_a_new_message_runs_alone_and_the_held_queue_follows(client, auth, db, chat, scheduled):
  queue(db, chat, ("a", "first"), ("b", "second"))
  stop(client, auth, chat)

  response = send(client, auth, chat, content="new", cid="n")

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "started"
  assert response.json()["message"]["content"] == "new"
  assert len(scheduled) == 1
  # Released, not merged: the turn-end drain runs them next.
  assert pending(db, chat) == [("a", None), ("b", None)]

  async def turn_end_drain():
    return await chat_queue.promote_pending_messages_locked(db, chat.id, "next-run")

  _, promoted, _ = asyncio.run(turn_end_drain())
  assert promoted["content"] == "first\nsecond"
  assert pending(db, chat) == []


def test_an_empty_send_needs_a_queue(client, auth, db, chat, scheduled):
  response = send(client, auth, chat, content="", continuation="queue")
  assert response.status_code == 409, response.text
  assert scheduled == []


def test_an_empty_send_carries_no_message(client, auth, chat):
  response = send(client, auth, chat, content="hello", continuation="queue")
  assert response.status_code == 422, response.text


def test_the_idle_sweep_does_not_even_claim_a_held_queue():
  """Held rows never look like an abandoned queue, so the sweep spends no
  chat load or writer round-trip on them each minute."""
  held = [{"role": "user", "content": "first", "cid": "a", "ts": 1, "held": True}]
  assert chat_mod._pending_head_is_stale(held, now_ms=10**12) is False
  assert chat_mod._pending_head_is_stale([{**held[0], "held": None}], now_ms=10**12) is True


def test_stop_drops_what_an_app_or_a_browser_guest_queued(client, auth, db, chat):
  chat.pending_messages = [
    {"role": "user", "content": "mine", "cid": "a", "ts": 1, **OWNER},
    {"role": "user", "content": "app job", "cid": "app", "ts": 2, "_initiated_by_app_id": 3},
    {"role": "user", "content": "guest", "cid": "guest", "ts": 3,
     "_browser_grant_id": "grant", **OWNER},
  ]
  db.commit()

  stop(client, auth, chat)

  assert pending(db, chat) == [("a", True)]


def test_an_empty_send_while_a_turn_runs_queues_the_held_rows_behind_it(
  client, auth, db, chat, scheduled, monkeypatch,
):
  from app.routes import chats_stream
  queue(db, chat, ("a", "first"))
  stop(client, auth, chat)
  monkeypatch.setattr(chats_stream, "is_chat_running", lambda chat_id: True)

  response = send(client, auth, chat, content="", continuation="queue")

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "queued"
  assert scheduled == []
  assert pending(db, chat) == [("a", None)], "they drain after the running turn"


def test_held_rows_are_not_runnable_work(db, chat):
  """Checks that mean "something is queued to run" (busy chats, compaction,
  a compaction pause continuing) ignore rows that wait for the owner."""
  from app.continuations import has_runnable_pending
  held = [{"role": "user", "content": "first", "cid": "a", "ts": 1, "held": True}]
  assert has_runnable_pending(held) is False
  assert has_runnable_pending([{**held[0], "held": None}]) is True
  chat.pending_messages = held
  db.commit()
  assert chat_mod.is_chat_busy(db, chat) is False
  park = models.ChatRun(id="p", chat_id=chat.id, status="parked", park_reason="compaction")
  assert chat_mod._park_continues_automatically(chat, park) is True


def test_a_browser_guest_cannot_send_the_owners_held_queue(db, chat, scheduled):
  """The released queue would run as the owner's work, outside the guest's
  revocable grant, so the guest is refused before anything is released."""
  from types import SimpleNamespace
  from fastapi import HTTPException
  from app.deps import Principal
  from app.routes import chats_stream
  queue(db, chat, ("a", "first"))
  chat.pending_messages = [{**chat.pending_messages[0], "held": True}]
  db.commit()
  guest = Principal(owner=None, app_id=None, scope="owner", browser=SimpleNamespace(grant_id="grant"))

  with pytest.raises(HTTPException) as refused:
    asyncio.run(chats_stream._send_queue(chat.id, db, guest, False))

  assert refused.value.status_code == 403
  assert pending(db, chat) == [("a", True)]
  assert scheduled == []


def test_an_empty_send_is_fresh_owner_input_for_a_goal_stop_paused(
  client, auth, db, chat, scheduled,
):
  """The rows were typed before Stop; pressing Send is the owner's input now,
  so the Goal the Stop paused may resume for it."""
  from app.goals import has_owner_input_after_hold, stage_goal_hold
  goal = models.ChatGoal(id="g", chat_id=chat.id, objective="Finish", revision=1,
                         plan_json={"tasks": []})
  db.add(goal)
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="stop")
  queue(db, chat, ("a", "first"))
  stop(client, auth, chat)

  response = send(client, auth, chat, content="", continuation="queue")

  assert response.status_code == 202, response.text
  db.expire_all()
  run = db.get(models.ChatRun, scheduled[0]["run_token"])
  assert has_owner_input_after_hold(run, db.get(models.ChatGoal, "g")) is True


def test_an_empty_send_restores_an_archived_chat_like_a_typed_one(client, auth, db, chat, scheduled):
  from datetime import UTC, datetime
  queue(db, chat, ("a", "first"))
  stop(client, auth, chat)
  db.expire_all()
  db.get(models.Chat, chat.id).archived_at = datetime.now(UTC)
  db.commit()

  assert send(client, auth, chat, content="", continuation="queue").status_code == 202

  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is None


def test_a_refused_empty_send_leaves_the_queue_held(client, auth, db, chat, scheduled, monkeypatch):
  from app.routes import chats_stream
  queue(db, chat, ("a", "first"))
  stop(client, auth, chat)
  monkeypatch.setattr(chats_stream, "mark_starting", lambda chat_id: False)

  response = send(client, auth, chat, content="", continuation="queue")

  assert response.status_code == 409, response.text
  assert scheduled == []
  assert pending(db, chat) == [("a", True)]


def test_an_agents_message_never_releases_the_owners_held_queue(client, auth, db, chat, scheduled):
  """Only the owner's send carries owner proof. A top-level agent token
  may post into the idle chat, but its message runs alone: the held rows
  stay held, unstamped, and no stopped Goal is credited with owner input."""
  from datetime import timedelta
  from app import auth as auth_mod
  from app.chat_writer import create_chat
  queue(db, chat, ("a", "first"), ("b", "second"))
  stop(client, auth, chat)
  db.add(create_chat(id="agent-home", title="Agent", messages=[]))
  db.add(models.ChatRun(id="agent-run", chat_id="agent-home", provider="claude"))
  db.commit()
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token("agent-home", owner.username, owner.token_epoch,
                                      run_id="agent-run", expires_delta=timedelta(minutes=5))

  response = send(client, {"Authorization": f"Bearer {token}"}, chat, content="from an agent", cid="ag")

  assert response.status_code == 202, response.text
  assert response.json()["message"]["content"] == "from an agent"
  db.expire_all()
  rows = db.get(models.Chat, chat.id).pending_messages
  assert [(row["cid"], row.get("held")) for row in rows] == [("a", True), ("b", True)]
  assert all(row["_owner_input_at"] == OWNER["_owner_input_at"] for row in rows)
  assert db.get(models.ChatRun, scheduled[0]["run_token"]).owner_input_at is None


def test_stop_drops_a_message_an_agent_queued(client, auth, db, chat):
  chat.pending_messages = [
    {"role": "user", "content": "mine", "cid": "a", "ts": 1, **OWNER},
    {"role": "user", "content": "agent", "cid": "ag", "ts": 2, "_agent_input": True},
  ]
  db.commit()
  stop(client, auth, chat)
  assert pending(db, chat) == [("a", True)]
