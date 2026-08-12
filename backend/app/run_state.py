"""Canonical read-side queries for durable chat-run state.

``ChatRun`` is the sole durable source of truth for whether work is running,
parked, or awaiting continuation. Runtime ownership still lives in
``runner_registry`` and the writer actor, but persisted state must never be
reconstructed from a second per-chat marker.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from app import models


def goal_objective_for_run_start(
  db: Session,
  chat_id: str,
  message: Mapping[str, Any] | None,
  run_token: str | None = None,
) -> str | None:
  """Resolve the goal metadata for a newly opened durable run.

  Explicit ``/goal`` commands start a new objective. A real continuation may
  inherit only from the immediately preceding unfinished/interrupted run; an
  ordinary turn after a completed goal therefore cannot revive stale UI state.
  """
  from app.chat_context import _goal_objective
  from app.continuations import is_continuation_message

  # A goal-plan stage owns its exact run identity.  The writer claims retries
  # and hidden stage continuations before reaching this read, so this lookup
  # can never accidentally revive a completed plan on an ordinary later turn.
  if run_token is not None:
    from app.goal_plans import active_plan_for_run, stage_label

    plan = active_plan_for_run(db, chat_id, run_token)
    if plan is not None:
      return stage_label(plan)

  content = message.get("content") if message is not None else ""
  objective = _goal_objective(content if isinstance(content, str) else "")
  if objective is not None:
    return objective
  if not is_continuation_message(message):
    return None
  previous = latest_run(db, chat_id)
  if previous is None or previous.status not in (
    *models.NONTERMINAL_RUN_STATUSES,
    "interrupted",
  ):
    return None
  return previous.goal_objective


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
  """Return only the active run's goal label for lightweight UI reads."""
  row = (
    db.query(models.ChatRun.goal_objective)
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
  return row[0] if row is not None else None


def running_goal_plan(db: Session, chat_id: str) -> dict | None:
  """Return plan progress only while its exact current stage is running."""
  row = running_run(db, chat_id)
  if row is None:
    return None
  from app.goal_plans import active_plan_summary
  return active_plan_summary(db, chat_id, row.id)


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
