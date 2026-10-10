"""Durable, dependency-aware todo plans attached to logical Goal runs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import re
from typing import Any

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session, defer, load_only

from app import models
from app.chat_message_identity import assistant_message_run_id


_LOG = logging.getLogger(__name__)


# Task IDs are stable 64-character protocol keys, not prose budgets.
UPDATE_GOAL_TOOLS = frozenset({
  "mobius_control:update_goal", "mcp__mobius_control__update_goal",
})
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_STATUSES = frozenset({
  "pending", "running", "completed", "blocked", "failed", "cancelled",
})
ACTIVE_TASK_STATUSES = frozenset({"running"})
SETTLED_TASK_STATUSES = frozenset({"completed", "cancelled"})
MAX_TASKS = 64
MAX_DEPENDENCIES = 16
MAX_TITLE = 160
MAX_NOTE = 1000
MAX_RESULT = 1000


class GoalPlanError(ValueError):
  """The requested plan would violate the visible execution contract.

  ``code`` and ``facts`` let a client name its own remedy without matching
  the prose; the message stays client-neutral.
  """

  def __init__(self, message: str, *, code: str = "invalid_plan", **facts: Any):
    super().__init__(message)
    self.code = code
    self.facts = facts


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

  Explicit dependencies and implicit child-before-parent completion edges form
  one DAG. A task may run or complete only after every dependency has settled,
  and repeated progress cannot claim completion before its total has
  been reached. Those are orchestration invariants, not UI hints, so every
  write path shares this function.
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
    if not isinstance(status, str) or status not in TASK_STATUSES:
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

  children_by_parent: dict[str, list[str]] = {}
  for task in tasks:
    parent_id = task.get("parent_id")
    if parent_id is not None:
      children_by_parent.setdefault(parent_id, []).append(task["id"])

  visiting: set[str] = set()
  visited: set[str] = set()
  for task in tasks:
    start = task["id"]
    if start in visited:
      continue
    # Explicit frames keep dependency validation independent of recursion
    # depth within the bounded plan.
    stack = [(start, False)]
    while stack:
      task_id, leaving = stack.pop()
      if leaving:
        visiting.remove(task_id)
        visited.add(task_id)
        continue
      if task_id in visited:
        continue
      if task_id in visiting:
        raise GoalPlanError(
          "goal-plan dependencies and parentage must not form a completion cycle"
        )
      visiting.add(task_id)
      stack.append((task_id, True))
      stack.extend((dependency, False) for dependency in reversed(
        by_id[task_id]["depends_on"] + children_by_parent.get(task_id, [])))

  def effective_dependencies(task_id: str) -> list[str]:
    """Return direct dependencies plus those inherited from parent groups."""
    dependencies: list[str] = []
    current = by_id[task_id]
    while True:
      dependencies.extend(current["depends_on"])
      parent_id = current.get("parent_id")
      if parent_id is None:
        break
      current = by_id[parent_id]
    return list(dict.fromkeys(dependencies))

  for task in tasks:
    incomplete = [
      dependency for dependency in effective_dependencies(task["id"])
      if by_id[dependency]["status"] not in SETTLED_TASK_STATUSES
    ]
    if task["status"] in {"running", "completed"} and incomplete:
      raise GoalPlanError(
        f"{task['id']} cannot be {task['status']} until these dependencies "
        f"complete: {', '.join(incomplete)}"
      )
    unfinished_children = [
      child["id"] for child in tasks
      if child.get("parent_id") == task["id"]
      and child["status"] not in SETTLED_TASK_STATUSES
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
        f"{task['id']} cannot complete at {progress['current']}/"
        f"{progress['total']} progress",
        code="progress_incomplete", task_id=task["id"],
        current=progress["current"], total=progress["total"],
      )
  return tasks


def _goal_rows_for_physical(
  db: Session, physical: models.ChatRun,
) -> tuple[models.ChatRun, models.ChatGoal]:
  """Resolve attempt to its sole durable plan/outcome owner."""
  from app.goals import goal_for_run
  goal = goal_for_run(db, physical)
  if goal is None:
    raise RuntimeError("Attempt does not own a Goal")
  return physical, goal


def active_goal_rows(
  db: Session, chat_id: str,
) -> tuple[models.ChatRun, models.ChatGoal] | None:
  """Return the active attempt and its durable Goal for mutations."""
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
  dismissed_goal_id = db.query(models.Chat.dismissed_goal_id).filter(
    models.Chat.id == chat_id,
  ).scalar()
  if (
    physical is not None
    and physical.goal_id is not None
    and physical.goal_id == dismissed_goal_id
  ):
    return None
  return _goal_rows_for_physical(db, physical) if physical is not None else None


def helper_plan_task(
  db: Session, chat_id: str, requested: str | None,
) -> str | None:
  """Choose the plan task a new helper of ``chat_id`` works on.

  An explicit task must exist in the chat's active Goal plan. Otherwise the
  helper joins the plan's current focus: the one running leaf task. With no
  Goal, no plan, or several running leaves the helper stays unfiled rather
  than guessing among them.

  A delegated chat that owns no Goal works on the Goal its delegation chain
  is assigned to (goal_assignment): an explicit task is checked against that
  plan and must lie within the helper's own assigned branch (the task itself
  or one beneath it), and an omitted one stays unfiled so the new helper
  inherits its parent's branch instead of the whole plan's focus.
  """
  rows = active_goal_rows(db, chat_id)
  goal = rows[1] if rows else None
  assignment = goal_assignment(db, chat_id) if goal is None else None
  if assignment is not None:
    goal = assignment.goal
  tasks = list(((goal.plan_json or {}) if goal is not None else {}).get("tasks") or [])
  if requested is not None:
    ids = [str(task.get("id")) for task in tasks]
    if assignment is not None and assignment.plan_task is not None:
      # A branch helper splits only its own branch, as its brief is scoped.
      parent_of = {str(task.get("id")): task.get("parent_id") for task in tasks}
      ids = [
        task_id for task_id in ids
        if _within_branch(task_id, assignment.plan_task, parent_of)
      ]
    if requested not in ids:
      known = ", ".join(ids) if ids else "none (no active Goal plan)"
      raise GoalPlanError(
        f"plan_task {requested!r} is not a task of this chat's Goal plan; "
        f"plan tasks: {known}"
      )
    return requested
  if assignment is not None:
    return None
  running = {
    str(task.get("id")): task for task in tasks if task.get("status") == "running"
  }
  parents = {str(task.get("parent_id")) for task in running.values()}
  leaves = [task_id for task_id in running if task_id not in parents]
  return leaves[0] if len(leaves) == 1 else None


def _within_branch(task_id: str, branch: str, parent_of: dict) -> bool:
  """Whether ``task_id`` is ``branch`` or nested beneath it."""
  seen: set[str] = set()
  current: str | None = task_id
  while current is not None and current not in seen:
    if current == branch:
      return True
    seen.add(current)
    parent = parent_of.get(current)
    current = str(parent) if parent is not None else None
  return False


@dataclass(frozen=True)
class GoalAssignment:
  """The exact Goal and plan task one delegated chat works on.

  ``plan_task`` is None when no delegation in the chain was filed under a
  task, or when the filed task is no longer in the plan
  (``plan_task_missing``); the helper then reads the Goal overview.
  """

  goal: models.ChatGoal
  delegation_id: str
  helper: str
  plan_task: str | None
  plan_task_missing: bool
  depth: int


def goal_assignment(db: Session, chat_id: str) -> GoalAssignment | None:
  """Resolve immutable Goal ownership and the nearest filed task in a chain.

  Historical rows whose parent root is exactly a Goal id remain readable.
  Other NULL ownership is ambiguous after A→B and is never guessed from a
  mutable run link. App/source-owned work is not Goal work.
  """
  row = db.query(models.Delegation).filter(
    models.Delegation.child_chat_id == chat_id,
  ).first()
  if row is None:
    return None
  chain = [row]
  while True:
    parent = db.query(models.Delegation).filter(
      models.Delegation.child_chat_id == chain[-1].parent_chat_id,
    ).first()
    if parent is None:
      break
    if any(parent.id == seen.id for seen in chain):
      return None
    chain.append(parent)
  if any(link.app_id is not None or link.source_work_id is not None for link in chain):
    return None
  anchored = {link.goal_id for link in chain if link.goal_id is not None}
  if len(anchored) > 1:
    return None
  top = chain[-1]
  root_id = top.parent_root_run_id
  goal = db.get(models.ChatGoal, anchored.pop()) if anchored else db.get(models.ChatGoal, root_id)
  if goal is None or goal.chat_id != top.parent_chat_id:
    return None
  filed = next((link.goal_task_id for link in chain if link.goal_task_id), None)
  plan_ids = {
    task.get("id") for task in ((goal.plan_json or {}).get("tasks") or [])
    if isinstance(task, dict)
  } if isinstance(goal.plan_json, dict) else set()
  missing = filed is not None and filed not in plan_ids
  return GoalAssignment(
    goal=goal, delegation_id=row.id, helper=row.task_key,
    plan_task=None if missing else filed, plan_task_missing=missing,
    depth=len(chain),
  )


def _presented_goal_attempts(db: Session, chat_ids):
  """Shared exact retained-Goal selection, independent of execution liveness.

  Rank before applying visibility: clearing the latest identity (or a latest
  attempt without an objective) must not uncover an older Goal. Ordinary
  non-Goal turns do not displace the retained Goal.
  """
  ranked = db.query(
    models.ChatRun.id.label("run_id"),
    func.row_number().over(
      partition_by=models.ChatRun.chat_id,
      order_by=(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()),
    ).label("position"),
  ).filter(
    models.ChatRun.chat_id.in_(list(chat_ids)),
    or_(models.ChatRun.goal_id.isnot(None), models.ChatRun.goal_objective.isnot(None)),
  ).subquery()
  return db.query(models.ChatRun).join(
    ranked, ranked.c.run_id == models.ChatRun.id,
  ).join(models.Chat, models.Chat.id == models.ChatRun.chat_id).filter(
    ranked.c.position == 1,
    models.ChatRun.goal_objective.isnot(None),
    or_(models.ChatRun.goal_id.is_(None), models.Chat.dismissed_goal_id.is_(None),
        models.ChatRun.goal_id != models.Chat.dismissed_goal_id),
  )


def presented_goal_rows(
  db: Session, chat_id: str,
) -> tuple[models.ChatRun, models.ChatGoal] | None:
  """Latest Goal remains visible until its exact identity is explicitly cleared."""
  physical = _presented_goal_attempts(db, [chat_id]).first()
  return _goal_rows_for_physical(db, physical) if physical is not None else None


def presented_deferred_goals(db: Session, chat_ids) -> dict[str, dict]:
  """Tiny batch hold projection: no plans, transcripts, or per-chat reads.

  Uses the same retained-attempt selection as the full Goal presentation; a
  newer terminal or cleared Goal must never expose an older deferred Goal.
  """
  from app.goals import goal_hold
  ids = list(chat_ids)
  if not ids:
    return {}
  rows = _presented_goal_attempts(db, ids).join(
    models.ChatGoal,
    (models.ChatGoal.id == models.ChatRun.goal_id)
    & (models.ChatGoal.chat_id == models.ChatRun.chat_id),
  ).filter(models.ChatGoal.status == "stopped").with_entities(
    models.ChatRun.chat_id, models.ChatGoal.id, models.ChatGoal.hold_json,
  ).all()
  result = {}
  for row in rows:
    hold = goal_hold(row)
    if hold and hold["cause"] == "deferred":
      result[row.chat_id] = {"id": row.id, "pause_reason": "deferred",
                             "hold_reason": hold["reason"]}
  return result


def _delegation_tree(
  db: Session, physical: models.ChatRun, root: models.ChatGoal,
  *, all_attempts: bool = False,
) -> list[dict[str, Any]]:
  """Project durable immediate-child ownership without copying transcripts."""
  from app.delegations import delegation_statuses

  # Only the historical overloaded Goal-id root is immutable evidence for
  # NULL ownership. A physical root may later be rebound to another Goal.
  legacy_ids = {root.id}
  root_rows = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == physical.chat_id,
    models.Delegation.app_id.is_(None),
    models.Delegation.source_work_id.is_(None),
    or_(models.Delegation.goal_id == root.id,
        (models.Delegation.goal_id.is_(None))
        & models.Delegation.parent_root_run_id.in_(legacy_ids)),
  ).order_by(models.Delegation.created_at.asc()).all()
  # A resumed Goal may delegate the same plan task again from a newer physical
  # run. Only the latest attempt is current execution; older attempts remain in
  # Workflows history rather than appearing twice (or disagreeing with the
  # compact rail) in the Goal tree.
  roots_by_task = {row.task_key: row for row in root_rows}
  roots = root_rows if all_attempts else list(roots_by_task.values())
  children_by_parent: dict[str, list[models.Delegation]] = {}
  frontier = [row.child_chat_id for row in roots]
  seen_rows = {row.id for row in roots}
  while frontier:
    child_rows = db.query(models.Delegation).filter(
      models.Delegation.parent_chat_id.in_(frontier),
      models.Delegation.app_id.is_(None),
      models.Delegation.source_work_id.is_(None),
      or_(models.Delegation.goal_id == root.id,
          models.Delegation.goal_id.is_(None)),
    ).order_by(models.Delegation.created_at.asc()).all()
    frontier = []
    latest_by_owner_and_task = {
      (child.parent_chat_id, child.task_key): child for child in child_rows
    }
    for child in (child_rows if all_attempts else latest_by_owner_and_task.values()):
      if child.id in seen_rows:
        continue
      seen_rows.add(child.id)
      children_by_parent.setdefault(child.parent_chat_id, []).append(child)
      frontier.append(child.child_chat_id)

  statuses = delegation_statuses(db, roots + [
    child for children in children_by_parent.values() for child in children
  ])
  from app.delegations import open_questions
  waiting = [row for row in roots + [
    child for children in children_by_parent.values() for child in children
  ] if statuses[row.id] == "needs_input"]
  waiting_runs = []
  if waiting:
    ranked = db.query(
      models.ChatRun.id.label("run_id"),
      func.row_number().over(
        partition_by=models.ChatRun.chat_id,
        order_by=(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()),
      ).label("position"),
    ).filter(models.ChatRun.chat_id.in_([row.child_chat_id for row in waiting])).subquery()
    latest = {run.chat_id: run for run in db.query(models.ChatRun).join(
      ranked, ranked.c.run_id == models.ChatRun.id,
    ).filter(ranked.c.position == 1).all()}
    waiting_runs = [(row, latest.get(row.child_chat_id)) for row in waiting]
  questions = open_questions(db, waiting_runs)
  raw_plan = root.plan_json if isinstance(root.plan_json, dict) else {}
  titles = {task.get("id"): task.get("title") for task in
    (raw_plan.get("tasks") or []) if isinstance(task, dict)}

  def project(row: models.Delegation, seen: set[str]) -> dict[str, Any]:
    if row.id in seen:
      return {"id": row.id, "task_key": row.task_key, "plan_task": row.goal_task_id,
              "title": titles.get(row.goal_task_id), "provider": row.provider,
              "status": "failed", "question": None, "children": []}
    children = children_by_parent.get(row.child_chat_id, [])
    return {
      "id": row.id,
      "task_key": row.task_key,
      "plan_task": row.goal_task_id,
      "title": titles.get(row.goal_task_id),
      "provider": row.provider,
      "status": statuses[row.id],
      "question": ({"id": questions[row.id].id, "text": questions[row.id].question,
                    "options": list(questions[row.id].options_json or [])}
                   if row.id in questions else None),
      "children": [project(child, seen | {row.id}) for child in children],
    }

  return [project(row, set()) for row in roots]


def publish_plan_for_delegation(
  db: Session, row: models.Delegation,
) -> None:
  """Invalidate the Goal owner and this helper's direct parent after a commit."""
  if row.app_id is not None or row.source_work_id is not None:
    return
  publish_goal_changed(row.parent_chat_id)
  if row.goal_id is not None:
    owner = db.query(models.ChatGoal.chat_id).filter(
      models.ChatGoal.id == row.goal_id,
    ).scalar()
    if owner and owner != row.parent_chat_id:
      publish_goal_changed(owner)
    return
  assignment = goal_assignment(db, row.child_chat_id)
  if assignment is not None and assignment.goal.chat_id != row.parent_chat_id:
    publish_goal_changed(assignment.goal.chat_id)


def publish_goal_changed(chat_id: str) -> None:
  """Post-commit runtime invalidation; the system channel survives idle turns."""
  try:
    from app.broadcast import get_system_broadcast
    get_system_broadcast().publish({
      "type": "chat_wait_changed", "chatId": chat_id, "chat_id": chat_id,
      "source": "goal",
    })
  except Exception:
    _LOG.exception("Goal invalidation failed for chat %s", chat_id)


def _active_helper_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Delegation-tree nodes, at any depth, whose execution has not settled."""
  from app.delegations import TERMINAL_DELEGATION_STATUSES

  active: list[dict[str, Any]] = []
  for node in nodes:
    if node.get("status") not in TERMINAL_DELEGATION_STATUSES:
      active.append(node)
    active.extend(_active_helper_nodes(node.get("children") or []))
  return active


def _helper_key(node: dict[str, Any]) -> str:
  return str(node.get("task_key") or node["id"])


def active_goal_helpers(
  db: Session, physical: models.ChatRun, root: models.ChatGoal,
) -> list[str]:
  """Task keys of this Goal's helpers still working, with or without a plan."""
  return [
    _helper_key(node)
    # Presentation folds superseded attempts, but settlement cannot abandon
    # an older child merely because a newer attempt used the same task key.
    for node in _active_helper_nodes(_delegation_tree(db, physical, root, all_attempts=True))
  ]


def serialize_plan(
  db: Session, physical: models.ChatRun, root: models.ChatGoal,
) -> dict[str, Any] | None:
  raw = root.plan_json
  if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
    return None
  try:
    tasks = normalize_tasks(deepcopy(raw["tasks"]))
  except GoalPlanError:
    return None
  by_id = {task["id"]: task for task in tasks}
  children_by_parent: dict[str, list[str]] = {}
  for task in tasks:
    parent_id = task.get("parent_id")
    if parent_id is not None:
      children_by_parent.setdefault(parent_id, []).append(task["id"])

  def effective_waiting(task: dict[str, Any]) -> list[str]:
    dependencies: list[str] = []
    current = task
    while True:
      dependencies.extend(current.get("depends_on", []))
      parent_id = current.get("parent_id")
      if parent_id is None:
        break
      current = by_id[parent_id]
    return [
      dependency for dependency in dict.fromkeys(dependencies)
      if by_id.get(dependency, {}).get("status") not in SETTLED_TASK_STATUSES
    ]
  delegations = _delegation_tree(db, physical, root)
  active_helpers = _active_helper_nodes(delegations)
  active_execution_keys = [_helper_key(node) for node in active_helpers]
  # Plan tasks with a helper still working: such a task is not counted complete
  # even if it was marked so, because its execution has not settled.
  tasks_with_active_helpers = {
    str(node["plan_task"]) for node in active_helpers if node.get("plan_task")
  }
  completed = sum(
    task.get("status") == "completed" and task["id"] not in tasks_with_active_helpers
    for task in tasks
  )
  running = [task["id"] for task in tasks if task.get("status") == "running"]
  task_blockers = [
    task["id"] for task in tasks
    if task.get("status") not in {"completed", "cancelled"}
  ]
  completion_blockers = list(dict.fromkeys(
    task_blockers + active_execution_keys
  ))
  ready: list[str] = []
  for task in tasks:
    waiting_on = effective_waiting(task)
    task["waiting_on"] = waiting_on
    children = children_by_parent.get(task["id"], [])
    task["children"] = children
    task["ready"] = (
      task.get("status") == "pending" and not waiting_on and not children
    )
    task["ready_to_verify"] = (
      task.get("status") in {"pending", "running"}
      and not waiting_on and bool(children) and all(
        by_id[child_id].get("status") in SETTLED_TASK_STATUSES
        for child_id in children
      )
    )
    if task["ready"]:
      ready.append(task["id"])
  return {
    "version": 1,
    "goal_id": root.id,
    "root_run_id": physical.root_run_id or physical.id,
    "objective": root.objective,
    "revision": int(root.revision or 0),
    "updated_at": raw.get("updated_at"),
    "tasks": tasks,
    "delegations": delegations,
    "summary": {
      "completed": completed,
      "total": len(tasks),
      "running": running,
      "ready": ready,
      "can_complete": not completion_blockers,
      "completion_blockers": completion_blockers,
    },
  }


def serialize_goal(
  db: Session,
  physical: models.ChatRun,
  root: models.ChatGoal,
) -> dict[str, Any]:
  """Project durable Goal presentation independently of turn liveness."""
  plan = serialize_plan(db, physical, root)
  return _goal_presentation(db, physical, root, plan)


def _goal_presentation(
  db: Session,
  physical: models.ChatRun,
  root: models.ChatGoal,
  plan: dict[str, Any] | None,
) -> dict[str, Any]:
  """Project only the Goal's own lifecycle: active, paused, or terminal.

  Chat-wide work cannot masquerade as this Goal's executor or owner question.
  Exact attempt, Wait and helper ownership supplies its read-only handoff.
  """
  if root.status in {"completed", "cannot_complete", "cancelled"}:
    status = root.status
  elif root.status in {"stopped", "dismissed"}:
    status = "paused"
  elif physical.status == "running":
    status = "active"
  else:
    status = "paused"
  presentation = {
    "id": root.id,
    "revision": int(root.revision or 0),
    "objective": root.objective,
    "status": status,
    "resumable": status == "paused",
  }
  if status in {"completed", "cannot_complete", "cancelled"}:
    presentation["result"] = root.result
  if root.status == "stopped":
    from app.goals import goal_hold
    hold = goal_hold(root)
    if hold and hold["cause"] == "deferred":
      presentation.update(pause_reason="deferred", hold_reason=hold["reason"])
    else:
      presentation["pause_reason"] = hold["actor"] if hold else "unknown"
  presentation["handoff"] = _goal_handoff(db, physical, root)
  presentation["plan"] = plan
  return presentation


def _goal_handoff(db: Session, physical: models.ChatRun, goal: models.ChatGoal) -> dict:
  from app.chat_handoffs import project_handoff
  none = {"kind": "none", "reason": None}
  if goal.status == "stopped":
    from app.goals import goal_hold
    hold = goal_hold(goal)
    if hold and hold["cause"] == "deferred":
      return none
    actor = hold["actor"] if hold else "unknown"
    if actor == "owner":
      return {"kind": "owner_hold", "reason": "owner"}
    return {"kind": "recovery", "reason": "agent_pause" if actor == "agent" else "unknown_stop"}
  if goal.status != "open":
    return none
  latest = db.query(models.ChatRun.id, models.ChatRun.goal_id, models.ChatRun.status).filter(
    models.ChatRun.chat_id == goal.chat_id,
  ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
  owns_chat_attempt = latest is not None and latest.goal_id == goal.id
  from app import questions
  pending = questions.get(goal.chat_id)
  pending_id = db.query(models.Chat.pending_question_id).filter(
    models.Chat.id == goal.chat_id,
  ).scalar()
  # Saved-card admission prevents a successor until this marker is answered or
  # cancelled. Its current physical attempt is therefore the exact owner;
  # native in-turn questions additionally carry their explicit run identity.
  owner_input = bool(owns_chat_attempt and (
    pending_id or (pending is not None and pending.run_token == latest.id)
  ))
  if owner_input:
    return {"kind": "owner_input", "reason": "saved_card"}
  if owns_chat_attempt and latest.status == "running":
    return {"kind": "working", "reason": None}
  from app.chat_waits import _goal_waits, _FIRED_UNDELIVERED, serialize_wait
  waits = [serialize_wait(row, db=db) for row in _goal_waits(db, goal.chat_id, goal.id).filter(
    (models.ChatWait.status == "armed") | _FIRED_UNDELIVERED,
  ).all()]
  if any(wait.get("resume_blocker") == "owner_input" for wait in waits):
    return {"kind": "blocked", "reason": "other_work"}
  from app.delegations import _self_resuming_helper_rows
  roots = {goal.id}
  helper_count = sum(row.goal_id == goal.id or (
    row.goal_id is None and row.parent_root_run_id in roots)
    for row, _status in _self_resuming_helper_rows(db, {goal.chat_id}))
  from app.chat import continuation_handoff_for_chat
  park = continuation_handoff_for_chat(db, goal.chat_id) if owns_chat_attempt else none
  handoff = project_handoff(owner_input=False, running=False, waits=waits,
    helper_count=helper_count, park=park)
  if handoff["kind"] == "none" and owns_chat_attempt and latest.status in {"failed", "interrupted"}:
    return {"kind": "recovery", "reason": "execution"}
  return handoff


def presented_goal(db: Session, chat_id: str) -> dict[str, Any] | None:
  """Serialize the Goal presentation retained by this chat, if any."""
  rows = presented_goal_rows(db, chat_id)
  return serialize_goal(db, *rows) if rows is not None else None


def paused_goal_run(db: Session, chat_id: str) -> models.ChatRun | None:
  """The physical run of this chat's paused Goal: unfinished work that an
  owner "continue" or a peer wake resumes under."""
  rows = presented_goal_rows(db, chat_id)
  if rows is None or serialize_goal(db, *rows)["status"] != "paused":
    return None
  return rows[0]


def _goal_completion_anchor(messages, run_ids, result, status="completed",
                            *, max_timestamp_ms=None, require_exact_result=False):
  """Locate the successful terminal receipt, never a refused attempt.

  Modern tool rows carry exact execution identity. Historical provider input
  summaries clip arguments, so their successful receipt supplies the verdict.
  Goal state still owns completion; this only locates its transcript position.
  """
  from app.goals import cannot_complete_result
  if status != "completed" and (not isinstance(result, str) or not result):
    return None
  if require_exact_result and not isinstance(result, str):
    return None
  for index in range(len(messages) - 1, -1, -1):
    message = messages[index]
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    if (max_timestamp_ms is not None
        and isinstance(message.get("ts"), (int, float))
        and message["ts"] > max_timestamp_ms + 1000):
      continue
    if assistant_message_run_id(message.get("id")) not in run_ids:
      continue
    for block in reversed(message.get("blocks") or []):
      if (not isinstance(block, dict) or block.get("type") != "tool"
          or block.get("tool") not in UPDATE_GOAL_TOOLS or block.get("status") != "done"
          or block.get("output_exit_code") != 0 or not block.get("tool_use_id")):
        continue
      raw = block.get("input")
      if not isinstance(raw, str):
        continue
      try:
        args = json.loads(raw)
      except ValueError:
        # Summarized arguments cannot prove the exact result; the successful
        # server receipt can. A plain read must never become the anchor.
        field = {"completed": "complete", "cannot_complete": "cannot_complete",
                 "cancelled": "cancel"}.get(status)
        if (not require_exact_result and field and re.search(rf"(?:^|, ){field}=", raw)
            and f"Goal {status}, revision " in str(block.get("output") or "")):
          return index, block["tool_use_id"]
      else:
        terminal_value = (args.get("complete") if status == "completed" else
                          args.get("cancel") if status == "cancelled" else
                          args.get("cannot_complete")) if isinstance(args, dict) else None
        if ((not require_exact_result and status == "completed"
             and terminal_value is True and result is None)
            or (isinstance(terminal_value, str) and terminal_value == result)
            or (status == "cannot_complete" and isinstance(terminal_value, dict)
                and all(isinstance(terminal_value.get(key), str) for key in
                        ("reason", "efforts", "unmet_outcome"))
                and result == cannot_complete_result(terminal_value))):
          return index, block["tool_use_id"]
  return None


def terminal_goal_summaries_by_message_index(
  db: Session,
  chat_id: str,
  messages: list[dict[str, Any]],
  *,
  message_start: int = 0,
  message_end: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
  """Project terminal Goals at their outcome receipt, or legacy final answer.

  Goal history already belongs to ``ChatGoal`` and its attempt rows; copying it into
  ``Chat.messages`` would create a second persistence mechanism and make plan
  revisions race transcript settlement. This read-side projection keeps one
  durable owner while giving paginated chat history a stable place to render
  each terminal Goal. A summary travels with its successful completion call,
  or the final answer for legacy history without that receipt. Ordinary message
  pagination naturally paginates Goal cards too. Find that row before
  filtering to the requested half-open window; searching only the page would
  move a resumed Goal's card onto an earlier answer. Plans and handoffs for
  off-page Goals are never hydrated.
  """
  goals = db.query(models.ChatGoal).options(defer(models.ChatGoal.plan_json)).filter(
    models.ChatGoal.chat_id == chat_id,
    models.ChatGoal.status.in_(("completed", "cannot_complete", "cancelled")),
  ).order_by(models.ChatGoal.created_at.asc(), models.ChatGoal.id.asc()).all()
  goal_rows = db.query(models.ChatRun).options(load_only(
    models.ChatRun.id, models.ChatRun.chat_id, models.ChatRun.goal_id,
    models.ChatRun.root_run_id, models.ChatRun.goal_objective,
    models.ChatRun.status, models.ChatRun.started_at, models.ChatRun.ended_at,
  )).filter(
    models.ChatRun.chat_id == chat_id,
  ).order_by(models.ChatRun.started_at.asc(), models.ChatRun.id.asc()).all()
  grouped: dict[str, list[models.ChatRun]] = {}
  by_run = {row.id: row for row in goal_rows}
  goals_by_completion_run: dict[str, set[str]] = {}
  completion_receipt_counts: dict[tuple, int] = {}
  for goal in goals:
    if goal.completion_run_id:
      goals_by_completion_run.setdefault(goal.completion_run_id, set()).add(goal.id)
      receipt_key = (goal.completion_run_id, goal.status, goal.result)
      completion_receipt_counts[receipt_key] = completion_receipt_counts.get(receipt_key, 0) + 1
  for row in goal_rows:
    if row.goal_id is not None:
      grouped.setdefault(row.goal_id, []).append(row)

  positions = dict(db.query(
    models.ChatActivityPosition.event_id, models.ChatActivityPosition.position,
  ).filter(
    models.ChatActivityPosition.chat_id == chat_id,
    models.ChatActivityPosition.event_id.in_(
      [f"goal-outcome:{goal.id}" for goal in goals]),
  ).all()) if goals else {}

  assistant_rows: list[tuple[int, int, object]] = []
  for index, message in enumerate(messages):
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    ts = message.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
      assistant_rows.append((index, int(ts), message.get("id")))

  def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
      return None
    if value.tzinfo is None:
      value = value.replace(tzinfo=UTC)
    return round(value.timestamp() * 1000)

  projected: dict[int, list[dict[str, Any]]] = {}
  for root in goals:
    identity = root.id
    rows = grouped.get(identity, [])
    legacy_rebound = not rows and root.completion_run_id is None
    if legacy_rebound:
      # Historical Goals sometimes used the execution root as their id.
      # A later rebind can remove the last mutable run.goal_id link, but the
      # physical root is still an exact old-data anchor for this Goal's time.
      rows = [row for row in goal_rows
        if (row.root_run_id or row.id) == identity
        and (root.completed_at is None or row.started_at <= root.completed_at)]
    completion_run = by_run.get(root.completion_run_id) if root.completion_run_id else None
    latest = completion_run or (rows[-1] if rows else None)
    if latest is None:
      continue
    started_at = min(
      (row.started_at for row in rows if row.started_at is not None),
      default=root.created_at or latest.started_at,
    )
    ended_at = root.completed_at or latest.ended_at
    started_ms = epoch_ms(started_at)
    ended_ms = epoch_ms(ended_at)
    if started_ms is None or ended_ms is None:
      continue
    position_key = f"goal-outcome:{identity}"
    has_immutable_position = position_key in positions
    position = positions.get(position_key)
    candidate_index = None
    anchor = None
    if isinstance(position, dict):
      message_id = position.get("assistant_message_id")
      candidate_index = next((index for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
        and message_id is not None and message.get("id") == message_id), None)
      block_key = position.get("block_key")
      if candidate_index is not None and isinstance(block_key, str) and block_key.startswith("tool:"):
        anchor = (candidate_index, block_key.removeprefix("tool:"))
    shared_completion_run = (root.completion_run_id is not None
      and len(goals_by_completion_run.get(root.completion_run_id, ())) > 1)
    rebound_without_position = (root.completion_run_id is not None
      and completion_run is not None and completion_run.goal_id != identity)
    ambiguous_run = shared_completion_run or rebound_without_position
    if (not has_immutable_position and shared_completion_run
        and completion_receipt_counts.get(
          (root.completion_run_id, root.status, root.result), 0) > 1):
      # Matching receipt arguments cannot distinguish two identical outcomes
      # on one physical run. Never attribute the later Goal's receipt to A.
      continue
    if not has_immutable_position:
      # An immutable frontier stays authoritative even if its message is
      # unavailable. Only its absence permits a legacy receipt or segment.
      # Migrated rows can know the completion run but lack an immutable outcome
      # position. A rebound/shared run needs an exact result-bearing receipt;
      # an unqualified success could belong to the other Goal.
      receipt_runs = ({root.completion_run_id} if root.completion_run_id
                      else {row.id for row in rows})
      anchor = _goal_completion_anchor(
        messages, receipt_runs, root.result, root.status,
        max_timestamp_ms=ended_ms if legacy_rebound else None,
        require_exact_result=ambiguous_run,
      )
      if anchor is not None:
        candidate_index = anchor[0]
      elif not ambiguous_run:
        # Without a receipt, use the run's last segment. A rebound run cannot
        # safely use this fallback because the segment may belong to B.
        candidate_index = next((index for index in range(len(messages) - 1, -1, -1)
          if isinstance(messages[index], dict)
          and messages[index].get("role") == "assistant"
          and assistant_message_run_id(messages[index].get("id")) == latest.id), None)
        if candidate_index is None:
          candidate_index = next((index for index, ts, message_id in reversed(assistant_rows)
            if message_id is None and started_ms - 1000 <= ts <= ended_ms + 1000), None)
    if (candidate_index is None or candidate_index < message_start
        or (message_end is not None and candidate_index >= message_end)):
      continue
    plan = serialize_plan(db, latest, root)
    presentation = _goal_presentation(db, latest, root, plan)
    projected.setdefault(candidate_index, []).append({
      **presentation,
      "started_at": started_at.isoformat(),
      "completed_at": ended_at.isoformat(),
      "duration_seconds": max(0, round((ended_ms - started_ms) / 1000)),
      **({"completion_tool_use_id": anchor[1]} if anchor else {}),
    })
  return projected


def replace_plan(
  db: Session,
  *,
  physical: models.ChatRun,
  root: models.ChatGoal,
  expected_revision: int,
  tasks: Any,
) -> dict[str, Any]:
  normalized = normalize_tasks(tasks)
  current_tasks = (
    root.plan_json.get("tasks")
    if isinstance(root.plan_json, dict)
    and isinstance(root.plan_json.get("tasks"), list)
    else None
  )
  try:
    current_normalized = normalize_tasks(current_tasks)
  except GoalPlanError:
    # An invalid saved plan cannot be an identical no-op, but a fully
    # validated replacement must still be able to repair it.
    current_normalized = None
  if current_normalized == normalized:
    # Identical saves must not manufacture progress and thereby authorize a
    # fresh provider turn. The no-op UPDATE retains the optimistic CAS: a
    # stale writer still conflicts even when its payload matches current data.
    result = db.execute(
      update(models.ChatGoal)
      .where(
        models.ChatGoal.id == root.id,
        models.ChatGoal.status == "open",
        models.ChatGoal.revision == expected_revision,
      )
      .values(revision=expected_revision)
    )
    if result.rowcount != 1:
      db.rollback()
      raise GoalPlanConflict("goal plan changed; fetch it and retry")
    db.commit()
    db.refresh(root)
    plan = serialize_plan(db, physical, root)
    if plan is None:  # pragma: no cover - current_tasks proves it exists
      raise RuntimeError("goal plan disappeared during no-op update")
    return plan
  document = {
    "version": 1,
    "updated_at": datetime.now(UTC).isoformat(),
    "tasks": normalized,
  }
  result = db.execute(
    update(models.ChatGoal)
    .where(
      models.ChatGoal.id == root.id,
      models.ChatGoal.status == "open",
      models.ChatGoal.revision == expected_revision,
    )
    .values(
      plan_json=document,
      revision=expected_revision + 1,
    )
  )
  if result.rowcount != 1:
    db.rollback()
    raise GoalPlanConflict("goal plan changed; fetch it and retry")
  db.commit()
  db.refresh(root)
  plan = serialize_plan(db, physical, root)
  if plan is None:  # pragma: no cover - the write above guarantees a document
    raise RuntimeError("goal plan disappeared after commit")
  publish_goal_changed(root.chat_id)
  return plan


def staged_task_edits(root: models.ChatGoal, edits: list[dict[str, Any]]) -> dict[str, Any]:
  """Build a validated plan document without committing it.

  Goal outcome writes use this document in their single revision/CAS update.
  """
  saved = root.plan_json.get("tasks") if isinstance(root.plan_json, dict) else None
  tasks = [dict(task) for task in saved if isinstance(task, dict)] if isinstance(saved, list) else []
  by_id = {task.get("id"): task for task in tasks}
  for position, edit in enumerate(edits):
    if not isinstance(edit, dict) or not isinstance(edit.get("id"), str):
      raise GoalPlanError(f"task edit {position + 1} needs a string id")
    unknown = set(edit) - TASK_EDIT_FIELDS - {"id"}
    if unknown:
      raise GoalPlanError(f"unknown fields for {edit['id']}: {', '.join(sorted(unknown))}")
    target = by_id.get(edit["id"])
    if target is None:
      target = {"id": edit["id"]}
      tasks.append(target)
      by_id[edit["id"]] = target
    target.update({key: value for key, value in edit.items() if key != "id"})
  normalized = normalize_tasks(tasks)
  return {"version": 1, "updated_at": datetime.now(UTC).isoformat(), "tasks": normalized}


TASK_EDIT_FIELDS = frozenset({
  "title", "status", "depends_on", "parent_id", "completion_condition",
  "note", "result", "progress",
})
