from pathlib import Path

import pytest

import app.chat_media as chat_media
from app import models
from app.chat_media import fix_forward_chat_media
from app.config import get_settings


def test_fix_forward_chat_media_moves_files_and_rewrites_urls(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/old.png"
  new_url = f"/api/chats/{chat.id}/media/old.png"
  chat.messages = [{"role": "assistant", "content": f"![image]({old_url})"}]
  chat.pending_messages = [{"content": {"preview": old_url}}]
  db.commit()

  chat_root = Path(get_settings().data_dir) / "chats" / chat.id
  old_dir = chat_root / "generated"
  old_dir.mkdir(parents=True)
  (old_dir / "old.png").write_bytes(b"old-image")

  changed = fix_forward_chat_media(db, get_settings().data_dir)
  db.refresh(chat)

  assert changed == 3
  assert not old_dir.exists()
  assert (chat_root / "media" / "old.png").read_bytes() == b"old-image"
  assert chat.messages[0]["content"] == f"![image]({new_url})"
  assert chat.pending_messages[0]["content"]["preview"] == new_url


def test_fix_forward_chat_media_is_idempotent(db, chat):
  first = fix_forward_chat_media(db, get_settings().data_dir)
  second = fix_forward_chat_media(db, get_settings().data_dir)
  assert first == 0
  assert second == 0


def test_fix_forward_chat_media_skips_unrelated_transcripts(
  db, chat, monkeypatch,
):
  chat.messages = [{
    "role": "assistant",
    "content": (
      "Discussing /api/chats/someone-else/generated/example.png "
      "must not make this chat a migration candidate."
    ),
    "blocks": [{"type": "text", "content": "x" * 100_000}],
  }]
  db.commit()

  def unexpected_writer():
    raise AssertionError("unrelated transcript was selected for rewrite")

  monkeypatch.setattr(chat_media, "get_writer", unexpected_writer)

  assert fix_forward_chat_media(db, get_settings().data_dir) == 0


def test_fix_forward_chat_media_rewrites_legacy_url_without_old_directory(
  db, chat,
):
  old_url = f"/api/chats/{chat.id}/generated/already-moved.png"
  new_url = f"/api/chats/{chat.id}/media/already-moved.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  assert fix_forward_chat_media(db, get_settings().data_dir) == 1
  db.refresh(chat)
  assert chat.messages[0]["content"] == new_url


def test_fix_forward_chat_media_preflights_conflicts(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/same.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  chat_root = Path(get_settings().data_dir) / "chats" / chat.id
  old_dir = chat_root / "generated"
  media_dir = chat_root / "media"
  old_dir.mkdir(parents=True)
  media_dir.mkdir(parents=True)
  (old_dir / "same.png").write_bytes(b"old")
  (media_dir / "same.png").write_bytes(b"different")

  with pytest.raises(RuntimeError, match="Conflicting chat media file"):
    fix_forward_chat_media(db, get_settings().data_dir)

  db.refresh(chat)
  assert chat.messages[0]["content"] == old_url
  assert (old_dir / "same.png").read_bytes() == b"old"
  assert (media_dir / "same.png").read_bytes() == b"different"


def test_fix_forward_chat_media_keeps_both_copies_when_commit_fails(
  db, chat, monkeypatch,
):
  old_url = f"/api/chats/{chat.id}/generated/old.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  chat_root = Path(get_settings().data_dir) / "chats" / chat.id
  old_file = chat_root / "generated" / "old.png"
  old_file.parent.mkdir(parents=True)
  old_file.write_bytes(b"old-image")

  from app import chat_writer
  monkeypatch.setattr(chat_writer, "_commit_or_rollback", lambda _db: False)
  with pytest.raises(chat_writer._PersistFailed, match="RewriteChatMediaPaths"):
    fix_forward_chat_media(db, get_settings().data_dir)

  assert old_file.read_bytes() == b"old-image"
  assert (chat_root / "media" / "old.png").read_bytes() == b"old-image"
  persisted = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert persisted.messages[0]["content"] == old_url


def test_fix_forward_chat_media_timeout_stays_valid_after_late_commit(
  db, chat, monkeypatch,
):
  old_url = f"/api/chats/{chat.id}/generated/old.png"
  new_url = f"/api/chats/{chat.id}/media/old.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  chat_root = Path(get_settings().data_dir) / "chats" / chat.id
  old_file = chat_root / "generated" / "old.png"
  new_file = chat_root / "media" / "old.png"
  old_file.parent.mkdir(parents=True)
  old_file.write_bytes(b"old-image")

  pending_ack = None

  def timeout_without_cancelling(ack):
    nonlocal pending_ack
    pending_ack = ack
    raise TimeoutError("writer acknowledgement timed out")

  monkeypatch.setattr(chat_media, "wait_ack", timeout_without_cancelling)
  with pytest.raises(TimeoutError, match="acknowledgement timed out"):
    fix_forward_chat_media(db, get_settings().data_dir)

  assert old_file.read_bytes() == b"old-image"
  assert new_file.read_bytes() == b"old-image"
  assert pending_ack is not None
  assert pending_ack.result(timeout=5) == 1

  db.expire_all()
  persisted = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert persisted.messages[0]["content"] == new_url
