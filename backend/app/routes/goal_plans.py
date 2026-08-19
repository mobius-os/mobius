"""Owner-authored Goal plans and their live chat-scoped progress events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.broadcast import get_broadcast
from app.database import get_db
from app.deps import (
  Principal,
  get_owner_or_chat_embed_principal,
  reject_cross_site,
  require_chat_embed_operation,
)
from app.goal_plans import (
  GoalPlanConflict,
  GoalPlanError,
  active_goal_rows,
  replace_plan,
  serialize_plan,
  update_task,
)
from app.resource_access import get_active_chat_for_principal


router = APIRouter(prefix="/api/chats", tags=["goal-plans"])


class GoalPlanReplace(BaseModel):
  model_config = ConfigDict(extra="forbid")

  expected_revision: int = Field(ge=0)
  tasks: list[dict[str, Any]]


class GoalTaskUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  expected_revision: int = Field(ge=0)
  status: str | None = None
  note: str | None = None
  result: str | None = None
  progress: dict[str, Any] | None = None

  @model_validator(mode="after")
  def require_change(self) -> "GoalTaskUpdate":
    if (
      self.status is None and self.note is None and self.result is None
      and self.progress is None
    ):
      raise ValueError("provide status, note, result, or progress")
    return self


def _require_owner(principal: Principal) -> None:
  if principal.scope != "owner" or principal.app_id is not None:
    raise HTTPException(
      status_code=403, detail="Only the owner agent may update a Goal plan."
    )


def _active_rows_or_409(db: Session, chat_id: str):
  rows = active_goal_rows(db, chat_id)
  if rows is None:
    raise HTTPException(
      status_code=409, detail="This chat has no active Goal to plan."
    )
  return rows


def _publish(chat_id: str, plan: dict[str, Any]) -> None:
  broadcast = get_broadcast(chat_id)
  if broadcast is None or not broadcast.running:
    return
  broadcast.publish({"type": "goal_plan_updated", "plan": plan})


@router.get("/{chat_id}/goal-plan")
def get_goal_plan(
  chat_id: str,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  require_chat_embed_operation(principal, "chat:read")
  get_active_chat_for_principal(db, chat_id, principal)
  rows = active_goal_rows(db, chat_id)
  return {"plan": serialize_plan(*rows) if rows is not None else None}


@router.put(
  "/{chat_id}/goal-plan",
  dependencies=[Depends(reject_cross_site)],
)
async def put_goal_plan(
  chat_id: str,
  body: GoalPlanReplace,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  get_active_chat_for_principal(db, chat_id, principal)
  from app import chat_queue
  async with chat_queue.get_transition_lock(chat_id):
    db.rollback()
    physical, root = _active_rows_or_409(db, chat_id)
    try:
      plan = replace_plan(
        db, physical=physical, root=root,
        expected_revision=body.expected_revision, tasks=body.tasks,
      )
    except GoalPlanError as exc:
      raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GoalPlanConflict as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  _publish(chat_id, plan)
  return {"plan": plan}


@router.patch(
  "/{chat_id}/goal-plan/tasks/{task_id}",
  dependencies=[Depends(reject_cross_site)],
)
async def patch_goal_task(
  chat_id: str,
  task_id: str,
  body: GoalTaskUpdate,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  get_active_chat_for_principal(db, chat_id, principal)
  from app import chat_queue
  async with chat_queue.get_transition_lock(chat_id):
    db.rollback()
    physical, root = _active_rows_or_409(db, chat_id)
    try:
      plan = update_task(
        db, physical=physical, root=root,
        expected_revision=body.expected_revision,
        task_id=task_id,
        changes={
          "status": body.status,
          "note": body.note,
          "result": body.result,
          "progress": body.progress,
        },
      )
    except GoalPlanError as exc:
      raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GoalPlanConflict as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  _publish(chat_id, plan)
  return {"plan": plan}
