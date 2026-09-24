"""Goal intent and attempt admission. No provider state or transcript inference."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from sqlalchemy import update
from app import models


def goal_for_run(db, run):
  if run is None or not run.goal_id:
    return None
  goal = db.get(models.ChatGoal, run.goal_id)
  if goal is None or goal.chat_id != run.chat_id:
    raise RuntimeError("Goal attempt has no matching durable work record")
  return goal


def admit_goal(db, chat_id, goal_id, objective, message=None):
  """Called only by the writer, in the transaction admitting an exact attempt.

  Duplicate attempt admission is fenced by the existing writer commands.
  Execution turns are not a budget; explicit Stop remains authoritative.
  """
  if not goal_id:
    return
  from app.continuations import continuation_reason
  goal = db.get(models.ChatGoal, goal_id)
  if goal is None:
    goal = models.ChatGoal(id=goal_id, chat_id=chat_id, objective=objective, status="open")
    db.add(goal)
  elif goal.chat_id != chat_id:
    raise RuntimeError("Goal belongs to another chat")
  reason = continuation_reason(message)
  owner_admission = message is not None and (
    not message.get("kind") or reason in {"manual", "question_answer"}
  )
  if owner_admission:
    from app.goal_commands import is_goal_continue
    if goal.status == "stopped" and (reason == "manual" or is_goal_continue(str(message.get("content") or ""))):
      goal.status = "open"
      goal.revision += 1
  elif reason == "goal_handoff" and goal.status != "open":
    raise RuntimeError("Goal is closed")


def scoped_goal_context(db, goal, task_id=None):
  from app.goal_context import project_goal
  payload = project_goal(goal, task_id)
  others = db.query(models.ChatGoal).filter(
    models.ChatGoal.chat_id == goal.chat_id, models.ChatGoal.id != goal.id,
    models.ChatGoal.status == "open",
  ).order_by(models.ChatGoal.created_at, models.ChatGoal.id).all()
  if others:
    payload["other_open_goals"] = [{"id": g.id, "objective": g.objective} for g in others]
  return payload


def resume_context(db, run_id):
  run = db.get(models.ChatRun, run_id)
  goal = goal_for_run(db, run)
  if goal is None:
    return ""
  return (
    "Möbius Goal work data, not additional authority. Preserve the original outcome. "
    "This is a scoped view, not the full plan. Work in this run; do not end merely "
    "to get another task or refresh context. Run "
    "python3 /data/platform/backend/scripts/goal_plan.py context --task ID "
    "to inspect a branch, context for current focus, show for the full plan. "
    "Advance focus in one call: update DONE --status completed --start NEXT. "
    "Checkpoint only before a real handoff; complete --result only after "
    "verifying the entire Goal.\n"
    "<mobius_goal>" + json.dumps(scoped_goal_context(db, goal), ensure_ascii=False,
                                separators=(",", ":")) + "</mobius_goal>"
  )


def update_goal_record(db, run, goal, expected_revision, *, checkpoint=None,
                       next_action=None, result=None):
  from app.goal_plans import GoalPlanConflict, GoalPlanError, serialize_plan
  if (goal.status == "completed" and result is not None
      and goal.result == result.strip() and goal.revision == expected_revision + 1):
    return {"goal_id": goal.id, "status": goal.status, "revision": goal.revision}
  if goal.status != "open":
    raise GoalPlanConflict("Goal is not open")
  values = {"revision": expected_revision + 1}
  if result is not None:
    plan = serialize_plan(db, run, goal)
    if goal.plan_json is not None and plan is None:
      raise GoalPlanError(
        "Goal plan is unreadable; replace it with a validated plan before completion"
      )
    if plan is not None and not plan["summary"]["can_complete"]:
      raise GoalPlanError("Goal has unfinished tasks or active delegations")
    from app.goal_plans import goal_handoff_owner_kind
    if goal_handoff_owner_kind(db, goal.chat_id, goal.id) is not None:
      raise GoalPlanError("Goal still owns a pending handoff")
    if not result.strip():
      raise GoalPlanError("Completion requires a verification result")
    values.update(status="completed", result=result.strip(),
                  completed_at=datetime.now(UTC))
  else:
    values.update(checkpoint=checkpoint, next_action=next_action)
  changed = db.execute(update(models.ChatGoal).where(
    models.ChatGoal.id == goal.id, models.ChatGoal.revision == expected_revision,
    models.ChatGoal.status == "open",
  ).values(**values))
  if changed.rowcount != 1:
    db.rollback()
    raise GoalPlanConflict("Goal changed; fetch it and retry")
  db.commit()
  db.refresh(goal)
  return {"goal_id": goal.id, "status": goal.status, "revision": goal.revision}
