"""Durable, dependency-aware todo plans attached to logical Goal runs."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app import models


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_STATUSES = frozenset({
  "pending", "running", "completed", "blocked", "failed", "cancelled",
})
ACTIVE_TASK_STATUSES = frozenset({"running"})
MAX_TASKS = 64
MAX_DEPENDENCIES = 16
MAX_TITLE = 160
MAX_NOTE = 500
MAX_RESULT = 1000


class GoalPlanError(ValueError):
  """The requested plan would violate the visible execution contract."""


class GoalPlanConflict(RuntimeError):
  """Another writer advanced the plan revision first."""


def _clean_text(value: Any, *, field: str, maximum: int, required: bool) -> str:
  if value is None and not required:
    return ""
  if not isinstance(value, str):
    raise GoalPlanError(f"{field} must be text")
  cleaned = " ".join(value.split())
  if required and not cleaned:
    raise GoalPlanError(f"{field} must not be empty")
  if len(cleaned) > maximum:
    raise GoalPlanError(f"{field} must be at most {maximum} characters")
  return cleaned


def normalize_tasks(raw_tasks: Any) -> list[dict[str, Any]]:
  """Validate and normalize one complete plan snapshot.

  Dependencies form a DAG. A task may run or complete only after every
  dependency has completed, and repeated progress cannot claim completion
  before its total has been reached. Those are orchestration invariants, not
  UI hints, so every write path shares this function.
  """
  if not isinstance(raw_tasks, list) or not raw_tasks:
    raise GoalPlanError("a goal plan needs at least one task")
  if len(raw_tasks) > MAX_TASKS:
    raise GoalPlanError(f"a goal plan supports at most {MAX_TASKS} tasks")

  tasks: list[dict[str, Any]] = []
  ids: set[str] = set()
  for position, raw in enumerate(raw_tasks):
    if not isinstance(raw, dict):
      raise GoalPlanError(f"task {position + 1} must be an object")
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
      raise GoalPlanError(
        f"task {position + 1} id must start with a letter/number and use "
        "only letters, numbers, dots, underscores, or hyphens"
      )
    if task_id in ids:
      raise GoalPlanError(f"duplicate task id: {task_id}")
    ids.add(task_id)
    status = raw.get("status", "pending")
    if status not in TASK_STATUSES:
      raise GoalPlanError(f"invalid status for {task_id}: {status}")
    depends_on = raw.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(
      isinstance(value, str) for value in depends_on
    ):
      raise GoalPlanError(f"depends_on for {task_id} must be a list of ids")
    depends_on = list(dict.fromkeys(depends_on))
    if len(depends_on) > MAX_DEPENDENCIES:
      raise GoalPlanError(
        f"{task_id} supports at most {MAX_DEPENDENCIES} dependencies"
      )
    progress = raw.get("progress")
    normalized_progress = None
    if progress is not None:
      if not isinstance(progress, dict):
        raise GoalPlanError(f"progress for {task_id} must be an object")
      current = progress.get("current")
      total = progress.get("total")
      if (
        not isinstance(current, int) or isinstance(current, bool)
        or not isinstance(total, int) or isinstance(total, bool)
        or total < 1 or current < 0 or current > total
      ):
        raise GoalPlanError(
          f"progress for {task_id} needs integers with 0 <= current <= total"
        )
      normalized_progress = {"current": current, "total": total}
    task = {
      "id": task_id,
      "title": _clean_text(
        raw.get("title"), field=f"title for {task_id}",
        maximum=MAX_TITLE, required=True,
      ),
      "status": status,
      "depends_on": depends_on,
    }
    parent_id = raw.get("parent_id")
    if parent_id is not None:
      if not isinstance(parent_id, str) or not TASK_ID_RE.fullmatch(parent_id):
        raise GoalPlanError(f"parent_id for {task_id} must be a valid task id")
      task["parent_id"] = parent_id
    completion_condition = _clean_text(
      raw.get("completion_condition"),
      field=f"completion_condition for {task_id}",
      maximum=MAX_NOTE, required=False,
    )
    if completion_condition:
      task["completion_condition"] = completion_condition
    note = _clean_text(
      raw.get("note"), field=f"note for {task_id}",
      maximum=MAX_NOTE, required=False,
    )
    if note:
      task["note"] = note
    result = _clean_text(
      raw.get("result"), field=f"result for {task_id}",
      maximum=MAX_RESULT, required=False,
    )
    if result:
      task["result"] = result
    if normalized_progress is not None:
      task["progress"] = normalized_progress
    tasks.append(task)

  by_id = {task["id"]: task for task in tasks}
  for task in tasks:
    parent_id = task.get("parent_id")
    if parent_id == task["id"]:
      raise GoalPlanError(f"{task['id']} cannot be its own parent")
    if parent_id is not None and parent_id not in by_id:
      raise GoalPlanError(f"{task['id']} has missing parent {parent_id}")
    for dependency in task["depends_on"]:
      if dependency == task["id"]:
        raise GoalPlanError(f"{task['id']} cannot depend on itself")
      if dependency not in by_id:
        raise GoalPlanError(
          f"{task['id']} depends on missing task {dependency}"
        )

  visiting: set[str] = set()
  visited: set[str] = set()

  def visit(task_id: str) -> None:
    if task_id in visited:
      return
    if task_id in visiting:
      raise GoalPlanError("goal-plan dependencies must not contain a cycle")
    visiting.add(task_id)
    for dependency in by_id[task_id]["depends_on"]:
      visit(dependency)
    visiting.remove(task_id)
    visited.add(task_id)

  for task in tasks:
    visit(task["id"])

  # Parentage is a second, deliberately independent acyclic relation. A
  # dependency orders sibling work; parentage owns completion verification.
  for task in tasks:
    seen = {task["id"]}
    parent_id = task.get("parent_id")
    while parent_id is not None:
      if parent_id in seen:
        raise GoalPlanError("goal-plan parentage must not contain a cycle")
      seen.add(parent_id)
      parent_id = by_id[parent_id].get("parent_id")

  for task in tasks:
    incomplete = [
      dependency for dependency in task["depends_on"]
      if by_id[dependency]["status"] != "completed"
    ]
    if task["status"] in {"running", "completed"} and incomplete:
      raise GoalPlanError(
        f"{task['id']} cannot be {task['status']} until these dependencies "
        f"complete: {', '.join(incomplete)}"
      )
    unfinished_children = [
      child["id"] for child in tasks
      if child.get("parent_id") == task["id"]
      and child["status"] not in {"completed", "cancelled"}
    ]
    if task["status"] == "completed" and unfinished_children:
      raise GoalPlanError(
        f"{task['id']} cannot complete before its children settle: "
        f"{', '.join(unfinished_children)}"
      )
    progress = task.get("progress")
    if (
      task["status"] == "completed" and progress is not None
      and progress["current"] != progress["total"]
    ):
      raise GoalPlanError(
        f"{task['id']} cannot complete before its repeated progress is full"
      )
  return tasks


def active_goal_rows(
  db: Session, chat_id: str,
) -> tuple[models.ChatRun, models.ChatRun] | None:
  """Return (active physical row, logical root row) for this chat's Goal."""
  physical = (
    db.query(models.ChatRun)
    .filter(
      models.ChatRun.chat_id == chat_id,
      models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES),
      models.ChatRun.goal_objective.isnot(None),
    )
    .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
    .first()
  )
  if physical is None:
    return None
  if physical.goal_id:
    plan_owner = (
      db.query(models.ChatRun)
      .filter(
        models.ChatRun.chat_id == chat_id,
        models.ChatRun.goal_id == physical.goal_id,
        models.ChatRun.goal_plan_json.isnot(None),
      )
      .order_by(models.ChatRun.started_at.asc(), models.ChatRun.id.asc())
      .first()
    )
    if plan_owner is not None:
      return physical, plan_owner
  root_id = physical.root_run_id or physical.id
  root = db.query(models.ChatRun).filter(
    models.ChatRun.id == root_id,
    models.ChatRun.chat_id == chat_id,
  ).first()
  if root is None:
    raise RuntimeError("active Goal refers to a missing logical root run")
  return physical, root


def serialize_plan(
  physical: models.ChatRun, root: models.ChatRun,
) -> dict[str, Any] | None:
  raw = root.goal_plan_json
  if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
    return None
  tasks = deepcopy(raw["tasks"])
  by_id = {task["id"]: task for task in tasks}
  completed = sum(task.get("status") == "completed" for task in tasks)
  running = [task["id"] for task in tasks if task.get("status") == "running"]
  completion_blockers = [
    task["id"] for task in tasks
    if task.get("status") not in {"completed", "cancelled"}
  ]
  ready: list[str] = []
  for task in tasks:
    waiting_on = [
      dep for dep in task.get("depends_on", [])
      if by_id.get(dep, {}).get("status") != "completed"
    ]
    task["waiting_on"] = waiting_on
    task["ready"] = task.get("status") == "pending" and not waiting_on
    children = [
      child["id"] for child in tasks
      if child.get("parent_id") == task["id"]
    ]
    task["children"] = children
    task["ready_to_verify"] = bool(children) and all(
      by_id[child_id].get("status") in {"completed", "cancelled"}
      for child_id in children
    ) and task.get("status") not in {"completed", "cancelled"}
    if task["ready"]:
      ready.append(task["id"])
  return {
    "version": 1,
    "goal_id": physical.goal_id,
    "root_run_id": root.id,
    "objective": physical.goal_objective,
    "revision": int(root.goal_plan_revision or 0),
    "updated_at": raw.get("updated_at"),
    "tasks": tasks,
    "summary": {
      "completed": completed,
      "total": len(tasks),
      "running": running,
      "ready": ready,
      "can_complete": not completion_blockers,
      "completion_blockers": completion_blockers,
    },
  }


def replace_plan(
  db: Session,
  *,
  physical: models.ChatRun,
  root: models.ChatRun,
  expected_revision: int,
  tasks: Any,
) -> dict[str, Any]:
  normalized = normalize_tasks(tasks)
  document = {
    "version": 1,
    "updated_at": datetime.now(UTC).isoformat(),
    "tasks": normalized,
  }
  result = db.execute(
    update(models.ChatRun)
    .where(
      models.ChatRun.id == root.id,
      models.ChatRun.goal_plan_revision == expected_revision,
    )
    .values(
      goal_plan_json=document,
      goal_plan_revision=expected_revision + 1,
    )
  )
  if result.rowcount != 1:
    db.rollback()
    raise GoalPlanConflict("goal plan changed; fetch it and retry")
  db.commit()
  db.refresh(root)
  plan = serialize_plan(physical, root)
  if plan is None:  # pragma: no cover - the write above guarantees a document
    raise RuntimeError("goal plan disappeared after commit")
  return plan


def update_task(
  db: Session,
  *,
  physical: models.ChatRun,
  root: models.ChatRun,
  expected_revision: int,
  task_id: str,
  changes: dict[str, Any],
) -> dict[str, Any]:
  existing = serialize_plan(physical, root)
  if existing is None:
    raise GoalPlanError("this Goal does not have a plan yet")
  tasks = existing["tasks"]
  target = next((task for task in tasks if task["id"] == task_id), None)
  if target is None:
    raise GoalPlanError(f"unknown task id: {task_id}")
  target.pop("ready", None)
  target.pop("waiting_on", None)
  for key, value in changes.items():
    if value is not None:
      target[key] = value
  return replace_plan(
    db,
    physical=physical,
    root=root,
    expected_revision=expected_revision,
    tasks=tasks,
  )
