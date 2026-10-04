"""A live Question Box answer receives uploaded files without changing its choice."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routes import chats_stream
from app.schemas import SendMessage
from app import models
from app.chat_writer import apply_answers_to_last_question, _question_answer_fields


def test_live_question_attachment_context_uses_verified_upload(tmp_path, monkeypatch):
  upload = tmp_path / "chats" / "chat-a" / "uploads" / "photo.png"
  upload.parent.mkdir(parents=True)
  upload.write_bytes(b"image")
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  chat = SimpleNamespace(uploads=[{
    "name": "photo.png", "path": str(upload), "mime_type": "image/png",
  }])
  answers = {"Which option?": "Keep it"}

  delivered = chats_stream._question_answer_with_attachments(
    chat, answers, [{"name": "photo.png"}],
  )

  assert answers == {"Which option?": "Keep it"}
  assert delivered["Which option?"].startswith("Keep it\n\n[Attached files:")
  assert str(upload) in delivered["Which option?"]


def test_live_question_attachment_context_rejects_unuploaded_file(tmp_path, monkeypatch):
  monkeypatch.setattr(chats_stream, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  chat = SimpleNamespace(uploads=[])
  with pytest.raises(HTTPException) as exc:
    chats_stream._question_answer_with_attachments(
      chat, {"Show me": "Attached 1 file"}, [{"name": "forged.png"}],
    )
  assert exc.value.status_code == 409


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
  chat = models.Chat(
    id="question-photo", title="Photo answer",
    messages=[{"role": "assistant", "blocks": [{
      "type": "question", "question_id": "card-q",
      "questions": [{"id": "q", "question": "Show me", "options": []}],
    }]}],
  )
  attached = [{"name": "photo.png", "size": 5, "mime_type": "image/png"}]
  assert apply_answers_to_last_question(
    chat, {"Show me": "Attached 1 file"}, "card-q",
    metadata={"attachments": attached},
  )
  saved = chat.messages[0]["blocks"][0]
  assert _question_answer_fields(saved)["attachments"] == attached
