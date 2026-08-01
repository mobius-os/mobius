"""Fix-forward migration for the canonical per-chat media directory."""

import filecmp
import shutil
from pathlib import Path

from sqlalchemy import String, cast, literal, or_
from sqlalchemy.orm import Session

from app import models
from app.chat_writer import RewriteChatMediaPaths, get_writer, wait_ack


def fix_forward_chat_media(db: Session, data_dir: str) -> int:
  """Copies old chat images into `media/` and rewrites stored message URLs.

  The old copy remains until the writer confirms the URL rewrite. This matters
  because a timed-out writer acknowledgement does not cancel a command already
  running on the actor thread: either eventual database outcome therefore still
  names a directory containing the bytes. A conflicting destination is accepted
  only when its bytes match; otherwise the migration stops rather than silently
  overwriting either image.
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

  # The writer actor owns transcript loading and mutation. Keep the boot session
  # on ids only so migration does not materialize the same JSON blob twice.
  for chat_id in sorted(candidate_ids):
    chat_root = chats_root / chat_id
    old_dir = chat_root / "generated"
    media_dir = chat_root / "media"

    try:
      if old_dir.is_dir():
        media_dir.mkdir(parents=True, exist_ok=True)
        for source in old_dir.iterdir():
          if not source.is_file():
            continue
          destination = media_dir / source.name
          if not destination.exists():
            shutil.copy2(source, destination)
          changed += 1

      old_prefix = f"/api/chats/{chat_id}/generated/"
      new_prefix = f"/api/chats/{chat_id}/media/"
      rewritten = wait_ack(get_writer().submit(RewriteChatMediaPaths(
        chat_id=chat_id,
        old_prefix=old_prefix,
        new_prefix=new_prefix,
      )))
      changed += int(rewritten or 0)
      db.expire_all()
    except BaseException:
      db.rollback()
      # Keep both copies. The actor may still commit after a caller-side
      # timeout, and either old or new URLs must remain readable in that case.
      raise

    if old_dir.is_dir():
      for source in old_dir.iterdir():
        if source.is_file():
          source.unlink()
    if old_dir.exists() and not any(old_dir.iterdir()):
      shutil.rmtree(old_dir)

  return changed
