"""Run-bound checkpoint writes and owner-readable chat continuity."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm import load_only
from starlette.concurrency import run_in_threadpool

from app import chat_queue, models
from app.broadcast import get_system_broadcast
from app.chat_continuity import (
  MAX_ENTRY_LIMIT,
  completed_prefix,
  continuity_wire,
  project_continuity,
  read_legacy_note,
)
from app.chat_titles import renamed_event
from app.chat_writer import CheckpointContinuity, await_ack, get_writer
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.deps import Principal, get_agent_principal, get_current_owner, reject_cross_site


router = APIRouter(tags=["chat-continuity"])
log = logging.getLogger("moebius.chat.continuity")


class SourceCursor(BaseModel):
  model_config = ConfigDict(extra="forbid")

  message_count: int = Field(ge=0)
  prefix_hash: str | None = Field(default=None, min_length=64, max_length=64)


class CheckpointBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  checkpoint_id: str = Field(min_length=1, max_length=128)
  expected_revision: int = Field(ge=0)
  digest: str = Field(min_length=1, max_length=8_000)
  summary: str | None = Field(default=None, max_length=12_000)
  title: str | None = Field(default=None, max_length=256)
  source_cursor: SourceCursor | None = None


def _active_chat(db: Session, chat_id: str) -> models.Chat:
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).options(load_only(
    models.Chat.id,
    models.Chat.title,
    models.Chat.title_locked,
    models.Chat.updated_at,
    models.Chat.activity_at,
  )).one_or_none()
  if chat is None:
    raise HTTPException(status_code=404, detail="Chat not found.")
  return chat


def _project_from_fresh_session(data_dir: str, chat_id: str) -> None:
  with SessionLocal() as db:
    project_continuity(data_dir, db, chat_id)


@router.get("/api/chat/continuity")
async def read_agent_continuity(
  after_revision: int | None = Query(default=None, ge=0),
  limit: int = Query(default=5, ge=1, le=MAX_ENTRY_LIMIT),
  full: bool = False,
  principal: Principal = Depends(get_agent_principal),
  db: Session = Depends(get_db),
):
  chat = _active_chat(db, principal.chat_id or "")
  result = continuity_wire(
    db, chat, after_revision=after_revision, limit=limit, full=full,
    data_dir=get_settings().data_dir,
  )
  messages = db.query(models.Chat.messages).filter(
    models.Chat.id == chat.id,
  ).scalar() or []
  count, prefix_hash = completed_prefix(messages, principal.run_id or "")
  result["source_cursor"] = {
    "message_count": count, "prefix_hash": prefix_hash,
  }
  result["has_uncovered_source"] = (
    result["coverage"] != result["source_cursor"]
  )
  return result


@router.post(
  "/api/chat/continuity/checkpoints",
  dependencies=[Depends(reject_cross_site)],
)
async def checkpoint_agent_continuity(
  body: CheckpointBody,
  principal: Principal = Depends(get_agent_principal),
  db: Session = Depends(get_db),
):
  chat_id = principal.chat_id or ""
  run_id = principal.run_id or ""
  data_dir = get_settings().data_dir
  projection_warning = None
  async with chat_queue.get_transition_lock(chat_id):
    _active_chat(db, chat_id)
    state = db.get(models.ChatContinuity, chat_id)
    legacy = read_legacy_note(data_dir, chat_id) if state is None else None
    result = await await_ack(get_writer().submit(CheckpointContinuity(
      chat_id=chat_id,
      run_token=run_id,
      checkpoint_id=body.checkpoint_id,
      expected_revision=body.expected_revision,
      digest=body.digest,
      summary=body.summary,
      title=body.title,
      source_cursor=(body.source_cursor.model_dump() if body.source_cursor else None),
      legacy_markdown=legacy,
    )))
    if result.get("status") in {"conflict", "stale_run"}:
      raise HTTPException(status_code=409, detail=result)
    try:
      await run_in_threadpool(_project_from_fresh_session, data_dir, chat_id)
    except Exception:
      projection_warning = "The durable checkpoint committed, but its file projection needs repair."
      log.warning(
        "continuity projection failed chat_id=%s revision=%s",
        chat_id, result.get("revision"), exc_info=True,
      )
    db.rollback()
    chat = _active_chat(db, chat_id)
    title_event = renamed_event(chat) if result.get("title_applied") else None
  if title_event is not None:
    get_system_broadcast().publish(title_event)
  if projection_warning is not None:
    result["projection_warning"] = projection_warning
  return result


@router.get("/api/chats/{chat_id}/continuity")
async def read_owner_continuity(
  chat_id: str,
  after_revision: int | None = Query(default=None, ge=0),
  limit: int = Query(default=5, ge=1, le=MAX_ENTRY_LIMIT),
  full: bool = False,
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  chat = _active_chat(db, chat_id)
  return continuity_wire(
    db, chat, after_revision=after_revision, limit=limit, full=full,
    data_dir=get_settings().data_dir,
  )
