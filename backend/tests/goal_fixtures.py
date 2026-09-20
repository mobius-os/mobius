"""Explicit paired Goal/attempt fixtures for tests of the new owning boundary.

Legacy constructor fields here are fixture input only, not a runtime fallback.
Tests of the actual upgrade use schema_migrations instead.
"""
from app import models


def goal_run(db, **kwargs):
  plan = kwargs.pop("goal_plan_json", None)
  revision = kwargs.pop("goal_plan_revision", 0)
  if kwargs.get("goal_objective") and not kwargs.get("goal_id"):
    kwargs["goal_id"] = kwargs.get("root_run_id") or kwargs["id"]
  goal_id = kwargs.get("goal_id")
  if goal_id:
    goal = next((r for r in db.new if isinstance(r, models.ChatGoal) and r.id == goal_id), None)
    goal = goal or db.get(models.ChatGoal, goal_id)
    if goal is None:
      tasks = (plan or {}).get("tasks")
      completed = bool(tasks) and all(t.get("status") in {"completed", "cancelled"} for t in tasks)
      status = "completed" if completed and kwargs.get("status") == "completed" else "open"
      if kwargs.get("status") == "stopped":
        status = "stopped"
      goal = models.ChatGoal(id=goal_id, chat_id=kwargs["chat_id"],
                            objective=kwargs.get("goal_objective") or "Test Goal",
                            status=status, plan_json=plan, revision=revision)
      if kwargs.get("started_at"):
        goal.created_at=kwargs["started_at"]
      db.add(goal)
    else:
      if kwargs.get("status") == "stopped":
        goal.status = "stopped"
      if plan is not None:
        goal.plan_json=plan
        goal.revision=revision
  return models.ChatRun(**kwargs)


def persist_goal_fixture(db, run, *, status="open"):
  """Explicitly seed the obligation for tests that assemble an attempt in steps."""
  goal = db.get(models.ChatGoal, run.goal_id)
  if goal is None:
    goal = models.ChatGoal(id=run.goal_id, chat_id=run.chat_id,
                          objective=run.goal_objective)
    db.add(goal)
  goal.status = status
  if run.goal_plan_json is not None:
    goal.plan_json = run.goal_plan_json
    goal.revision = run.goal_plan_revision or 0
  # New attempts do not own plan snapshots.
  run.goal_plan_json = None
  run.goal_plan_revision = 0
  return goal
