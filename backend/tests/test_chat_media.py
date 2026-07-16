from pathlib import Path

import pytest

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


def test_fix_forward_chat_media_restores_moved_file_when_commit_fails(
  db, chat, monkeypatch,
):
  old_url = f"/api/chats/{chat.id}/generated/old.png"
  chat.messages = [{"role": "assistant", "content": old_url}]
  db.commit()

  chat_root = Path(get_settings().data_dir) / "chats" / chat.id
  old_file = chat_root / "generated" / "old.png"
  old_file.parent.mkdir(parents=True)
  old_file.write_bytes(b"old-image")

  def fail_commit():
    raise RuntimeError("commit failed")

  monkeypatch.setattr(db, "commit", fail_commit)
  with pytest.raises(RuntimeError, match="commit failed"):
    fix_forward_chat_media(db, get_settings().data_dir)

  assert old_file.read_bytes() == b"old-image"
  assert not (chat_root / "media" / "old.png").exists()
  persisted = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert persisted.messages[0]["content"] == old_url
