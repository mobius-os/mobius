"""Background chat lifecycle sweeps."""

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import models, questions
from app.config import get_settings
from app.chat import forget_chat
from app.database import SessionLocal

log = logging.getLogger(__name__)

SOFT_DELETE_TTL = timedelta(days=7)

# How long an untouched empty chat (no session, no messages, no pending
# queue) survives before the lifecycle sweeper hard-deletes it. Long
# enough that a user who opened a chat, started a draft in the browser,
# and walked away for the afternoon doesn't lose it; short enough that
# abandoned empties don't pile up across weeks. Hard-delete (not soft)
# because there's nothing to recover.
EMPTY_CHAT_GRACE = timedelta(hours=24)

NOTIFICATION_TTL = timedelta(days=90)
CHAT_SWEEP_INTERVAL = timedelta(minutes=15)


def _purge_chat_dir(chat_id: str) -> None:
  """Removes per-chat scratch dirs left on disk after a chat is gone."""
  data_dir = Path(get_settings().data_dir)
  shutil.rmtree(data_dir / "chats" / chat_id, ignore_errors=True)
  shutil.rmtree(
    data_dir / "agent-browser-profiles" / f"chat-{chat_id}",
    ignore_errors=True,
  )


def _hard_delete_chat(db: Session, chat: models.Chat) -> None:
  """Deletes one chat and its sidecar runtime/storage state."""
  questions.cancel(chat.id)
  forget_chat(chat.id)
  _purge_chat_dir(chat.id)
  db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == chat.id,
  ).delete(synchronize_session=False)
  db.delete(chat)


def sweep_chat_lifecycle(db: Session) -> dict[str, int]:
  """Runs chat cleanup that must not happen on read routes."""
  now = datetime.now(UTC).replace(tzinfo=None)
  stats = {
    "soft_deleted_purged": 0,
    "empty_chats_purged": 0,
    "orphaned_runs_purged": 0,
    "notifications_purged": 0,
  }

  cutoff = now - SOFT_DELETE_TTL
  stale = db.query(models.Chat).filter(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
  ).all()
  for chat in stale:
    _hard_delete_chat(db, chat)
    stats["soft_deleted_purged"] += 1

  empty_cutoff = now - EMPTY_CHAT_GRACE
  candidates = db.query(models.Chat).filter(
    models.Chat.deleted_at.is_(None),
    models.Chat.session_id.is_(None),
    models.Chat.created_at < empty_cutoff,
  ).all()
  for chat in candidates:
    if chat.messages or chat.pending_messages:
      continue
    _hard_delete_chat(db, chat)
    stats["empty_chats_purged"] += 1

  for run in db.query(models.ChatRun).all():
    chat_exists = db.query(models.Chat.id).filter(
      models.Chat.id == run.chat_id,
    ).first()
    if chat_exists is None:
      db.delete(run)
      stats["orphaned_runs_purged"] += 1

  notification_cutoff = now - NOTIFICATION_TTL
  stats["notifications_purged"] = db.query(models.Notification).filter(
    models.Notification.sent_at < notification_cutoff,
  ).delete(synchronize_session=False)

  if any(stats.values()):
    db.commit()
  else:
    db.rollback()
  return stats


async def periodic_chat_sweep(interval: float | None = None) -> None:
  """Runs the lifecycle sweep forever at a fixed cadence."""
  delay = (
    interval if interval is not None
    else CHAT_SWEEP_INTERVAL.total_seconds()
  )
  while True:
    await asyncio.sleep(delay)
    db = SessionLocal()
    try:
      stats = sweep_chat_lifecycle(db)
      if any(stats.values()):
        log.info("chat lifecycle sweep: %s", stats)
    except asyncio.CancelledError:
      raise
    except Exception:
      db.rollback()
      log.exception("chat lifecycle sweep failed")
    finally:
      db.close()
