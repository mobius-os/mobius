"""Server-owned lifecycle for per-chat uploads.

A new upload is a draft (`claimed: False`). The chat writer claims it in the
same commit that admits a user message naming it in `attachments`, whether the
message starts a turn or is queued (steered rows come from the queue, already
claimed). Cancelling a queued message releases its files back to drafts unless
another message still names them. Only drafts can be deleted, and drafts older
than `UNCLAIMED_UPLOAD_TTL` are swept when the chat next receives an upload or
a message. Entries without the key predate this lifecycle and are always kept,
because nothing records whether a sent message uses them; deleting one is a
silent no-op. A draft a browser keeps unsent for longer than the TTL loses
its file the same way an abandoned one does.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from app.config import get_settings
from app.image_previews import discard_image_preview
from app.path_utils import validate_path_within_base

UNCLAIMED_UPLOAD_TTL = timedelta(days=7)


def is_draft(entry: dict) -> bool:
  return entry.get("claimed") is False


def attachment_names(attachments: list[dict] | None) -> set[str]:
  return {
    a["name"] for a in (attachments or [])
    if isinstance(a, dict) and isinstance(a.get("name"), str)
  }


def _set_claimed(chat, names: set[str], claimed: bool) -> None:
  if not names or not any(
    u.get("name") in names and is_draft(u) == claimed for u in chat.uploads or []
  ):
    return
  chat.uploads = [
    {**u, "claimed": claimed} if u.get("name") in names and "claimed" in u else u
    for u in chat.uploads
  ]


def claim_uploads(chat, attachments: list[dict] | None) -> None:
  """Mark the chat's draft uploads named by an admitted message's attachments."""
  _set_claimed(chat, attachment_names(attachments), True)


def release_uploads(chat, removed_rows: list[dict]) -> None:
  """Return a cancelled message's files to drafts unless anything else names them.

  Still in use means named by a transcript, in-progress or queued row, or
  saved on an answered question card (cards answered mid-turn live in the
  in-progress assistant row).
  """
  names = set()
  for row in removed_rows:
    names |= attachment_names(row.get("attachments"))
  if not names:
    return
  from sqlalchemy.orm import object_session

  from app import transcript_rows

  live = chat.live_assistant
  # Only transcript rows flagged as naming attachments are read.
  rows = [*transcript_rows.attachment_bodies(object_session(chat), chat),
          *([live] if isinstance(live, dict) else []),
          *(chat.pending_messages or [])]
  for row in rows:
    names -= attachment_names(row.get("attachments"))
    for block in row.get("blocks") or []:
      if isinstance(block, dict):
        names -= attachment_names(block.get("attachments"))
  _set_claimed(chat, names, False)


def partition_expired_drafts(
  uploads: list[dict], *, keep: set[str] = frozenset(), now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
  """Split uploads into (kept, expired drafts); names in `keep` never expire."""
  cutoff = (now or datetime.now(UTC)) - UNCLAIMED_UPLOAD_TTL
  kept, expired = [], []
  for entry in uploads:
    stale = (is_draft(entry) and entry.get("name") not in keep
             and _uploaded_before(entry, cutoff))
    (expired if stale else kept).append(entry)
  return kept, expired


def _uploaded_before(entry: dict, cutoff: datetime) -> bool:
  try:
    return datetime.fromisoformat(entry["uploaded_at"]) < cutoff
  except (KeyError, TypeError, ValueError):
    return False


def take_expired_drafts(chat, keep: set[str] = frozenset()) -> list[pathlib.Path]:
  """Drop drafts past the TTL from `chat.uploads`; return their files to remove."""
  kept, expired = partition_expired_drafts(chat.uploads or [], keep=keep)
  if expired:
    chat.uploads = kept
  return [pathlib.Path(e.get("path") or "") for e in expired]


def upload_dir(chat_id: str) -> pathlib.Path:
  return pathlib.Path(get_settings().data_dir) / "chats" / chat_id / "uploads"


def remove_upload_files(directory: pathlib.Path, paths: list[pathlib.Path]) -> None:
  """Delete upload files (and image previews), never outside `directory`."""
  for path in paths:
    try:
      safe = validate_path_within_base(path.name, directory)
    except HTTPException:
      continue
    if safe.is_file():
      safe.unlink()
      discard_image_preview(safe, directory)


async def sweep_expired_uploads(db, chat, keep: set[str] = frozenset()) -> None:
  """Drop expired drafts when a message arrives; the caller holds the chat lock.

  `keep` names the arriving message's own files, which it is about to claim.
  """
  def drop() -> list[pathlib.Path]:
    expired = take_expired_drafts(chat, keep)
    if expired:
      db.commit()
    return expired

  expired = await run_in_threadpool(drop)
  if expired:
    await run_in_threadpool(remove_upload_files, upload_dir(chat.id), expired)
