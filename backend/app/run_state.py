"""Canonical read-side queries for durable chat-run state.

``ChatRun`` is the sole durable source of truth for whether work is running,
parked, or awaiting continuation. Runtime ownership still lives in
``runner_registry`` and the writer actor, but persisted state must never be
reconstructed from a second per-chat marker.
"""

from collections.abc import Iterable, Mapping
from typing import Any
import json
import uuid

from sqlalchemy.orm import Session

from app import models


def goal_identity_for_run_start(
  db: Session,
  chat_id: str,
  message: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
  """Resolve objective + stable Goal identity before prior runs are closed."""
  from app.chat_context import _goal_objective
  from app.continuations import is_continuation_message

  content = message.get("content") if message is not None else ""
  objective = _goal_objective(content if isinstance(content, str) else "")
  if objective is not None:
    return objective, str(uuid.uuid4())
  from app.continuations import DELEGATION_RESULT_MESSAGE_KIND
  if (
    isinstance(message, Mapping)
    and message.get("kind") == DELEGATION_RESULT_MESSAGE_KIND
  ):
    source_work_id = message.get("source_work_id")
    if isinstance(source_work_id, str) and source_work_id:
      source = (
        db.query(models.ChatRun)
        .filter(
          models.ChatRun.chat_id == chat_id,
          models.ChatRun.goal_id == source_work_id,
          models.ChatRun.goal_objective.isnot(None),
        )
        .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
        .first()
      )
      if source is not None:
        return source.goal_objective, source.goal_id
    return None, None
  previous = latest_run(db, chat_id)
  semantic_continuation = is_continuation_message(message)
  literal_continue = str(content or "").strip().lower() == "continue"
  if not semantic_continuation and not literal_continue:
    return None, None
  if (
    previous is not None
    and previous.goal_objective is not None
  ):
    if (
      semantic_continuation
      and previous.status in (
        *models.NONTERMINAL_RUN_STATUSES, "interrupted",
      )
    ):
      # Pre-identity Goal rows can still exist in backups and fixtures. Keep
      # their objective through an explicit semantic continuation; the normal
      # migration supplies a stable id for current production rows.
      return previous.goal_objective, previous.goal_id
    if (
      previous.goal_id is not None
      and _goal_plan_is_unfinished(db, chat_id, previous.goal_id)
    ):
      return previous.goal_objective, previous.goal_id
  if not semantic_continuation:
    return None, None

  # A restart can interrupt a physical continuation after its provider turn
  # has already closed, leaving one no-goal recovery row between the new
  # continuation marker and the logical Goal. Follow only an explicitly
  # unfinished visible plan; completed or unplanned historical Goals never
  # revive through this recovery path.
  candidates = (
    db.query(models.ChatRun)
    .filter(
      models.ChatRun.chat_id == chat_id,
      models.ChatRun.goal_id.isnot(None),
      models.ChatRun.goal_objective.isnot(None),
    )
    .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
    .all()
  )
  seen: set[str] = set()
  for candidate in candidates:
    if candidate.goal_id in seen:
      continue
    seen.add(candidate.goal_id)
    if _goal_plan_is_unfinished(db, chat_id, candidate.goal_id):
      return candidate.goal_objective, candidate.goal_id
  return None, None


def _goal_plan_is_unfinished(db: Session, chat_id: str, goal_id: str) -> bool:
  """Whether a stable Goal identity owns a plan with unsettled work."""
  owner = (
    db.query(models.ChatRun.goal_plan_json)
    .filter(
      models.ChatRun.chat_id == chat_id,
      models.ChatRun.goal_id == goal_id,
      models.ChatRun.goal_plan_json.isnot(None),
    )
    .order_by(models.ChatRun.started_at.asc(), models.ChatRun.id.asc())
    .first()
  )
  if owner is None:
    return False
  raw = owner[0]
  try:
    plan = json.loads(raw) if isinstance(raw, str) else raw
  except (TypeError, json.JSONDecodeError):
    return False
  tasks = (plan or {}).get("tasks") if isinstance(plan, dict) else None
  return bool(tasks) and any(
    isinstance(task, dict)
    and task.get("status") not in ("completed", "cancelled")
    for task in tasks
  )


def latest_run(db: Session, chat_id: str) -> models.ChatRun | None:
  """Return the deterministic latest durable run for one chat."""
  return (
    db.query(models.ChatRun)
    .filter(models.ChatRun.chat_id == chat_id)
    .order_by(
      models.ChatRun.started_at.desc(),
      models.ChatRun.id.desc(),
    )
    .first()
  )


def run_is_latest(db: Session, run: models.ChatRun) -> bool:
  """Whether ``run`` is the deterministic latest run for its chat."""
  latest = latest_run(db, run.chat_id)
  return latest is not None and latest.id == run.id


def has_run_in(
  db: Session,
  chat_id: str,
  statuses: Iterable[str],
) -> bool:
  """Whether a chat has any durable run in one of ``statuses``."""
  wanted = tuple(statuses)
  if not wanted:
    return False
  return db.query(models.ChatRun.id).filter(
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.status.in_(wanted),
  ).first() is not None


def has_running_run(db: Session, chat_id: str) -> bool:
  return has_run_in(db, chat_id, ("running",))


def has_nonterminal_run(db: Session, chat_id: str) -> bool:
  return has_run_in(db, chat_id, models.NONTERMINAL_RUN_STATUSES)


def running_run(db: Session, chat_id: str) -> models.ChatRun | None:
  """Return the latest currently-running row for one chat."""
  return (
    db.query(models.ChatRun)
    .filter(
      models.ChatRun.chat_id == chat_id,
      models.ChatRun.status == "running",
    )
    .order_by(
      models.ChatRun.started_at.desc(),
      models.ChatRun.id.desc(),
    )
    .first()
  )


def running_goal_objective(db: Session, chat_id: str) -> str | None:
  """Return the live or latest still-unsettled Goal label for UI reads."""
  from app.goal_plans import active_goal_rows

  rows = active_goal_rows(db, chat_id)
  return rows[0].goal_objective if rows is not None else None


def running_chat_ids(
  db: Session,
  chat_ids: Iterable[str] | None = None,
) -> set[str]:
  """Return chat ids with a durable running row, optionally bounded."""
  query = db.query(models.ChatRun.chat_id).filter(
    models.ChatRun.status == "running",
  )
  if chat_ids is not None:
    bounded = tuple(dict.fromkeys(chat_ids))
    if not bounded:
      return set()
    query = query.filter(models.ChatRun.chat_id.in_(bounded))
  return {str(row[0]) for row in query.distinct().all()}
