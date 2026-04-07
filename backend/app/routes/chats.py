"""Routes for chat CRUD operations."""

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.chat import is_chat_running, stop_chat_for
from app.database import get_db
from app.deps import get_current_owner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chats", tags=["chats"])

SOFT_DELETE_TTL = timedelta(days=7)


def _purge_chat_dir(chat_id: str) -> None:
  """Removes /data/chats/{chat_id}/ if it exists."""
  chat_dir = Path(get_settings().data_dir) / "chats" / chat_id
  if chat_dir.exists():
    shutil.rmtree(chat_dir)



class ChatUpdate(BaseModel):
  title: str | None = None
  messages: list[dict] | None = None


@router.get("")
def list_chats(
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns all active chats ordered by most recently updated."""
  # Purge chats soft-deleted more than TTL ago.
  # Use naive datetime to match SQLite's naive UTC storage — comparing an
  # aware datetime against a naive DB value throws TypeError in Python 3.11+.
  cutoff = datetime.now(UTC).replace(tzinfo=None) - SOFT_DELETE_TTL
  stale = db.query(models.Chat).filter(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
  ).all()
  for c in stale:
    _purge_chat_dir(c.id)
    db.delete(c)
  db.commit()

  chats = (
    db.query(models.Chat)
    .filter(models.Chat.deleted_at.is_(None))
    .order_by(models.Chat.updated_at.desc())
    .all()
  )
  return [
    {
      "id": c.id,
      "title": c.title,
      "updated_at": c.updated_at.isoformat(),
      "has_messages": bool(c.messages and len(c.messages) > 0),
    }
    for c in chats
  ]


@router.post("")
def create_chat(
  body: ChatUpdate,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Creates a new chat."""
  import uuid
  chat = models.Chat(
    id=str(uuid.uuid4()),
    title=body.title or "New chat",
    messages=body.messages or [],
  )
  db.add(chat)
  db.commit()
  db.refresh(chat)
  return {"id": chat.id, "title": chat.title, "messages": chat.messages}


@router.put("/{chat_id}")
def update_chat(
  body: ChatUpdate,
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Updates a chat's title and/or messages."""
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found.")
  if body.title is not None:
    chat.title = body.title
  if body.messages is not None:
    chat.messages = body.messages
  # Always touch updated_at so the chat moves to the top of history.
  chat.updated_at = datetime.now(UTC)
  db.commit()
  return {"ok": True}


@router.get("/{chat_id}")
def get_chat(
  chat_id: str,
  limit: int = 20,
  before: int | None = None,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns a chat with paginated messages and running status.

  Pagination uses a message-index cursor. `before` is the index (in the full
  message list) of the first message the client does NOT have. Omit it (or
  pass None) to fetch the most recent `limit` messages. Pass the index of
  the oldest message from the previous page to load older messages.

  Messages are returned in the order they appear in the list, so newer
  messages have higher indices. The response includes `offset` (the index
  of the first message in this page) and `total` (total message count).
  """
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found.")
  all_msgs = chat.messages or []
  total = len(all_msgs)
  if before is not None:
    start = max(0, before - limit)
    page = all_msgs[start:before]
  else:
    start = max(0, total - limit)
    page = all_msgs[start:]
  return {
    "id": chat.id,
    "title": chat.title,
    "messages": page,
    "total": total,
    "offset": start,
    "running": is_chat_running(chat_id),
    "session_id": chat.session_id,
  }


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Soft-deletes a chat and stops any running agent for it."""
  # Stop the agent first so it can't write to the chat after we mark it deleted.
  # Use try/finally so a stop error never prevents the soft-delete.
  try:
    await stop_chat_for(chat_id)
  except Exception:
    log.warning("Failed to stop agent for chat %s during delete", chat_id)
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  if chat:
    chat.deleted_at = datetime.now(UTC)
    db.commit()


@router.post("/{chat_id}/recover")
def recover_chat(
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Restores a soft-deleted chat if the TTL window has not expired."""
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.isnot(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found or not deleted.")
  if (datetime.now(UTC).replace(tzinfo=None) - chat.deleted_at) >= SOFT_DELETE_TTL:
    raise HTTPException(status_code=410, detail="Recovery window has expired.")
  chat.deleted_at = None
  db.commit()
  return {"ok": True}
