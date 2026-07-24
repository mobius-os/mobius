"""Fix-forward migration for the canonical per-chat media directory."""

import filecmp
import shutil
from pathlib import Path

from sqlalchemy import String, cast, literal, or_
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
  # Upgrade-only work must be proportional to actual legacy state. The old
  # implementation loaded every Chat ORM row on every boot; because Chat owns
  # the complete JSON transcript, 400 settled chats produced a ~294 MiB
  # allocation burst even when this migration returned ``changed == 0``.
  # Find candidate ids using the filesystem and narrow SQL string predicates,
  # neither of which decodes Chat.messages.
  filesystem_ids: set[str] = set()
  if chats_root.is_dir():
    for chat_root in chats_root.iterdir():
      if chat_root.is_dir() and (chat_root / "generated").is_dir():
        filesystem_ids.add(chat_root.name)
  # Preserve the old behavior for orphaned chat directories: only directories
  # with a corresponding Chat row enter collision preflight. Chunk the IN
  # query below SQLite's parameter ceiling without loading any transcript.
  candidate_ids: set[str] = set()
  ordered_filesystem_ids = sorted(filesystem_ids)
  for offset in range(0, len(ordered_filesystem_ids), 500):
    candidate_ids.update(
      row[0]
      for row in (
        db.query(models.Chat.id)
        .filter(models.Chat.id.in_(
          ordered_filesystem_ids[offset:offset + 500],
        ))
        .all()
      )
    )
  legacy_url = (
    literal("%/api/chats/")
    + cast(models.Chat.id, String)
    + literal("/generated/%")
  )
  candidate_ids.update(
    row[0]
    for row in (
      db.query(models.Chat.id)
      .filter(or_(
        cast(models.Chat.messages, String).like(legacy_url),
        cast(models.Chat.pending_messages, String).like(legacy_url),
      ))
      .all()
    )
  )
  if not candidate_ids:
    return 0

  # Validate every collision before changing either filesystem or database
  # state. A single conflicting name must not leave earlier chats half-moved.
  for chat_id in sorted(candidate_ids):
    old_dir = chats_root / chat_id / "generated"
    media_dir = chats_root / chat_id / "media"
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
          f"Conflicting chat media file for chat {chat_id}: {source.name}"
        )

  # Load one transcript at a time. A real legacy fleet may contain many
  # candidates, but the migration's peak memory stays bounded by its largest
  # individual chat rather than the sum of every chat in the instance.
  for chat_id in sorted(candidate_ids):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if chat is None:
      continue
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
