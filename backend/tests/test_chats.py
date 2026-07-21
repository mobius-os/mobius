"""Chat route regression tests."""

import asyncio
from uuid import uuid4

from app import memory, questions
from app.pending_questions import PendingQuestion


def _make_pending() -> PendingQuestion:
  """Builds a fresh PendingQuestion with a live future on the running loop."""
  return PendingQuestion(
    question_id=str(uuid4()),
    questions=[{"id": "q1", "question": "Pick one", "options": ["a", "b"]}],
    future=asyncio.get_event_loop().create_future(),
  )


def test_delete_chat_cancels_orphan_pending_question(client, auth, chat):
  # This belongs in the chat route tests because it exercises DELETE
  # /api/chats/{id} and its side effects on idle-chat cleanup.
  async def go():
    pending = _make_pending()
    questions.register(chat.id, pending)

    response = client.delete(f"/api/chats/{chat.id}", headers=auth)

    assert response.status_code == 204
    assert questions.get(chat.id) is None
    assert pending.future.done()

  asyncio.run(go())


def test_agent_context_includes_evolving_chat_summary(
  client, auth, chat, monkeypatch,
):
  monkeypatch.setattr(
    "app.compaction.load_cumulative_summary",
    lambda _data_dir, chat_id: (
      "The cumulative handoff." if chat_id == chat.id else None
    ),
  )
  monkeypatch.setattr(
    "app.memory.load_chat_summary_metadata",
    lambda _data_dir, chat_id: {
      "description": "A one-line summary" if chat_id == chat.id else None,
      "digest": "The bounded digest." if chat_id == chat.id else None,
    },
  )
  monkeypatch.setattr(
    "app.memory.build_memory_block",
    lambda *_args, **_kwargs: memory.MemoryBlock(
      text="<recent_chat>...</recent_chat>",
      loaded=["chats/older/index.md"],
      entries=[{
        "name": "Older chat",
        "location": "chats/older/index.md",
        "digest": "A bounded digest.",
      }],
      mode="recent_chats",
    ),
  )
  monkeypatch.setattr("app.providers.get_skill_origin", lambda: "platform")

  response = client.get(
    f"/api/chats/{chat.id}/agent-context",
    headers=auth,
  )

  assert response.status_code == 200
  payload = response.json()
  assert {
    key: payload[key]
    for key in ("chat_description", "chat_digest", "chat_summary")
  } == {
    "chat_description": "A one-line summary",
    "chat_digest": "The bounded digest.",
    "chat_summary": "The cumulative handoff.",
  }
  assert payload["recent_chat_entries"] == [{
    "name": "Older chat",
    "location": "chats/older/index.md",
    "digest": "A bounded digest.",
  }]
  assert payload["system_prompt_origin"] == "platform"


def test_create_chat_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/chats",
    json={"title": "Blocked"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_create_chat_returns_canonical_owner_drawer_summary(client, auth):
  created = client.post(
    "/api/chats",
    json={"title": "Canonical create"},
    headers=auth,
  )
  assert created.status_code == 200
  body = created.json()

  listed = client.get("/api/chats", headers=auth)
  assert listed.status_code == 200
  row = next(item for item in listed.json() if item["id"] == body["id"])
  assert {key: body[key] for key in row} == row
  assert body["messages"] == []

  detail = client.get(f"/api/chats/{body['id']}", headers=auth)
  assert detail.status_code == 200
  detail_body = detail.json()
  assert body["detail"] == detail_body


def test_update_chat_rejects_cross_site_request(client, auth, chat):
  cross = client.put(
    f"/api/chats/{chat.id}",
    json={"title": "Blocked"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_delete_chat_rejects_cross_site_request(client, auth, chat):
  cross = client.delete(
    f"/api/chats/{chat.id}",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_recover_chat_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/recover",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_question_answers_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/question-answers",
    json={"answers": {"q1": "red"}},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_send_message_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "hi"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_fresh_send_response_includes_stored_user_message(
  client, auth, chat, db, monkeypatch,
):
  async def _noop_run_chat(*args, **kwargs):
    return None

  monkeypatch.setattr("app.routes.chats_stream.run_chat", _noop_run_chat)

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "build forge"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  body = response.json()
  assert body["status"] == "started"
  assert body["message"]["role"] == "user"
  assert body["message"]["content"] == "build forge"
  assert isinstance(body["message"]["ts"], int)

  db.refresh(chat)
  assert chat.messages == [body["message"]]


def test_retry_of_durable_message_is_acknowledged_without_new_turn(
  client, auth, chat, db, monkeypatch,
):
  calls = []

  async def _record_run_chat(*args, **kwargs):
    calls.append((args, kwargs))

  monkeypatch.setattr("app.routes.chats_stream.run_chat", _record_run_chat)
  stored = {
    "role": "user",
    "content": "build forge",
    "ts": 123,
    "cid": "cid-retry",
  }
  chat.messages = [stored]
  db.commit()

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "build forge", "cid": "cid-retry"},
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json() == {
    "status": "duplicate",
    "message": stored,
    "running": False,
  }
  db.refresh(chat)
  assert chat.messages == [stored]
  assert chat.run_status is None
  assert calls == []


def test_retry_of_durable_message_preserves_a_later_running_turn(
  client, auth, chat, db, monkeypatch,
):
  stored = {
    "role": "user",
    "content": "first request",
    "ts": 123,
    "cid": "cid-retry",
  }
  chat.messages = [stored]
  db.commit()
  monkeypatch.setattr(
    "app.routes.chats_stream.is_chat_running", lambda _chat_id: True,
  )

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "first request", "cid": "cid-retry"},
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json()["status"] == "duplicate"
  assert response.json()["running"] is True
  db.refresh(chat)
  assert chat.messages == [stored]


def test_retry_of_pending_message_returns_its_existing_queue_position(
  client, auth, chat, db, monkeypatch,
):
  first = {
    "role": "user", "content": "first", "ts": 10, "cid": "cid-first",
  }
  retry = {
    "role": "user", "content": "second", "ts": 11, "cid": "cid-retry",
  }
  chat.pending_messages = [first, retry]
  db.commit()
  monkeypatch.setattr(
    "app.routes.chats_stream.is_chat_running", lambda _chat_id: True,
  )

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "second", "cid": "cid-retry"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "queued"
  assert response.json()["position"] == 2
  assert response.json()["pending_message"] == retry
  db.refresh(chat)
  assert chat.pending_messages == [first, retry]


def test_fresh_send_returns_503_when_writer_is_unavailable(client, auth, chat):
  from app.chat_writer import get_writer

  get_writer()._go_fatal()
  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "please keep this draft"},
    headers=auth,
  )

  assert response.status_code == 503
  assert response.json()["detail"] == "Could not save your message; please try again."


def test_queued_send_returns_503_when_writer_is_unavailable(
  client, auth, chat, db,
):
  from app.chat_writer import get_writer

  chat.pending_messages = [{
    "role": "user", "content": "already queued", "ts": 1, "cid": "prior",
  }]
  db.commit()
  get_writer()._go_fatal()

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "queue this too"},
    headers=auth,
  )

  assert response.status_code == 503
  assert response.json()["detail"] == "Could not save your message; please try again."


def test_update_icon_rejects_cross_site_request(client, auth):
  # The cross-site dependency fires before the handler, so a non-existent
  # app id still 403s (mirrors test_update_app_rejects_cross_site_request).
  cross = client.put(
    "/api/apps/1/icon",
    content=b"not-an-image",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_chat_title_naming_precedence(client, auth, db, chat):
  """user > agent > first-message: a manual rename locks the name (the agent's
  by_agent sync can't clobber it); clear unlocks + falls back to the first
  message; the agent can fill the name again once it's unlocked."""
  from app import models
  cid = chat.id
  chat.messages = [{"role": "user", "content": "help me dial in espresso"}]
  chat.title = "help me dial in espresso"
  db.commit()

  def patch(payload):
    return client.patch(f"/api/chats/{cid}", json=payload, headers=auth)

  def current():
    db.expire_all()
    return db.query(models.Chat).filter_by(id=cid).first()

  # 1) agent fills the name when not locked
  assert patch({"title": "Espresso shot dial-in", "by_agent": True}).status_code == 200
  c = current(); assert c.title == "Espresso shot dial-in" and c.title_locked is False
  # 2) a manual (user) rename locks it
  assert patch({"title": "Coffee help"}).status_code == 200
  c = current(); assert c.title == "Coffee help" and c.title_locked is True
  # 3) the agent can NOT overwrite a locked name
  patch({"title": "Something else", "by_agent": True})
  assert current().title == "Coffee help"
  # 4) clear unlocks + falls back to the first message
  assert patch({"clear_title": True}).status_code == 200
  c = current(); assert c.title == "help me dial in espresso" and c.title_locked is False
  # 5) the agent can fill again once unlocked
  patch({"title": "Espresso dial-in", "by_agent": True})
  assert current().title == "Espresso dial-in"
