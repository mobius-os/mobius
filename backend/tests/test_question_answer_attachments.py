"""A live Question Box answer receives uploaded files without changing its choice."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routes import chats_stream
from app.schemas import SendMessage
from app import models, transcript_rows
from app.chat_writer import apply_answers_to_last_question, _question_answer_fields, create_chat


def test_question_attachments_resolve_once_per_upload(tmp_path, monkeypatch):
  upload = tmp_path / "chats" / "chat-a" / "uploads" / "photo.png"
  upload.parent.mkdir(parents=True)
  upload.write_bytes(b"image")
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  chat = SimpleNamespace(uploads=[{
    "name": "photo.png", "path": str(upload), "size": 5, "mime_type": "image/png",
  }])
  resolved = chats_stream._canonical_question_attachments(
    chat, [{"name": "photo.png", "path": "/forged"}, {"name": "photo.png"}],
  )
  assert resolved == [{"name": "photo.png", "size": 5, "mime_type": "image/png"}]


def test_question_attachments_are_bounded(tmp_path, monkeypatch):
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  too_many = [{"name": f"f{i}.txt"} for i in range(chats_stream.MAX_QUESTION_ATTACHMENTS + 1)]
  with pytest.raises(HTTPException) as exc:
    chats_stream._canonical_question_attachments(SimpleNamespace(uploads=[]), too_many)
  assert exc.value.status_code == 422


def test_live_question_attachment_context_rejects_unuploaded_file(tmp_path, monkeypatch):
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  chat = SimpleNamespace(uploads=[])
  with pytest.raises(HTTPException) as exc:
    chats_stream._canonical_question_attachments(
      chat, [{"name": "forged.png"}],
    )
  assert exc.value.status_code == 409
  assert "forged.png" in exc.value.detail


def test_agent_card_answer_still_cannot_attach_arbitrary_upload():
  card = {"questions": [{"id": "q", "question": "Show me", "options": []}]}
  body = SendMessage(
    content="unrelated hidden content",
    answers={"Show me": "Okay"},
    attachments=[{"name": "forged.png"}],
    hidden=True,
    question_id="card-q",
  )
  confined = chats_stream._confine_agent_card_answer(body, card)
  assert confined.attachments is None
  assert confined.content == "- Show me: Okay"


def test_answered_card_keeps_attachment_for_reopen(db):
  chat = create_chat(
    id="question-photo", title="Photo answer",
    messages=[{"role": "assistant", "blocks": [{
      "type": "question", "question_id": "card-q",
      "questions": [{"id": "q", "question": "Show me", "options": []}],
    }]}],
  )
  db.add(chat)
  db.commit()
  attached = [{"name": "photo.png", "size": 5, "mime_type": "image/png"}]
  assert apply_answers_to_last_question(
    chat, {"Show me": "Attached 1 file"}, "card-q",
    metadata={"attachments": attached},
  )
  saved = transcript_rows.at(db, chat, 0)["blocks"][0]
  assert _question_answer_fields(saved)["attachments"] == attached


@pytest.mark.parametrize("mode", ["idle", "running", "native"])
@pytest.mark.parametrize("valid", [False, True])
def test_saved_card_route_canonical_attachments(
  client, auth, chat, monkeypatch, tmp_path, mode, valid,
):
  """Saved-card producer → route → writer → next turn and cross-tab event."""
  import asyncio

  from app import chat as chat_mod, questions
  from app.pending_questions import PendingQuestion
  from app.database import SessionLocal
  from app.chat_writer import get_writer, ReplaceTranscript, QuestionCommit

  running = mode != "idle"
  get_writer().submit(ReplaceTranscript(
    chat_id=chat.id,
    messages=[{"role": "user", "content": "Choose", "ts": 1}],
  )).result(timeout=5)
  get_writer().submit(QuestionCommit(
    chat_id=chat.id,
    snapshot={"role": "assistant", "content": "", "ts": 2, "blocks": [{
      "type": "question", "question_id": "saved-files",
      **({"response_mode": "continuation"} if mode != "native" else {}),
      "questions": [
        {"id": "q1", "question": "Pick one", "options": ["a", "b"]},
        {"id": "q2", "question": "Add details", "options": [], "multiSelect": True},
      ],
    }]},
  )).result(timeout=5)
  upload = tmp_path / "photo.png"
  upload.write_bytes(b"image")
  canonical = {"name": "photo.png", "size": 5, "mime_type": "image/png"}
  stored = {**canonical, "path": str(upload)}
  with SessionLocal() as db:
    row = db.get(models.Chat, chat.id)
    row.uploads = [{**stored, "claimed": False}] if valid else []
    db.commit()
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  monkeypatch.setattr(chats_stream, "is_chat_running", lambda _: running)
  events, scheduled = [], []
  monkeypatch.setattr(chats_stream, "get_broadcast", lambda _: SimpleNamespace(publish=events.append))

  def schedule(**kwargs):
    scheduled.append(kwargs)
    chat_mod.discard_starting(kwargs["chat_id"])

  monkeypatch.setattr(chats_stream, "_schedule_continuation", schedule)
  loop = asyncio.new_event_loop()
  future = loop.create_future()
  if mode == "native":
    questions.register(chat.id, PendingQuestion(
      question_id="saved-files", questions=[], future=future,
    ))
  res = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "- Pick one: a", "hidden": True,
    "answers": {"Pick one": "a", "Add details": "Attached 1 file"}, "question_id": "saved-files",
    "attachments": [{"name": "photo.png", "size": 999, "mime_type": "text/html", "path": "/forged"}],
  })
  if mode == "native":
    resolved = future.done()
    questions.cancel(chat.id)
  loop.close()
  assert res.status_code == (202 if valid and mode != "native" else 409), res.text
  with SessionLocal() as db:
    row = db.get(models.Chat, chat.id)
    card = next(b for m in transcript_rows.read_all(db, row) for b in m.get("blocks", []) if b.get("question_id") == "saved-files")
    if not valid or mode == "native":
      if mode == "native":
        # Provider-native questions are retired and refuse files outright.
        assert not resolved
      assert row.uploads == ([{**stored, "claimed": False}] if valid else [])
      assert "answers" not in card
      assert not row.pending_messages
      assert not events
      assert not scheduled
      return
    assert card["answers"] == {"Pick one": "a", "Add details": "Attached 1 file"}
    assert card["attachments"] == [canonical]
    assert row.uploads == [{**stored, "claimed": True}]
    message = row.pending_messages[0] if running else scheduled[0]["next_user"]
    assert message["attachments"] == [canonical]
    assert str(upload) in message["content"]
    assert "/forged" not in message["content"]
  applied = next(event for event in events if event["type"] == "answers_applied")
  assert applied["attachments"] == [canonical]
  assert applied["answers"] == {"Pick one": "a", "Add details": "Attached 1 file"}


def test_quiet_close_answer_refuses_files(client, auth, chat, monkeypatch, tmp_path):
  """A choice that closes the card without a reply cannot carry files."""
  from app.database import SessionLocal
  from app.chat_writer import get_writer, ReplaceTranscript, QuestionCommit

  get_writer().submit(ReplaceTranscript(
    chat_id=chat.id, messages=[{"role": "user", "content": "Choose", "ts": 1}],
  )).result(timeout=5)
  get_writer().submit(QuestionCommit(
    chat_id=chat.id,
    snapshot={"role": "assistant", "content": "", "ts": 2, "blocks": [{
      "type": "question", "question_id": "quiet-files", "response_mode": "continuation",
      "questions": [{"id": "q", "question": "Proceed?", "options": [
        {"id": "0", "label": "Not now", "on_answer": "close"},
      ]}],
    }]},
  )).result(timeout=5)
  upload = tmp_path / "photo.png"
  upload.write_bytes(b"image")
  with SessionLocal() as db:
    row = db.get(models.Chat, chat.id)
    row.uploads = [{"name": "photo.png", "path": str(upload), "size": 5,
                    "mime_type": "image/png", "claimed": False}]
    db.commit()
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))

  res = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "- Proceed?: Not now", "hidden": True, "question_id": "quiet-files",
    "answers": {"Proceed?": "Not now"}, "selected_options": {"q": ["0"]},
    "attachments": [{"name": "photo.png"}],
  })

  assert res.status_code == 409, res.text
  assert "closes the card without sending files" in res.json()["detail"]
  with SessionLocal() as db:
    row = db.get(models.Chat, chat.id)
    card = next(b for m in transcript_rows.read_all(db, row) for b in m.get("blocks", []) if b.get("question_id") == "quiet-files")
    assert "answers" not in card
    assert row.uploads[0]["claimed"] is False


def test_restart_card_reply_refuses_files(client, auth, chat, monkeypatch):
  """A written Restart reply never carries files, even from a direct API call."""
  import app.platform_restart as platform_restart

  monkeypatch.setattr(platform_restart, "restart_action_block", lambda *_: {"type": "question"})
  res = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "- Restart?: later", "hidden": True, "question_id": "restart-card",
    "answers": {"Restart?": "later"}, "attachments": [{"name": "photo.png"}],
  })
  assert res.status_code == 409, res.text
  assert "Restart card can't take files" in res.json()["detail"]
