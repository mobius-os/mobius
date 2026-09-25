"""Run-bound continuity saves from the working agent (``checkpoint_chat``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app import chat_queue, models
from app.broadcast import get_system_broadcast
from app.chat_continuity import apply_checkpoint, note_path, write_note
from app.chat_titles import renamed_event
from app.chat_writer import AuthorizeCheckpoint, await_ack, get_writer
from app.config import get_settings
from app.database import SessionLocal
from app.deps import Principal, get_agent_principal, reject_cross_site


router = APIRouter(tags=["chat-continuity"])


class CheckpointBody(BaseModel):
  model_config = ConfigDict(extra="forbid")

  title: str | None = Field(default=None, max_length=200)
  digest: str | None = Field(default=None, max_length=1_000)
  summary: str | None = Field(default=None, max_length=8_000)


def _save_note(data_dir: str, chat_id: str, body: CheckpointBody) -> dict | None:
  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    if chat is None:
      return None
    path = note_path(data_dir, chat_id)
    try:
      existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
      existing = None
    write_note(path, apply_checkpoint(
      existing, name=chat.title or "",
      digest=(body.digest or "").strip() or None,
      summary=(body.summary or "").strip() or None,
    ))
    return renamed_event(chat)


@router.post(
  "/api/chat/continuity/checkpoints",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def checkpoint_chat(
  body: CheckpointBody,
  principal: Principal = Depends(get_agent_principal),
):
  """Apply one save from the chat's live run to its note and name.

  The per-chat transition lock serializes saves with each other and with
  deletion; the writer admits only the chat's current run.
  """
  chat_id = principal.chat_id or ""
  title = " ".join((body.title or "").split()) or None
  if title is None and not (body.digest or "").strip() and not (body.summary or "").strip():
    return None
  async with chat_queue.get_transition_lock(chat_id):
    result = await await_ack(get_writer().submit(AuthorizeCheckpoint(
      chat_id=chat_id, run_token=principal.run_id or "", title=title,
    )))
    if result.get("status") != "ok":
      raise HTTPException(status_code=409, detail="This run can no longer save this chat.")
    renamed = await run_in_threadpool(
      _save_note, get_settings().data_dir, chat_id, body,
    )
  if renamed is not None and result.get("title_applied"):
    get_system_broadcast().publish(renamed)
  return None
