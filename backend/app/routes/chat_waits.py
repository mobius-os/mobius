"""Declare, list, and cancel durable chat waits.

Declaring runs under the exact agent-run bearer: a wait resumes its chat with
full owner authority and its check runs as the backend user, so an app-scoped
token must never be able to create one. Listing and cancelling are owner
surfaces (the chat UI's waiting card and its cancel affordance).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app import models
from app.chat_waits import (
  WaitValidationError,
  cancel_wait,
  declare_wait,
  outstanding_waits_for_chat,
  serialize_wait,
)
from app.database import get_db
from app.deps import (
  Principal,
  get_agent_run_principal,
  get_owner_or_chat_embed_principal,
  get_principal,
  require_chat_embed_operation,
)
from app.resource_access import get_active_chat_or_404, require_active_chat_access
from app.wait_checks import GitHubChecks

router = APIRouter(prefix="/api/chat-waits", tags=["chat-waits"])


def _require_owner(principal: Principal) -> None:
  if (
    principal.scope != "owner"
    or principal.app_id is not None
    or principal.delegation_id is not None
  ):
    raise HTTPException(status_code=403, detail="Owner authority required.")


class WaitDeclare(BaseModel):
  description: str = Field(min_length=1, max_length=500)
  condition_owner: str | None = Field(default=None, max_length=160)
  kind: str = Field(pattern="^(command|timer|github_checks)$")
  github_checks: GitHubChecks | None = None
  command: str | None = Field(default=None, max_length=4000)
  delay_secs: int | None = Field(default=None, gt=0)
  interval_secs: int | None = Field(default=None, gt=0)
  deadline_secs: int | None = Field(default=None, gt=0)

  @model_validator(mode="after")
  def require_bounded_command_owner(self) -> "WaitDeclare":
    if self.kind not in ("command", "github_checks"):
      return self
    if self.kind == "command" and not (self.condition_owner or "").strip():
      raise ValueError("command waits need a condition_owner")
    if self.deadline_secs is None:
      raise ValueError("command and GitHub waits need an explicit deadline_secs")
    return self


@router.post("")
def declare(
  payload: WaitDeclare,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  get_active_chat_or_404(db, principal.chat_id)
  try:
    row = declare_wait(
      db,
      chat_id=principal.chat_id,
      description=payload.description,
      condition_owner=payload.condition_owner,
      kind=payload.kind,
      command=payload.command,
      delay_secs=payload.delay_secs,
      interval_secs=payload.interval_secs,
      deadline_secs=payload.deadline_secs,
      created_by_run_id=principal.run_id,
      github_checks=payload.github_checks.model_dump() if payload.github_checks else None,
    )
  except WaitValidationError as exc:
    raise HTTPException(status_code=422, detail=str(exc))
  return serialize_wait(row, db=db)


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
  if include_settled:
    rows = db.query(models.ChatWait).filter(
      models.ChatWait.chat_id == chat_id,
    ).order_by(models.ChatWait.created_at.asc()).limit(50).all()
  else:
    rows = outstanding_waits_for_chat(db, chat_id)[:50]
  return {"waits": [serialize_wait(row, db=db) for row in rows]}


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
  if row.kind == "platform_activation":
    raise HTTPException(
      status_code=409,
      detail="Use the Restart card or Stop the chat instead.",
    )
  return cancel_wait(db, row)


class DecisionFollow(BaseModel):
  chat_id: str = Field(min_length=1, max_length=64)
  question_id: str = Field(min_length=1, max_length=64)


@router.post("/decision")
def wait_for_decision(
  payload: DecisionFollow,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Only the caller's exact open top-level Goal gains a subscription."""
  _require_owner(principal)
  from app.resource_access import get_active_chat_for_principal
  get_active_chat_for_principal(db, payload.chat_id, principal)
  from app.decision_cards import stage_wait_for_decision
  try:
    row = stage_wait_for_decision(db, chat_id=principal.chat_id,
      run_id=principal.run_id, source_chat_id=payload.chat_id,
      question_id=payload.question_id)
    db.commit()
  except ValueError as exc:
    db.rollback()
    raise HTTPException(status_code=422, detail=str(exc))
  following = row.result_disposition is None and row.withdrawn_at is None
  return {"dependency_id": row.id, "goal_id": row.waiting_goal_id,
    "status": row.result_disposition or ("result_owed" if row.resolved_at else "waiting"),
    "outcome": row.outcome_json,
    "next_action": ("Do independent work first. This exact Goal receives the saved outcome when eligible; "
      "no polling or peer interrupt is needed. An answer grants authority only to the card's own executor."
      if following else "This dependency is already disposed; no new continuation was requested. "
      "Its saved outcome is evidence, never authority.")}


def _withdraw_dependency_chat(dependency_id: str, principal_chat_id: str | None):
  from app.database import SessionLocal
  with SessionLocal() as db:
    row = db.get(models.GoalDependency, dependency_id)
    if row is None:
      raise HTTPException(status_code=404, detail="No such follow.")
    if principal_chat_id is not None and principal_chat_id != row.waiting_chat_id:
      raise HTTPException(status_code=403, detail="Not this chat's follow.")
    get_active_chat_or_404(db, row.waiting_chat_id)
    return row.waiting_chat_id


@router.get("/dependencies/{dependency_id}/text")
def read_dependency_text(
  dependency_id: str,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """The whole claim texts a Goal view showed in part, for its waiting chat."""
  require_chat_embed_operation(principal, "chat:read")
  row = db.get(models.GoalDependency, dependency_id)
  if row is None:
    raise HTTPException(status_code=404, detail="No such follow.")
  require_active_chat_access(db, row.waiting_chat_id, principal)
  from app.goal_dependencies import dependency_full_texts
  return dependency_full_texts(
    db, row, include_foreign_context=principal.scope != "chat_embed",
  )


@router.post("/dependencies/{dependency_id}/withdraw")
async def withdraw_dependency(
  dependency_id: str,
  principal: Principal = Depends(get_principal),
):
  """Owner control bound to the waiting chat, never the foreign source."""
  _require_owner(principal)
  chat_id = await asyncio.to_thread(
    _withdraw_dependency_chat, dependency_id, principal.chat_id,
  )
  from app.chat_writer import WithdrawGoalDependency, await_ack, get_writer
  result = await await_ack(get_writer().submit(WithdrawGoalDependency(
    chat_id=chat_id, dependency_id=dependency_id)))
  if result != "withdrawn":
    raise HTTPException(status_code=404 if result == "missing" else 409,
      detail="This follow is no longer available to stop.")
  return {"id": dependency_id, "disposition": "withdrawn"}
