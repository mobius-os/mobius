# backend/tests/test_augmentation.py
"""Tests that send_message appends uploaded-file info to the user message."""
import io
from unittest.mock import patch

from app.broadcast import get_broadcast


def _mark_done(chat_id):
  """Mark the broadcast completed so the next test doesn't see it running."""
  bc = get_broadcast(chat_id)
  if bc and bc.running:
    bc.mark_completed()


def test_no_augmentation_without_uploads(client, db, auth, chat):
  """When no files are uploaded, the message content must be unchanged."""
  captured = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    captured.extend(msgs)
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "hello"},
      headers=auth,
    )

  user_msg = next(m for m in captured if m.role == "user")
  assert user_msg.content == "hello"
  assert "[Files" not in user_msg.content


def test_attachments_saved_in_message(client, db, auth, chat):
  """When attachments are sent, they must be saved in the message dict."""
  attachments = [
    {"name": "photo.png", "size": 1024, "mime_type": "image/png"},
  ]

  captured = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    captured.append({"msgs": msgs, "attachments": kwargs.get("attachments")})
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    resp = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "check this", "attachments": attachments},
      headers=auth,
    )

  assert resp.status_code == 202
  assert len(captured) == 1
  assert captured[0]["attachments"] == attachments


def test_augmentation_with_uploads(client, db, auth, chat):
  """When files are uploaded, the file list must be appended."""
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("report.pdf", io.BytesIO(b"data"), "application/pdf"))],
    headers=auth,
  )

  captured = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    captured.extend(msgs)
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "analyze this"},
      headers=auth,
    )

  user_msg = next(m for m in captured if m.role == "user")
  assert "report.pdf" in user_msg.content
  assert "[Files in this session:" in user_msg.content


def test_message_saved_before_run_chat(client, db, auth, chat):
  """The user message must be in the DB before the background task runs."""
  from app import models

  captured_messages = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    # Read messages from DB at the moment run_chat starts.
    from app.database import SessionLocal
    s = SessionLocal()
    c = s.query(models.Chat).filter(models.Chat.id == chat_id).first()
    captured_messages.extend(c.messages or [])
    s.close()
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "hello world"},
      headers=auth,
    )

  assert len(captured_messages) == 1
  assert captured_messages[0]["role"] == "user"
  assert "hello world" in captured_messages[0]["content"]


def test_double_send_rejected_when_running(client, db, auth, chat):
  """Sending a message while agent is running must return 409."""
  from app.chat import _active_procs
  from unittest.mock import MagicMock

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _active_procs[chat.id] = mock_proc

  try:
    resp = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "second message"},
      headers=auth,
    )
    assert resp.status_code == 409
  finally:
    _active_procs.pop(chat.id, None)
