"""Declare, list, and cancel durable chat waits.

Declaring runs under the exact agent-run bearer: a wait resumes its chat with
full owner authority and its check runs as the backend user, so an app-scoped
token must never be able to create one. Listing and cancelling are owner
surfaces (the chat UI's waiting card and its cancel affordance).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.chat_waits import (
  WaitValidationError,
  cancel_wait,
  declare_wait,
  serialize_wait,
)
from app.database import get_db
from app.deps import Principal, get_agent_run_principal, get_principal
from app.resource_access import get_active_chat_or_404

router = APIRouter(prefix="/api/chat-waits", tags=["chat-waits"])


def _require_owner(principal: Principal) -> None:
  if principal.scope != "owner" or principal.app_id is not None:
    raise HTTPException(status_code=403, detail="Owner authority required.")


class WaitDeclare(BaseModel):
  description: str = Field(min_length=1, max_length=500)
  kind: str = Field(pattern="^(command|timer)$")
  command: str | None = Field(default=None, max_length=4000)
  delay_secs: int | None = Field(default=None, gt=0)
  interval_secs: int | None = Field(default=None, gt=0)
  deadline_secs: int | None = Field(default=None, gt=0)


@router.post("")
def declare(
  payload: WaitDeclare,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  get_active_chat_or_404(db, principal.chat_id)
  try:
    row = declare_wait(
      db,
      chat_id=principal.chat_id,
      description=payload.description,
      kind=payload.kind,
      command=payload.command,
      delay_secs=payload.delay_secs,
      interval_secs=payload.interval_secs,
      deadline_secs=payload.deadline_secs,
      created_by_run_id=principal.run_id,
    )
  except WaitValidationError as exc:
    raise HTTPException(status_code=422, detail=str(exc))
  return serialize_wait(row)


@router.get("")
def list_waits(
  chat_id: str,
  include_settled: bool = False,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  # An agent-run bearer reads only its own chat's waits, mirroring cancel;
  # the plain owner token (no chat binding) may read any chat's.
  if principal.chat_id is not None and principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Not this chat's waits.")
  get_active_chat_or_404(db, chat_id)
  query = db.query(models.ChatWait).filter(
    models.ChatWait.chat_id == chat_id,
  )
  if not include_settled:
    query = query.filter(models.ChatWait.status == "armed")
  rows = query.order_by(models.ChatWait.created_at.asc()).limit(50).all()
  return {"waits": [serialize_wait(row) for row in rows]}


@router.post("/{wait_id}/cancel")
def cancel(
  wait_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  row = db.query(models.ChatWait).filter(
    models.ChatWait.id == wait_id,
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="No such wait.")
  # An agent-run bearer may cancel only its own chat's waits; the plain owner
  # token may cancel any.
  if principal.chat_id is not None and principal.chat_id != row.chat_id:
    raise HTTPException(status_code=403, detail="Not this chat's wait.")
  return serialize_wait(cancel_wait(db, row))
