"""Canonical read-side queries for durable chat-run state.

``ChatRun`` is the sole durable source of truth for whether work is running,
parked, or awaiting continuation. Runtime ownership still lives in
``runner_registry`` and the writer actor, but persisted state must never be
reconstructed from a second per-chat marker.
"""

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app import models


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
