"""Canonical read-side queries for durable chat-run state.

``ChatRun`` is the sole durable source of truth for whether work is running,
parked, or awaiting continuation. Runtime ownership still lives in
``runner_registry`` and the writer actor, but persisted state must never be
reconstructed from a second per-chat marker.
"""

from collections.abc import Iterable, Mapping
from typing import Any
import uuid

from sqlalchemy.orm import Session

from app import models
from app.goal_commands import (
  goal_objective,
  is_goal_continue,
  is_natural_goal_resume,
)


def _recoverable_result_goal(
  db: Session,
  chat_id: str,
  source: models.ChatRun | None,
) -> tuple[str | None, str | None]:
  """A delivery names its Goal; only the Goal's explicit outcome can retire it."""
  if source is None or not source.goal_id:
    return None, None
  goal = db.get(models.ChatGoal, source.goal_id)
  if goal is None or goal.chat_id != chat_id or goal.status != "open":
    return None, None
  return goal.objective, goal.id


def goal_identity_for_run_start(db, chat_id, message):
  """Resolve explicit intent and exact delivery identity, never attempt outcome."""
  from app.continuations import (
    continuation_reason, is_continuation_message,
    DELEGATION_RESULT_MESSAGE_KIND, PEER_MESSAGE_WAKE_KIND,
    PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND, WAIT_RESULT_MESSAGE_KIND,
  )
  message = message or {}
  content = str(message.get("content") or "")
  objective = goal_objective(content)
  if objective is not None:
    return objective, str(uuid.uuid4())
  reason = continuation_reason(message)
  kind = message.get("kind")
  exact_goal_id = None
  if reason == GOAL_HANDOFF_REASON:
    exact_goal_id = message.get("goal_id")
  elif kind == DELEGATION_RESULT_MESSAGE_KIND:
    exact_goal_id = message.get("source_work_id")
  elif kind in {WAIT_RESULT_MESSAGE_KIND, PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND,
                PEER_MESSAGE_WAKE_KIND}:
    source_id = message.get("source_work_id")
    source = db.get(models.ChatRun, source_id) if source_id else None
    return _recoverable_result_goal(db, chat_id, source)
  if exact_goal_id:
    goal = db.get(models.ChatGoal, exact_goal_id)
    if goal is not None and goal.chat_id == chat_id and goal.status == "open":
      return goal.objective, goal.id
    return None, None
  if kind in {DELEGATION_RESULT_MESSAGE_KIND} or reason == GOAL_HANDOFF_REASON:
    return None, None

  manual = reason == "manual"
  natural = not kind and is_natural_goal_resume(content)
  literal = is_goal_continue(content)
  semantic = is_continuation_message(message)
  if not (manual or natural or literal or semantic):
    return None, None
  if natural:
    pending_question = db.query(models.Chat.pending_question_id).filter(
      models.Chat.id == chat_id,
    ).scalar()
    waiting = db.query(models.ChatWait.id).filter(
      models.ChatWait.chat_id == chat_id, models.ChatWait.status == "armed",
    ).first()
    if pending_question or waiting:
      return None, None
  if semantic and not (manual or literal):
    previous = latest_run(db, chat_id)
    if previous is not None and previous.goal_id:
      return _recoverable_result_goal(db, chat_id, previous)
    # A recovery may have an intervening goal-less failed attempt. The latest
    # durable work record still owns intent; there is no run-history search.

  goal = db.query(models.ChatGoal).filter(
    models.ChatGoal.chat_id == chat_id,
  ).order_by(models.ChatGoal.created_at.desc(), models.ChatGoal.id.desc()).first()
  if goal is None or goal.status not in ({"open", "stopped"} if manual or literal and not semantic else {"open"}):
    return None, None
  return goal.objective, goal.id


def product_result_continuation_root(
  db: Session,
  chat_id: str,
  message: Mapping[str, Any] | None,
) -> str | None:
  """Resolve queued result data to the work that produced it, not queue tail."""
  if not isinstance(message, Mapping):
    return None
  from app.continuations import (
    DELEGATION_RESULT_MESSAGE_KIND,
    WAIT_RESULT_MESSAGE_KIND,
    product_result_run_token,
  )

  kind = message.get("kind")
  source_work_id = message.get("source_work_id")
  physical_result_id = product_result_run_token(chat_id, message)
  if not isinstance(source_work_id, str) or not source_work_id:
    return physical_result_id if kind == WAIT_RESULT_MESSAGE_KIND else None
  query = db.query(models.ChatRun).filter(models.ChatRun.chat_id == chat_id)
  if kind == WAIT_RESULT_MESSAGE_KIND:
    source = query.filter(models.ChatRun.id == source_work_id).first()
  elif kind == DELEGATION_RESULT_MESSAGE_KIND:
    source = query.filter(
      models.ChatRun.goal_id == source_work_id,
    ).order_by(
      models.ChatRun.started_at.desc(), models.ChatRun.id.desc(),
    ).first()
    if source is None:
      source = query.filter(models.ChatRun.id == source_work_id).first()
  else:
    return None
  if source is None:
    return physical_result_id if kind == WAIT_RESULT_MESSAGE_KIND else None
  root_run_id = source.root_run_id or source.id
  exists = db.query(models.ChatRun.id).filter(
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.id == root_run_id,
  ).first()
  return root_run_id if exists is not None else None


# Durable hidden continuations use this reason to retain the exact Goal across
# physical turns. Older transcripts use the same value and remain recoverable.
GOAL_HANDOFF_REASON = "goal_handoff"


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


def latest_provider_goal_is_dismissed(db: Session, chat_id: str) -> bool:
  """Whether the newest provider-backed Goal is hidden by direct clearing."""
  dismissed_goal_id = db.query(models.Chat.dismissed_goal_id).filter(
    models.Chat.id == chat_id,
  ).scalar()
  if not dismissed_goal_id:
    return False
  latest_goal_row = (
    db.query(models.ChatRun.goal_id)
    .filter(
      models.ChatRun.chat_id == chat_id,
      models.ChatRun.goal_id.isnot(None),
    )
    .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
    .first()
  )
  latest_goal_id = latest_goal_row[0] if latest_goal_row is not None else None
  return latest_goal_id == dismissed_goal_id


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
