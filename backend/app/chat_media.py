"""Fix-forward migration for the canonical per-chat media directory."""

import filecmp
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app import models


def _replace_media_path(value, old_prefix: str, new_prefix: str):
  """Recursively rewrites media URLs inside JSON-compatible chat data."""
  if isinstance(value, str):
    return value.replace(old_prefix, new_prefix)
  if isinstance(value, list):
    return [_replace_media_path(item, old_prefix, new_prefix) for item in value]
  if isinstance(value, dict):
    return {
      key: _replace_media_path(item, old_prefix, new_prefix)
      for key, item in value.items()
    }
  return value


def fix_forward_chat_media(db: Session, data_dir: str) -> int:
  """Moves old chat images into `media/` and rewrites stored message URLs.

  The operation is idempotent. Files move before database references change,
  so a crash can only leave a second run with fewer files to move. A conflicting
  destination is accepted only when its bytes match; otherwise the migration
  stops rather than silently overwriting either image.
  """
  changed = 0
  chats_root = Path(data_dir) / "chats"
  chats = db.query(models.Chat).all()

  # Validate every collision before changing either filesystem or database
  # state. A single conflicting name must not leave earlier chats half-moved.
  for chat in chats:
    old_dir = chats_root / chat.id / "generated"
    media_dir = chats_root / chat.id / "media"
    if not old_dir.is_dir():
      continue
    for source in old_dir.iterdir():
      if not source.is_file():
        continue
      destination = media_dir / source.name
      if destination.exists() and (
        not destination.is_file()
        or not filecmp.cmp(source, destination, shallow=False)
      ):
        raise RuntimeError(
          f"Conflicting chat media file for chat {chat.id}: {source.name}"
        )

  for chat in chats:
    chat_root = chats_root / chat.id
    old_dir = chat_root / "generated"
    media_dir = chat_root / "media"
    moved: list[tuple[Path, Path]] = []
    duplicates: list[Path] = []

    try:
      if old_dir.is_dir():
        media_dir.mkdir(parents=True, exist_ok=True)
        for source in old_dir.iterdir():
          if not source.is_file():
            continue
          destination = media_dir / source.name
          if destination.exists():
            # The global preflight proved this is an identical regular file.
            # Keep the old copy until the URL rewrite commits successfully.
            duplicates.append(source)
          else:
            source.replace(destination)
            moved.append((source, destination))
          changed += 1

      old_prefix = f"/api/chats/{chat.id}/generated/"
      new_prefix = f"/api/chats/{chat.id}/media/"
      messages = _replace_media_path(chat.messages, old_prefix, new_prefix)
      pending = _replace_media_path(
        chat.pending_messages, old_prefix, new_prefix,
      )
      if messages != chat.messages:
        chat.messages = messages
        changed += 1
      if pending != chat.pending_messages:
        chat.pending_messages = pending
        changed += 1

      db.commit()
    except BaseException:
      db.rollback()
      # Restore files moved for this chat so a handled startup failure never
      # leaves old URLs pointing at a directory the live API no longer serves.
      for source, destination in reversed(moved):
        if destination.exists() and not source.exists():
          destination.replace(source)
      raise

    for source in duplicates:
      source.unlink(missing_ok=True)
    if old_dir.exists() and not any(old_dir.iterdir()):
      shutil.rmtree(old_dir)

  return changed
