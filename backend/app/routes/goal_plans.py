"""Owner-authored Goal plans and their live chat-scoped progress events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
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
  serialize_plan,
)
from app.resource_access import get_active_chat_for_principal


router = APIRouter(prefix="/api/chats", tags=["goal-plans"])


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
  if broadcast is not None and broadcast.running:
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
    run = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == chat_id, models.ChatRun.goal_id == goal_id,
    ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    goal = db.get(models.ChatGoal, goal_id)
    if goal is None or goal.chat_id != chat_id:
      raise HTTPException(status_code=404, detail="Goal not found in this chat.")
    if run is None and goal.completion_run_id is not None:
      run = db.get(models.ChatRun, goal.completion_run_id)
    if run is None:
      run = db.query(models.ChatRun).filter(
        models.ChatRun.chat_id == chat_id,
      ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    if run is None:
      raise HTTPException(status_code=404, detail="Goal has no execution in this chat.")
    rows = (run, goal)
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
        "type": "goal_activated", "objective": result["objective"],
        "root_run_id": result["root_run_id"], "run_id": result["run_id"],
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
    broadcast.publish({"type": "goal_cleared", "goal_id": result["goal_id"]})
  return {"cleared": True, "goal": None}


class CannotCompleteOutcome(BaseModel):
  model_config = ConfigDict(extra="forbid")
  reason: str = Field(min_length=1, max_length=1500)
  efforts: str = Field(min_length=1, max_length=2000)
  unmet_outcome: str = Field(min_length=1, max_length=1000)


class GoalUpdateRequest(BaseModel):
  """One atomic agent-facing Goal plan and outcome operation."""

  model_config = ConfigDict(extra="forbid")
  goal_id: str | None = Field(default=None, min_length=1, max_length=64)
  tasks: list[dict[str, Any]] | None = Field(default=None, min_length=1)
  next_action: str | None = Field(default=None, min_length=1, max_length=2000)
  complete: bool | str | None = None
  cannot_complete: CannotCompleteOutcome | None = None
  cancel: str | None = Field(default=None, min_length=1, max_length=4000)
  defer: str | None = Field(default=None, min_length=1, max_length=2000)
  finished_claims: list[str] = Field(default_factory=list, max_length=50)

  @field_validator("complete", mode="before")
  @classmethod
  def completion_signal(cls, value):
    if value is None or value is True:
      return value
    if isinstance(value, str) and value.strip() and len(value) <= 4000:
      return value
    raise ValueError("complete must be true")

  @model_validator(mode="after")
  def one_record_operation(self) -> "GoalUpdateRequest":
    if sum(value is not None for value in (
      self.complete, self.cannot_complete, self.cancel, self.defer, self.next_action,
    )) > 1:
      raise ValueError("Choose one outcome, deferral, or next action.")
    if self.finished_claims and self.complete is None:
      raise ValueError("Only a completion can name finished claims.")
    return self

  @property
  def changes_anything(self) -> bool:
    return any(
      value is not None
      for value in (self.goal_id, self.tasks, self.next_action, self.complete,
                    self.cannot_complete, self.cancel, self.defer)
    )


def _goal_summary(db: Session, goal) -> dict[str, Any]:
  from app.agent_work_claims import open_claim_keys
  from app.goals import goal_hold
  return {
    "hold": goal_hold(goal) if goal.status == "stopped" else None,
    "id": goal.id, "status": goal.status, "revision": goal.revision,
    "objective": goal.objective, "next_action": goal.next_action,
    "result": goal.result,
    "held_work_keys": sorted(open_claim_keys(db, chat_id=goal.chat_id, goal_id=goal.id)),
  }


async def _attach_run_to_goal(db: Session, chat_id: str, principal: Principal,
                              goal_id: str | None, *, deferring: bool = False):
  """Return this attempt's Goal rows, attaching the attempt when needed.

  An ordinary turn that resumes unfinished work is not yet bound to the Goal
  the chat presents; binding it here means a plan write never needs a
  separate resume step. Caller holds the chat transition lock.
  """
  rows = active_goal_rows(db, chat_id)
  if (
    rows is not None and rows[0].id == principal.run_id
    and rows[0].status == "running" and (rows[1].status != "stopped" or deferring)
    and (goal_id is None or rows[1].id == goal_id)
  ):
    return rows
  from app import models
  from app.chat_writer import (
    GoalPromotionRejected, PromoteRunToGoal, await_ack, get_writer,
  )
  if goal_id is not None:
    target = db.get(models.ChatGoal, goal_id)
    if target is None or target.chat_id != chat_id:
      raise HTTPException(status_code=404, detail="Goal not found in this chat.")
  else:
    presented = presented_goal_rows(db, chat_id)
    target = presented[1] if presented else None
  # Naming a held Goal is a deliberate reattachment. An implicit plan write
  # must never undo a hold simply because that Goal remains on screen.
  allowed = {"open", "stopped"} if goal_id is not None else {"open"}
  if target is None or target.status not in allowed:
    raise HTTPException(status_code=409, detail={
      "code": "no_active_goal",
      "message": "This chat has no open Goal to update. Promote one first.",
    })
  result = await await_ack(get_writer().submit(PromoteRunToGoal(
    chat_id=chat_id, run_token=principal.run_id or "",
    objective=target.objective, resume_goal_id=target.id,
  )))
  if isinstance(result, GoalPromotionRejected):
    raise HTTPException(
      status_code=409, detail="Goal cannot attach: " + result.reason,
    )
  if result["state"] == "promoted":
    broadcast = get_broadcast(chat_id)
    if broadcast is not None and broadcast.running:
      broadcast.publish({"type": "goal_activated", **result})
  db.rollback()
  return _active_rows_or_409(db, chat_id, principal)


@router.get("/{chat_id}/goal-brief")
def read_goal_brief(
  chat_id: str,
  task: str | None = Query(default=None, min_length=1, max_length=64),
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """read_goal: expand one task of the calling agent's own Goal, on demand.

  The same projection a turn receives, refocused on ``task``. Without a
  task a helper re-reads its assignment brief; a coordinator is pointed to
  update_goal with no arguments, its one overview. A helper reads only the
  Goal its delegation chain anchors; the owner's next step and
  other Goals never reach it. Read-only: it never attaches a run or lifts a
  hold. The full saved plan stays at goal-plan.
  """
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal)
  from app.goals import read_goal_view
  try:
    return read_goal_view(
      db, chat_id, principal.run_id, delegation_id=principal.delegation_id,
      task_id=task,
    )
  except PermissionError as exc:
    raise HTTPException(status_code=403, detail=str(exc)) from exc
  except ValueError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc


class CompactionBriefRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  thread_id: str = Field(min_length=1, max_length=128)


@router.post("/{chat_id}/goal-brief/compaction", dependencies=[Depends(reject_cross_site)])
async def take_goal_brief_after_compaction(
  chat_id: str,
  body: CompactionBriefRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Codex's post-compaction hook: this live turn's fresh brief, once.

  Codex runs this hook only after it compacts, so the call itself marks the
  need. Only the run that owns the streaming turn on that exact Codex thread
  may take it, never a finished or stopping one. Empty context means no Goal
  or a failed read; the next turn's brief and read_goal then cover it.
  """
  from app.chat_event_sink import get_active_sink
  from app.runner_registry import RunnerKind, registry

  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal, load_fields=())
  sink = get_active_sink(chat_id)
  handle = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  if sink is None or sink.run_token != principal.run_id or handle is None:
    raise HTTPException(status_code=409, detail="This run has no live Codex turn.")
  db.close()  # The brief loads in its own short session; hold no connection.
  try:
    context = await handle.goal_brief_after_compaction(body.thread_id)
  except LookupError as exc:
    raise HTTPException(status_code=409, detail=str(exc)) from exc
  return {"context": context}


@router.post("/{chat_id}/goal/update", dependencies=[Depends(reject_cross_site)])
async def update_goal(
  chat_id: str, body: GoalUpdateRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Edit the plan and optionally settle the Goal, as one atomic revision.

  With no fields it reads the presented Goal without attaching to it.
  """
  _require_owner(principal)
  if principal.chat_id != chat_id:
    raise HTTPException(status_code=403, detail="Agent run belongs to another chat.")
  get_active_chat_for_principal(db, chat_id, principal)
  if not body.changes_anything:
    rows = presented_goal_rows(db, chat_id)
    if rows is None:
      return {"goal": None, "plan": None}
    return {"goal": _goal_summary(db, rows[1]), "plan": serialize_plan(db, *rows)}
  from app import chat_queue
  from app.goals import update_goal_record
  record = None
  async with chat_queue.get_transition_lock(chat_id):
    db.rollback()
    run, goal = await _attach_run_to_goal(db, chat_id, principal, body.goal_id,
                                         deferring=body.defer is not None)
    try:
      if any(value is not None for value in (
        body.tasks, body.next_action, body.complete, body.cannot_complete, body.cancel, body.defer,
      )):
        record = update_goal_record(
          db, run, goal, goal.revision,
          tasks=body.tasks,
          checkpoint="Plan saved." if body.next_action is not None else None,
          next_action=body.next_action, complete=body.complete,
          cannot_complete=(body.cannot_complete.model_dump() if body.cannot_complete else None),
          cancel=body.cancel, defer=body.defer,
          finished_claims=body.finished_claims,
        )
        db.refresh(goal)
    except (GoalPlanError, GoalPlanConflict) as exc:
      db.rollback()
      if isinstance(exc, GoalPlanError):
        refusal = _plan_refusal(exc)
        raise refusal from exc
      raise HTTPException(status_code=409, detail=str(exc)) from exc
    plan = serialize_plan(db, run, goal)
  if plan is not None:
    _publish(chat_id, plan)
  if record is not None and record.get("status") in {"completed", "cannot_complete", "cancelled"}:
    # Completion settled the Goal's claims and fired Waits in the same commit;
    # wake claim followers and withdraw now-stale resume notices.
    from app.goals import settle_after_goal_completion
    await settle_after_goal_completion(chat_id)
  elif record is not None and record.get("status") == "stopped":
    from app.agent_coordination import settle_claims_with_owner
    await settle_claims_with_owner(chat_id)
  return {"goal": _goal_summary(db, goal), "plan": plan}


class HelperTaskUpdateRequest(BaseModel):
  """A partial checklist edit, never an outcome or whole-plan replacement."""
  model_config = ConfigDict(extra="forbid")
  tasks: list[dict[str, Any]] = Field(min_length=1)


@router.post("/{chat_id}/goal/tasks", dependencies=[Depends(reject_cross_site)])
async def update_helper_goal_tasks(
  chat_id: str, body: HelperTaskUpdateRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """Merge helper patches under the same lock and revision as coordinator edits.

  No parent wake or approval handshake: the UI invalidation is enough. A
  routine write returns only touched task states, not a fresh full brief.
  """
  from app import chat_queue, models
  from app.goal_plans import goal_assignment, stage_helper_task_edits
  from app.goals import update_goal_record

  if principal.chat_id != chat_id or principal.delegation_id is None:
    raise HTTPException(status_code=403, detail="Only the assigned helper may edit its branch.")
  get_active_chat_for_principal(db, chat_id, principal)
  assignment = goal_assignment(db, chat_id)
  if assignment is None or assignment.delegation_id != principal.delegation_id:
    raise HTTPException(status_code=403, detail="This helper has no Goal assignment.")
  owner_chat_id, goal_id = assignment.goal.chat_id, assignment.goal.id
  # Follow cancellation's ancestor-before-descendant lock order. A helper
  # being stopped must settle before its write can recheck run authority.
  async with (chat_queue.get_transition_lock(owner_chat_id),
              chat_queue.get_transition_lock(chat_id)):
    # The caller may have waited behind a coordinator edit or Stop. Resolve
    # authority again and merge into fresh state, never the pre-lock snapshot.
    db.rollback()
    db.expire_all()
    assignment = goal_assignment(db, chat_id)
    row = db.get(models.Delegation, principal.delegation_id)
    run = db.get(models.ChatRun, principal.run_id)
    if (assignment is None or assignment.delegation_id != principal.delegation_id
        or assignment.goal.id != goal_id or row is None
        or row.cancelled_at is not None or row.interrupted_at is not None
        or run is None or run.chat_id != chat_id or run.status != "running"):
      raise HTTPException(status_code=409, detail="This helper no longer owns the assignment.")
    try:
      stage_helper_task_edits(assignment, body.tasks)
      update_goal_record(db, run, assignment.goal, assignment.goal.revision, tasks=body.tasks)
    except (GoalPlanError, GoalPlanConflict) as exc:
      db.rollback()
      if isinstance(exc, GoalPlanError):
        raise _plan_refusal(exc) from exc
      raise HTTPException(status_code=409, detail=str(exc)) from exc
    touched = {edit["id"] for edit in body.tasks}
    return {"goal_id": goal_id, "revision": assignment.goal.revision,
            "tasks": [{"id": task["id"], "status": task["status"]}
                      for task in assignment.goal.plan_json["tasks"] if task["id"] in touched]}
