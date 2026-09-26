"""Durable, dependency-aware todo plans attached to logical Goal runs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.orm import Session, load_only

from app import models


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_STATUSES = frozenset({
  "pending", "running", "completed", "blocked", "failed", "cancelled",
})
ACTIVE_TASK_STATUSES = frozenset({"running"})
SETTLED_TASK_STATUSES = frozenset({"completed", "cancelled"})
MAX_TASKS = 64
MAX_DEPENDENCIES = 16
MAX_TITLE = 160
MAX_NOTE = 500
MAX_RESULT = 1000


def goal_plan_is_unfinished(
  db: Session, chat_id: str, goal_id: str,
) -> bool:
  """Whether the durable obligation is still open, even without a plan."""
  goal = db.get(models.ChatGoal, goal_id)
  return goal is not None and goal.chat_id == chat_id and goal.status == "open"


def goal_plan_revision(db: Session, chat_id: str, goal_id: str) -> int:
  """Return the exact durable plan revision for one stable Goal."""
  goal = db.get(models.ChatGoal, goal_id)
  if goal is None or goal.chat_id != chat_id:
    return 0
  return int(goal.revision or 0)


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

  def visit_completion_prerequisites(task_id: str) -> None:
    if task_id in visited:
      return
    if task_id in visiting:
      raise GoalPlanError(
        "goal-plan dependencies and parentage must not form a completion cycle"
      )
    visiting.add(task_id)
    prerequisites = (
      by_id[task_id]["depends_on"] + children_by_parent.get(task_id, [])
    )
    for prerequisite in prerequisites:
      visit_completion_prerequisites(prerequisite)
    visiting.remove(task_id)
    visited.add(task_id)

  for task in tasks:
    visit_completion_prerequisites(task["id"])

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
  """
  rows = active_goal_rows(db, chat_id)
  tasks = list(((rows[1].plan_json or {}) if rows else {}).get("tasks") or [])
  if requested is not None:
    ids = [str(task.get("id")) for task in tasks]
    if requested not in ids:
      known = ", ".join(ids) if ids else "none (no active Goal plan)"
      raise GoalPlanError(
        f"plan_task {requested!r} is not a task of this chat's Goal plan; "
        f"plan tasks: {known}"
      )
    return requested
  running = {
    str(task.get("id")): task for task in tasks if task.get("status") == "running"
  }
  parents = {str(task.get("parent_id")) for task in running.values()}
  leaves = [task_id for task_id in running if task_id not in parents]
  return leaves[0] if len(leaves) == 1 else None


def presented_goal_rows(
  db: Session, chat_id: str,
) -> tuple[models.ChatRun, models.ChatGoal] | None:
  """Return the latest Goal that remains visible until an explicit clear.

  ``Chat.dismissed_goal_id`` suppresses only the exact Goal the owner cleared;
  later ordinary turns cannot revive it, while a genuinely new Goal has a new
  identity and naturally becomes visible. Execution liveness is deliberately
  absent from this query so completed and paused Goals survive reloads.
  """
  physical = (
    db.query(models.ChatRun)
    .filter(
      models.ChatRun.chat_id == chat_id,
      or_(
        models.ChatRun.goal_id.isnot(None),
        models.ChatRun.goal_objective.isnot(None),
      ),
    )
    .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
    .first()
  )
  if physical is None or physical.goal_objective is None:
    return None
  dismissed_goal_id = db.query(models.Chat.dismissed_goal_id).filter(
    models.Chat.id == chat_id,
  ).scalar()
  if physical.goal_id is not None and physical.goal_id == dismissed_goal_id:
    return None
  return _goal_rows_for_physical(db, physical)


def _delegation_tree(
  db: Session, physical: models.ChatRun, root: models.ChatGoal,
) -> list[dict[str, Any]]:
  """Project durable immediate-child ownership without copying transcripts."""
  from app.delegations import derived_status

  run_ids = {root.id}
  if physical.goal_id:
    run_ids.add(physical.goal_id)
    run_ids.update(
      str(value) for (value,) in db.query(models.ChatRun.root_run_id).filter(
        models.ChatRun.chat_id == physical.chat_id,
        models.ChatRun.goal_id == physical.goal_id,
      ).all() if value
    )
  root_rows = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == physical.chat_id,
    models.Delegation.parent_root_run_id.in_(run_ids),
  ).order_by(models.Delegation.created_at.asc()).all()
  # A resumed Goal may delegate the same plan task again from a newer physical
  # run. Only the latest attempt is current execution; older attempts remain in
  # Workflows history rather than appearing twice (or disagreeing with the
  # compact rail) in the Goal tree.
  roots_by_task = {row.task_key: row for row in root_rows}
  roots = list(roots_by_task.values())
  children_by_parent: dict[str, list[models.Delegation]] = {}
  frontier = [row.child_chat_id for row in roots]
  seen_rows = {row.id for row in roots}
  while frontier:
    child_rows = db.query(models.Delegation).filter(
      models.Delegation.parent_chat_id.in_(frontier),
    ).order_by(models.Delegation.created_at.asc()).all()
    frontier = []
    latest_by_owner_and_task = {
      (child.parent_chat_id, child.task_key): child for child in child_rows
    }
    for child in latest_by_owner_and_task.values():
      if child.id in seen_rows:
        continue
      seen_rows.add(child.id)
      children_by_parent.setdefault(child.parent_chat_id, []).append(child)
      frontier.append(child.child_chat_id)

  def project(row: models.Delegation, seen: set[str]) -> dict[str, Any]:
    if row.id in seen:
      return {"id": row.id, "task_key": row.task_key, "status": "failed", "children": []}
    status, _run, _result = derived_status(db, row, load_result=False)
    children = children_by_parent.get(row.child_chat_id, [])
    return {
      "id": row.id,
      "task_key": row.task_key,
      "plan_task": row.goal_task_id,
      "provider": row.provider,
      "status": status,
      "children": [project(child, seen | {row.id}) for child in children],
    }

  return [project(row, set()) for row in roots]


def publish_plan_for_delegation(
  db: Session, row: models.Delegation,
) -> None:
  """Refresh the root Goal when any locally-owned descendant changes state."""
  top = row
  seen = {row.id}
  while True:
    parent = db.query(models.Delegation).filter(
      models.Delegation.child_chat_id == top.parent_chat_id,
    ).first()
    if parent is None:
      break
    if parent.id in seen:
      return
    seen.add(parent.id)
    top = parent
  rows = active_goal_rows(db, top.parent_chat_id)
  if rows is None:
    return
  plan = serialize_plan(db, *rows)
  if plan is None:
    return
  from app.broadcast import get_broadcast
  broadcast = get_broadcast(top.parent_chat_id)
  if broadcast is not None and broadcast.running:
    broadcast.publish({"type": "goal_plan_updated", "plan": plan})


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
  from app.delegations import TERMINAL_DELEGATION_STATUSES

  active_execution_keys: list[str] = []
  # Plan tasks with a helper still working: such a task is not counted complete
  # even if it was marked so, because its execution has not settled.
  tasks_with_active_helpers: set[str] = set()

  def collect_active_execution(nodes: list[dict[str, Any]]) -> None:
    for node in nodes:
      if node.get("status") not in TERMINAL_DELEGATION_STATUSES:
        active_execution_keys.append(str(node.get("task_key") or node["id"]))
        if node.get("plan_task"):
          tasks_with_active_helpers.add(str(node["plan_task"]))
      collect_active_execution(node.get("children") or [])

  collect_active_execution(delegations)
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
    "goal_id": physical.goal_id,
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


def goal_handoff_owner_kind(
  db: Session,
  chat_id: str,
  goal_id: str,
  *,
  excluding_question_id: str | None = None,
  include_queued_execution: bool = False,
  excluding_automatic_continuations: bool = False,
) -> str | None:
  """Name the durable actor that owns this exact Goal's next move.

  Goal presentation and turn settlement must agree on ownership. Keeping the
  identity check here prevents an unrelated question, Wait, or helper in the
  same chat from making unfinished Goal work look safely handed off.
  """
  # Goal presentation is also read by the small runtime route and once per
  # historical Goal. Do not decode the entire transcript for an absent card.
  # An actual continuation card still lazily reads messages to prove its exact
  # author; a detail read's already-loaded Chat is reused by the identity map.
  projected = [models.Chat.pending_question_id]
  if include_queued_execution:
    projected.append(models.Chat.pending_messages)
  chat = db.query(models.Chat).options(load_only(*projected)).filter(
    models.Chat.id == chat_id,
  ).first()
  pending_question_id = chat.pending_question_id if chat is not None else None
  if pending_question_id is not None and pending_question_id != excluding_question_id:
    from app.questions import continuation_question_owner_run_id
    owner_run_id = continuation_question_owner_run_id(chat, pending_question_id)
    if owner_run_id is not None:
      question_owner = db.get(models.ChatRun, owner_run_id)
    else:
      # Compatibility for pre-message-identity fixtures and legacy cards.
      question_owner = (
        db.query(models.ChatRun)
        .filter(
          models.ChatRun.chat_id == chat_id,
          models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES),
        )
        .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
        .first()
      )
    if (
      question_owner is not None
      and (
        question_owner.goal_id
        or question_owner.root_run_id
        or question_owner.id
      ) == goal_id
    ):
      return "owner_question"

  # A queued continuation is durable execution ownership, not owner speech.
  # Resolve its exact Goal so an unrelated continuation cannot make this Goal
  # look safely handed off.
  if include_queued_execution and chat is not None:
    from app.continuations import continuation_reason, is_continuation_message
    from app.run_state import GOAL_HANDOFF_REASON, goal_identity_for_run_start

    for pending in chat.pending_messages or []:
      if not isinstance(pending, dict) or not is_continuation_message(pending):
        continue
      if (
        excluding_automatic_continuations
        and continuation_reason(pending) == GOAL_HANDOFF_REASON
      ):
        continue
      _objective, pending_goal_id = goal_identity_for_run_start(
        db, chat_id, pending,
      )
      if pending_goal_id == goal_id:
        return "executor"

  from app.delegations import background_helper_goal_ids
  if goal_id in background_helper_goal_ids(db, chat_id):
    return "monitor"

  from app.chat_waits import wait_owns_goal
  if wait_owns_goal(db, chat_id, goal_id):
    return "monitor"
  return None


def _has_runnable_work(plan: dict[str, Any] | None) -> bool:
  """Whether a successor could start, verify, or complete something."""
  return plan is None or plan["summary"]["can_complete"] or any(
    task["ready"] or task["ready_to_verify"] or task.get("status") == "running"
    for task in plan["tasks"]
  )


@dataclass(frozen=True)
class GoalTerminalHandoff:
  """An ownerless unfinished Goal and its plan-owned next move."""

  goal_id: str
  automatic_allowed: bool
  plan_revision: int


def goal_terminal_handoff(
  db: Session, chat_id: str, ending_run_token: str,
) -> GoalTerminalHandoff | None:
  """Describe an unfinished Goal whose clean terminal has no next owner.

  The physical provider turn is still ``running`` while the writer performs
  this check. It is deliberately not an owner of its own future: only a saved
  question, durable monitor/helper, or queued exact continuation may let it
  close without creating the next executor.

  A provider turn cannot authorize its own successor merely because the Goal
  remains unfinished. Another turn is automatic only after the durable plan
  advances beyond the exact revision captured at admission and leaves a task
  a successor could run; recording a blocker alone is not progress. Otherwise
  the terminal path saves an owner question instead of starting an unbounded
  chain of clean, no-progress turns.
  """
  if not ending_run_token:
    return None
  run = db.query(models.ChatRun).filter(
    models.ChatRun.id == ending_run_token,
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.status == "running",
    models.ChatRun.goal_objective.isnot(None),
    models.ChatRun.goal_id.isnot(None),
  ).first()
  if run is None:
    return None
  goal = db.get(models.ChatGoal, run.goal_id)
  if goal is None or goal.status != "open" or goal_handoff_owner_kind(
    db, chat_id, run.goal_id, include_queued_execution=True,
  ) is not None:
    return None
  current_revision = goal_plan_revision(db, chat_id, run.goal_id)
  admitted_revision = run.goal_plan_revision_at_admission
  plan = serialize_plan(db, run, goal) if goal.plan_json is not None else None
  plan_corrupt = goal.plan_json is not None and plan is None
  return GoalTerminalHandoff(
    goal_id=run.goal_id,
    automatic_allowed=(
      not plan_corrupt
      and _has_runnable_work(plan)
      and isinstance(admitted_revision, int)
      and current_revision > admitted_revision
    ),
    plan_revision=current_revision,
  )


def require_quiet_answer_handoff(db: Session, chat, question_id: str) -> None:
  """Closing a card cannot remove the sole next owner of unfinished work.

  This checks the card's exact Goal, not the currently presented Goal or an
  unrelated follow-up. It also keeps a typed approval from silently abandoning
  the exact active work claim it admitted. It neither changes the plan nor
  creates a continuation.
  """
  from app.questions import (
    AnswerConflict, saved_question, saved_question_owner_run_id,
  )
  from app.run_state import goal_identity_for_run_start, _recoverable_result_goal

  card = saved_question(chat, question_id)
  action_key = card.get("action_key") if isinstance(card, dict) else None
  if isinstance(action_key, str) and action_key:
    active_claim = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.work_key == action_key,
      models.AgentWorkClaim.owner_chat_id == chat.id,
      models.AgentWorkClaim.completed_at.is_(None),
      models.AgentWorkClaim.released_at.is_(None),
    ).first()
    if active_claim is not None:
      raise AnswerConflict(
        "This question still owns an active work claim. "
        "Choose a reply option so the agent can release or complete the "
        "claim before closing it without a reply."
      )

  owner_id = saved_question_owner_run_id(chat, question_id)
  owner = db.get(models.ChatRun, owner_id) if owner_id else None
  if owner is None or not owner.goal_objective:
    return
  physical, root = _goal_rows_for_physical(db, owner)
  goal_id = physical.goal_id or root.id
  if _recoverable_result_goal(db, chat.id, owner)[0] is None:
    return  # The durable obligation owns completion/Stop, not its author attempt.
  if goal_handoff_owner_kind(db, chat.id, goal_id, excluding_question_id=question_id):
    return
  for pending in chat.pending_messages or []:
    if isinstance(pending, dict) and goal_identity_for_run_start(db, chat.id, pending)[1] == goal_id:
      return
  raise AnswerConflict(
    "This question is the only next step for an unfinished Goal. "
    "Choose a reply option or Stop the Goal before closing it without a reply."
  )


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
  """Use the same plan snapshot for completion and the historical plan card."""
  wait_kind = goal_handoff_owner_kind(
    db, physical.chat_id, physical.goal_id or root.id,
  )
  if root.status == "completed":
    status = "completed"
  elif root.status in {"stopped", "dismissed"}:
    status = "paused"
  elif physical.status == "running":
    status = "active"
  else:
    status = "paused"
  return {
    "id": physical.goal_id or root.id,
    "objective": root.objective,
    "status": status,
    "resumable": status == "paused",
    **({"wait_kind": wait_kind} if wait_kind is not None else {}),
  }


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


def terminal_goal_summaries_by_message_index(
  db: Session,
  chat_id: str,
  messages: list[dict[str, Any]],
  *,
  message_start: int = 0,
  message_end: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
  """Project completed/failed Goals beside their final assistant message.

  Goal history already belongs to ``ChatRun`` rows; copying it into
  ``Chat.messages`` would create a second persistence mechanism and make plan
  revisions race transcript settlement. This read-side projection keeps one
  durable owner while giving paginated chat history a stable place to render
  each terminal Goal. A summary travels only with the assistant row that
  concluded its final physical run, so ordinary message pagination naturally
  paginates Goal cards too. Find that row in the complete transcript before
  filtering to the requested half-open window; searching only the page would
  move a resumed Goal's card onto an earlier answer. Plans and handoffs for
  off-page Goals are never hydrated.
  """
  goal_rows = (
    db.query(models.ChatRun)
    .options(load_only(
      models.ChatRun.id,
      models.ChatRun.chat_id,
      models.ChatRun.goal_id,
      models.ChatRun.root_run_id,
      models.ChatRun.goal_objective,
      models.ChatRun.status,
      models.ChatRun.started_at,
      models.ChatRun.ended_at,
    ))
    .filter(
      models.ChatRun.chat_id == chat_id,
      models.ChatRun.goal_objective.isnot(None),
    )
    .order_by(models.ChatRun.started_at.asc(), models.ChatRun.id.asc())
    .all()
  )
  grouped: dict[str, list[models.ChatRun]] = {}
  for row in goal_rows:
    identity = row.goal_id or row.root_run_id or row.id
    grouped.setdefault(identity, []).append(row)

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
  for identity, rows in grouped.items():
    latest = rows[-1]
    if latest.ended_at is None:
      continue
    started_at = min(
      (row.started_at for row in rows if row.started_at is not None),
      default=None,
    )
    started_ms = epoch_ms(started_at)
    ended_ms = epoch_ms(latest.ended_at)
    if started_ms is None or ended_ms is None:
      continue
    # Modern assistant segments carry the exact physical-run identity. Prefer
    # it so clock skew cannot attach a Goal card to an unrelated answer. The
    # bounded timestamp fallback is reserved for genuinely id-less legacy rows.
    assistant_id = latest.id
    segment_prefix = f"{assistant_id}:assistant:"
    candidate_index = next((
      index for index in range(len(messages) - 1, -1, -1)
      if isinstance(messages[index], dict)
      and messages[index].get("role") == "assistant"
      and (
        messages[index].get("id") == assistant_id
        or (
          isinstance(messages[index].get("id"), str)
          and messages[index]["id"].startswith(segment_prefix)
          and re.fullmatch(
            r"[1-9][0-9]*",
            messages[index]["id"][len(segment_prefix):],
          ) is not None
        )
      )
    ), None)
    if candidate_index is None:
      candidate_index = next((
        index for index, ts, message_id in reversed(assistant_rows)
        if message_id is None
        and started_ms - 1000 <= ts <= ended_ms + 1000
      ), None)
    if (
      candidate_index is None
      or candidate_index < message_start
      or (message_end is not None and candidate_index >= message_end)
    ):
      continue
    physical, root = _goal_rows_for_physical(db, latest)
    plan = serialize_plan(db, physical, root)
    presentation = _goal_presentation(db, physical, root, plan)
    if presentation["status"] not in {"completed", "failed"}:
      continue
    projected.setdefault(candidate_index, []).append({
      **presentation,
      "id": identity,
      "started_at": started_at.isoformat() if started_at else None,
      "completed_at": latest.ended_at.isoformat(),
      "duration_seconds": max(0, round((ended_ms - started_ms) / 1000)),
      "plan": plan,
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
  return plan


def update_task(
  db: Session,
  *,
  physical: models.ChatRun,
  root: models.ChatGoal,
  expected_revision: int,
  task_id: str,
  changes: dict[str, Any],
) -> dict[str, Any]:
  existing = serialize_plan(db, physical, root)
  if existing is None:
    raise GoalPlanError("this Goal does not have a plan yet")
  tasks = existing["tasks"]
  target = next((task for task in tasks if task["id"] == task_id), None)
  if target is None:
    raise GoalPlanError(f"unknown task id: {task_id}")
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
