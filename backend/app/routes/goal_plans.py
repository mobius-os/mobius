"""Owner-authored Goal plans and their live chat-scoped progress events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.broadcast import get_broadcast
from app.database import get_db
from app.deps import (
  Principal,
  get_agent_run_principal,
  get_owner_or_chat_embed_principal,
  reject_cross_site,
  require_chat_embed_operation,
)
from app.goal_plans import (
  GoalPlanConflict,
  GoalPlanError,
  active_goal_rows,
  presented_goal_rows,
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


class GoalPromotionRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")

  objective: str = Field(min_length=1, max_length=1000)

  @field_validator("objective")
  @classmethod
  def clean_objective(cls, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
      raise ValueError("objective must not be empty")
    return cleaned


class GoalClearRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")

  goal_id: str = Field(min_length=1, max_length=64)


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
  if (
    principal.scope != "owner"
    or principal.app_id is not None
    or principal.delegation_id is not None
  ):
    raise HTTPException(
      status_code=403, detail="Only the owner agent may update a Goal plan."
    )


def _active_rows_or_409(db: Session, chat_id: str, principal=None):
  rows = active_goal_rows(db, chat_id)
  if rows is None:
    raise HTTPException(status_code=409, detail={
      "code": "no_active_goal", "message": "This chat has no active Goal to plan.",
    })
  if principal is not None and principal.run_id is not None and (
    rows[0].id != principal.run_id or rows[0].status != "running"
  ):
    raise HTTPException(status_code=409, detail="This execution attempt no longer owns the Goal.")
  return rows


def _plan_refusal(exc: GoalPlanError) -> HTTPException:
  """A typed 422: stable code and facts beside the client-neutral message."""
  return HTTPException(status_code=422, detail={
    **exc.facts, "code": exc.code, "message": str(exc),
  })


def _publish(chat_id: str, plan: dict[str, Any]) -> None:
  broadcast = get_broadcast(chat_id)
  if broadcast is None or not broadcast.running:
    return
  broadcast.publish({"type": "goal_plan_updated", "plan": plan})


@router.get("/{chat_id}/goal-plan")
def get_goal_plan(
  chat_id: str,
  goal_id: str | None = None,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  require_chat_embed_operation(principal, "chat:read")
  get_active_chat_for_principal(db, chat_id, principal)
  rows = presented_goal_rows(db, chat_id)
  if goal_id is not None:
    from app import models
    from app.goal_plans import _goal_rows_for_physical
    run = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == chat_id, models.ChatRun.goal_id == goal_id,
    ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    if run is None:
      raise HTTPException(status_code=404, detail="Goal not found in this chat.")
    rows = _goal_rows_for_physical(db, run)
  plan = serialize_plan(db, *rows) if rows is not None else None
  return {
    "plan": plan,
    # A saved plan that no longer validates serializes as None, like no plan.
    # Name it so callers repair it instead of treating the Goal as unplanned.
    "plan_unreadable": (
      rows is not None and rows[1].plan_json is not None and plan is None
    ),
    "goal": ({"id": rows[1].id, "revision": rows[1].revision,
              "status": rows[1].status, "objective": rows[1].objective,
              "checkpoint": rows[1].checkpoint, "next_action": rows[1].next_action}
             if rows else None),
  }


@router.post(
  "/{chat_id}/goal",
  dependencies=[Depends(reject_cross_site)],
)
async def promote_current_run_to_goal(
  chat_id: str,
  body: GoalPromotionRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Attach the caller's exact running turn to a platform-owned Goal."""
  _require_owner(principal)
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal)
  from app import chat_queue
  from app.chat_writer import (
    GoalPromotionRejected,
    PromoteRunToGoal,
    await_ack,
    get_writer,
  )
  async with chat_queue.get_transition_lock(chat_id):
    result = await await_ack(get_writer().submit(PromoteRunToGoal(
      chat_id=chat_id,
      run_token=principal.run_id or "",
      objective=body.objective,
    )))
  if isinstance(result, GoalPromotionRejected):
    messages = {
      "run_not_active": "The initiating agent turn is no longer active.",
      "unfinished_goal_exists": "This chat already has unfinished Goal work. Resume it rather than creating a replacement.",
      "run_not_current": "A newer agent turn now owns this chat.",
      "different_goal_active": "This turn already owns a different Goal.",
      "logical_root_missing": "The running turn has no durable logical root.",
    }
    raise HTTPException(
      status_code=409,
      detail=messages.get(result.reason, "This turn cannot become a Goal."),
    )
  if result["state"] == "promoted":
    broadcast = get_broadcast(chat_id)
    if broadcast is not None and broadcast.running:
      broadcast.publish({
        "type": "goal_activated",
        "objective": result["objective"],
        "root_run_id": result["root_run_id"],
        "run_id": result["run_id"],
      })
  return result


@router.delete(
  "/{chat_id}/goal",
  dependencies=[Depends(reject_cross_site)],
)
async def clear_presented_goal(
  chat_id: str,
  body: GoalClearRequest,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Dismiss one exact Goal; stop execution only while work is unfinished."""
  if principal.delegation_id is not None:
    raise HTTPException(
      status_code=403, detail="A delegated child cannot clear a Goal."
    )
  require_chat_embed_operation(principal, "chat:stop")
  get_active_chat_for_principal(db, chat_id, principal)
  from app.chat import clear_goal_for

  result = await clear_goal_for(chat_id, body.goal_id)
  if result["status"] == "still_running":
    raise HTTPException(
      status_code=409,
      detail="The Goal is still stopping; confirm again in a moment.",
    )
  if result["status"] == "conflict":
    raise HTTPException(
      status_code=409,
      detail="A newer Goal replaced the one this confirmation targeted.",
    )
  if result["status"] == "missing":
    return {"cleared": False, "goal": None}
  # Dismissal released the Goal's open work claims in its own commit; wake
  # the followers so they may take the exact action over.
  from app.agent_coordination import settle_claims_with_owner
  await settle_claims_with_owner(chat_id)
  broadcast = get_broadcast(chat_id)
  if broadcast is not None and broadcast.running:
    broadcast.publish({
      "type": "goal_cleared",
      "goal_id": result["goal_id"],
    })
  return {"cleared": True, "goal": None}


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
    physical, root = _active_rows_or_409(db, chat_id, principal)
    try:
      plan = replace_plan(
        db, physical=physical, root=root,
        expected_revision=body.expected_revision, tasks=body.tasks,
      )
    except GoalPlanError as exc:
      raise _plan_refusal(exc) from exc
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
    physical, root = _active_rows_or_409(db, chat_id, principal)
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
      raise _plan_refusal(exc) from exc
    except GoalPlanConflict as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  _publish(chat_id, plan)
  return {"plan": plan}


class GoalRecordUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  goal_id: str
  expected_revision: int = Field(ge=0)
  checkpoint: str | None = Field(default=None, max_length=4000)
  next_action: str | None = Field(default=None, max_length=2000)
  result: str | None = Field(default=None, min_length=1, max_length=4000)
  finished_claims: list[str] = Field(default_factory=list, max_length=50)

  @model_validator(mode="after")
  def require_operation(self):
    if self.finished_claims and self.result is None:
      raise ValueError("Only a completion can name finished claims.")
    if self.result is not None:
      if self.checkpoint is not None or self.next_action is not None:
        raise ValueError("Complete or checkpoint, not both.")
    elif self.checkpoint is None or self.next_action is None:
      raise ValueError("A checkpoint needs both progress and the next action.")
    return self


@router.patch("/{chat_id}/goal", dependencies=[Depends(reject_cross_site)])
async def patch_goal_record(
  chat_id: str, body: GoalRecordUpdate,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal)
  from app import chat_queue
  from app.goals import update_goal_record
  async with chat_queue.get_transition_lock(chat_id):
    db.rollback()
    run, goal = _active_rows_or_409(db, chat_id, principal)
    if run.id != principal.run_id or goal.id != body.goal_id:
      raise HTTPException(status_code=409, detail="The Goal or execution attempt changed.")
    try:
      result = update_goal_record(
        db, run, goal, body.expected_revision, checkpoint=body.checkpoint,
        next_action=body.next_action, result=body.result,
        finished_claims=body.finished_claims,
      )
    except GoalPlanError as exc:
      raise _plan_refusal(exc) from exc
    except GoalPlanConflict as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  _publish(chat_id, serialize_plan(db, run, goal))
  if result.get("status") == "completed":
    # Completion settled the Goal's open work claims with its verified result
    # in the same commit; wake the followers, so the owner needs no trailing
    # finish_agent_work call.
    from app.agent_coordination import settle_claims_with_owner
    await settle_claims_with_owner(chat_id)
  return result


class GoalTaskAdd(BaseModel):
  model_config = ConfigDict(extra="forbid")
  expected_revision: int = Field(ge=0)
  task: dict[str, Any]


@router.post("/{chat_id}/goal-plan/tasks", dependencies=[Depends(reject_cross_site)])
async def add_goal_task(
  chat_id: str, body: GoalTaskAdd,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  _require_owner(principal)
  get_active_chat_for_principal(db, chat_id, principal)
  from app import chat_queue
  async with chat_queue.get_transition_lock(chat_id):
    db.rollback()
    run, goal = _active_rows_or_409(db, chat_id, principal)
    tasks = list((goal.plan_json or {}).get("tasks") or [])
    try:
      plan = replace_plan(db, physical=run, root=goal,
                          expected_revision=body.expected_revision,
                          tasks=[*tasks, body.task])
    except GoalPlanError as exc:
      raise _plan_refusal(exc) from exc
    except GoalPlanConflict as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  _publish(chat_id, plan)
  return {"plan":plan}


@router.get("/{chat_id}/goals")
def list_goals(
  chat_id: str,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  require_chat_embed_operation(principal, "chat:read")
  get_active_chat_for_principal(db, chat_id, principal)
  from app import models
  return {"goals": [
    {"id":goal.id, "objective":goal.objective, "status":goal.status,
     "revision":goal.revision, "checkpoint":goal.checkpoint,
     "next_action":goal.next_action}
    for goal in db.query(models.ChatGoal).filter(
      models.ChatGoal.chat_id == chat_id,
    ).order_by(models.ChatGoal.created_at.desc()).all()
  ]}


class GoalResumeRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  goal_id: str = Field(min_length=1, max_length=64)


@router.post("/{chat_id}/goal/resume", dependencies=[Depends(reject_cross_site)])
async def attach_unfinished_goal(
  chat_id: str, body: GoalResumeRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Attach an ordinary owner attempt to named existing work, never replace it."""
  _require_owner(principal)
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal)
  from app import models, chat_queue
  from app.chat_writer import PromoteRunToGoal, GoalPromotionRejected, get_writer, await_ack
  goal = db.get(models.ChatGoal, body.goal_id)
  if goal is None or goal.chat_id != chat_id:
    raise HTTPException(status_code=404, detail="Goal not found in this chat.")
  async with chat_queue.get_transition_lock(chat_id):
    result = await await_ack(get_writer().submit(PromoteRunToGoal(
      chat_id=chat_id, run_token=principal.run_id, objective=goal.objective,
      resume_goal_id=goal.id,
    )))
  if isinstance(result, GoalPromotionRejected):
    raise HTTPException(status_code=409, detail="Goal cannot attach: " + result.reason)
  if result["state"] == "promoted":
    broadcast = get_broadcast(chat_id)
    if broadcast is not None and broadcast.running:
      broadcast.publish({"type":"goal_activated", **result})
  return result


@router.get("/{chat_id}/goal-context")
def get_goal_context(
  chat_id: str, task: str | None = None, goal_id: str | None = None,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  require_chat_embed_operation(principal, "chat:read")
  get_active_chat_for_principal(db, chat_id, principal)
  from app import models
  from app.goals import scoped_goal_context
  if goal_id is not None:
    goal = db.get(models.ChatGoal, goal_id)
    if goal is None or goal.chat_id != chat_id:
      raise HTTPException(status_code=404, detail="Goal not found in this chat.")
  else:
    rows = presented_goal_rows(db, chat_id)
    goal = rows[1] if rows else None
  if goal is None:
    return {"context": None}
  try:
    return {"context": scoped_goal_context(db, goal, task)}
  except ValueError as exc:
    raise HTTPException(status_code=404, detail=str(exc)) from exc
