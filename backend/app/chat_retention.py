"""Permanent cleanup for chats whose owner-deletion window has expired.

Only chats explicitly tombstoned through the delete lifecycle belong here.
Ordinary reads must not infer abandonment from age or content, and notification
history follows its own product lifecycle rather than piggybacking on chat
listing.
"""

import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, questions
from app.chat import forget_chat
from app.config import get_settings
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


def _purge_chat_storage(chat_id: str) -> None:
  """Remove data derived from a chat after its recovery window has closed."""
  data_dir = Path(get_settings().data_dir)
  shutil.rmtree(data_dir / "chats" / chat_id, ignore_errors=True)
  shutil.rmtree(
    data_dir / "agent-browser-profiles" / f"chat-{chat_id}",
    ignore_errors=True,
  )
  shutil.rmtree(
    data_dir / "shared" / "memory" / "chats" / chat_id,
    ignore_errors=True,
  )


def purge_expired_chat_tombstones(db: Session) -> list[str]:
  """Permanently remove chats explicitly deleted more than seven days ago.

  The candidate query selects IDs only, so this lifecycle sweep never decodes
  transcript or pending-message JSON. Database deletion commits before
  best-effort process/filesystem cleanup; a failed transaction therefore
  cannot erase recoverable data outside the database.
  """
  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  expired_chat_ids = select(models.Chat.id).where(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
  )
  chat_ids = [
    chat_id for chat_id in db.scalars(expired_chat_ids).all()
  ]
  if not chat_ids:
    return []

  dependent_models = (
    models.AgentLifecycleEvent,
    models.AgentLifecycleRunUpdate,
    models.ChatRun,
    models.ToolOutput,
    models.ThinkingTrace,
    models.ChatSessionLink,
  )
  for model in dependent_models:
    db.query(model).filter(
      model.chat_id.in_(expired_chat_ids),
    ).delete(synchronize_session=False)
  # Search rows are derived transcript data without a foreign key because the
  # SQLite FTS trigger owns their lifecycle. Remove them in the same durable
  # transaction as the source row rather than retaining a hard-deleted chat's
  # prose until a future search happens to reconcile the index.
  from app.chat_search import purge_chat_docs
  purge_chat_docs(db, chat_ids)
  db.query(models.Chat).filter(
    models.Chat.id.in_(expired_chat_ids),
  ).delete(synchronize_session=False)
  db.commit()

  for chat_id in chat_ids:
    questions.cancel(chat_id)
    forget_chat(chat_id)
    _purge_chat_storage(chat_id)

  return chat_ids
