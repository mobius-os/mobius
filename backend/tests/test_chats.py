"""Chat route regression tests."""

import asyncio
from uuid import uuid4

from app import questions
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


def test_create_chat_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/chats",
    json={"title": "Blocked"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


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
