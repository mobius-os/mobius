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


def queue(db, chat, *rows):
  chat.pending_messages = [
    {"role": "user", "content": content, "cid": cid, "ts": index + 1}
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
