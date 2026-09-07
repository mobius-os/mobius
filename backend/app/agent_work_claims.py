"""Atomic workspace ownership for work shared by otherwise independent chats."""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


WORK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9:._/@-]{2,255}$")


def clean_work_key(value: str) -> str:
  key = value.strip()
  if not WORK_KEY_RE.fullmatch(key):
    raise ValueError(
      "work_key must be 3-256 lowercase letters, numbers, or : . _ / @ -"
    )
  return key


def _goal_id(db: Session, chat_id: str, run_id: str) -> str | None:
  return db.query(models.ChatRun.goal_id).filter(
    models.ChatRun.id == run_id,
    models.ChatRun.chat_id == chat_id,
  ).scalar()


def _serialize(db: Session, row: models.AgentWorkClaim, state: str) -> dict:
  title = db.query(models.Chat.title).filter(
    models.Chat.id == row.owner_chat_id,
  ).scalar()
  result = {
    "id": row.id,
    "work_key": row.work_key,
    "summary": row.summary,
    "state": state,
    "owner_chat_id": row.owner_chat_id,
    "owner_name": title or row.owner_chat_id or "Deleted chat",
    "owner_goal_id": row.owner_goal_id,
    "revision": row.revision,
    "notification_pending": row.notification_revision < row.revision,
    "takeover_reason": row.takeover_reason,
    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    "outcome": row.outcome,
  }
  if state == "held_by_peer":
    result["next_action"] = (
      "Do not duplicate this exact action. Reconcile your own Goal now: "
      "continue any independent work and settle the overlapping plan task, "
      "or explicitly transfer the broader outcome. Following this claim does "
      "not transfer ownership of your whole Goal."
    )
  return result


def _follow(
  db: Session, row: models.AgentWorkClaim, *, chat_id: str, goal_id: str | None,
) -> None:
  if not goal_id or row.owner_chat_id == chat_id:
    return
  existing = db.query(models.AgentWorkInterest).filter(
    models.AgentWorkInterest.claim_id == row.id,
    models.AgentWorkInterest.chat_id == chat_id,
    models.AgentWorkInterest.goal_id == goal_id,
  ).first()
  if existing is None:
    db.add(models.AgentWorkInterest(
      id=str(uuid.uuid4()), claim_id=row.id, chat_id=chat_id, goal_id=goal_id,
    ))
  elif existing.resolved_at is not None:
    existing.resolved_at = None


def claim_work(
  db: Session,
  *,
  owner_id: int,
  chat_id: str,
  run_id: str,
  work_key: str,
  summary: str,
  takeover_reason: str | None = None,
  expected_owner_chat_id: str | None = None,
) -> dict:
  """Acquire first claim, observe its owner, or deliberately transfer it."""
  key = clean_work_key(work_key)
  clean_summary = summary.strip()
  if not clean_summary or len(clean_summary) > 500:
    raise ValueError("summary must be 1-500 characters")
  reason = takeover_reason.strip() if takeover_reason else None
  if reason is not None and not 10 <= len(reason) <= 1000:
    raise ValueError("takeover_reason must be 10-1000 characters")
  goal_id = _goal_id(db, chat_id, run_id)

  row = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_id == owner_id,
    models.AgentWorkClaim.work_key == key,
  ).first()
  if row is None:
    row = models.AgentWorkClaim(
      id=str(uuid.uuid4()), owner_id=owner_id, work_key=key,
      summary=clean_summary, owner_chat_id=chat_id, owner_run_id=run_id,
      owner_goal_id=goal_id,
    )
    db.add(row)
    try:
      db.commit()
      db.refresh(row)
      return _serialize(db, row, "claimed")
    except IntegrityError:
      # Another chat won the same first-claim race.
      db.rollback()
      row = db.query(models.AgentWorkClaim).filter(
        models.AgentWorkClaim.owner_id == owner_id,
        models.AgentWorkClaim.work_key == key,
      ).one()

  if row.completed_at is not None:
    return _serialize(db, row, "completed")
  if row.released_at is not None:
    previous_revision = row.revision
    next_revision = previous_revision + 1
    now = now_naive_utc()
    changed = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.id == row.id,
      models.AgentWorkClaim.revision == previous_revision,
      models.AgentWorkClaim.released_at.is_not(None),
      models.AgentWorkClaim.completed_at.is_(None),
    ).update({
      models.AgentWorkClaim.summary: clean_summary,
      models.AgentWorkClaim.owner_chat_id: chat_id,
      models.AgentWorkClaim.owner_run_id: run_id,
      models.AgentWorkClaim.owner_goal_id: goal_id,
      models.AgentWorkClaim.previous_owner_chat_id: None,
      models.AgentWorkClaim.takeover_reason: None,
      models.AgentWorkClaim.released_at: None,
      models.AgentWorkClaim.outcome: None,
      models.AgentWorkClaim.claimed_at: now,
      models.AgentWorkClaim.updated_at: now,
      models.AgentWorkClaim.revision: next_revision,
      models.AgentWorkClaim.notification_revision: next_revision,
    }, synchronize_session=False)
    if changed != 1:
      db.rollback()
      raise ValueError("The released claim changed; inspect it before retrying.")
    db.commit()
    row = db.get(models.AgentWorkClaim, row.id)
    db.refresh(row)
    return _serialize(db, row, "claimed")
  if row.owner_chat_id == chat_id:
    previous_revision = row.revision
    changed = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.id == row.id,
      models.AgentWorkClaim.owner_chat_id == chat_id,
      models.AgentWorkClaim.revision == previous_revision,
      models.AgentWorkClaim.completed_at.is_(None),
      models.AgentWorkClaim.released_at.is_(None),
    ).update({
      models.AgentWorkClaim.summary: clean_summary,
      models.AgentWorkClaim.owner_run_id: run_id,
      models.AgentWorkClaim.owner_goal_id: goal_id or row.owner_goal_id,
      models.AgentWorkClaim.updated_at: now_naive_utc(),
    }, synchronize_session=False)
    if changed != 1:
      db.rollback()
      raise ValueError("The claim changed while refreshing; inspect it before retrying.")
    db.commit()
    db.expire(row)
    db.refresh(row)
    pending_transfer = (
      row.previous_owner_chat_id is not None
      and row.notification_revision < row.revision
    )
    result = _serialize(db, row, "transferred" if pending_transfer else "owned")
    if pending_transfer:
      result["previous_owner_chat_id"] = row.previous_owner_chat_id
    return result
  if reason is None:
    _follow(db, row, chat_id=chat_id, goal_id=goal_id)
    db.commit()
    db.refresh(row)
    return _serialize(db, row, "held_by_peer")
  if expected_owner_chat_id != row.owner_chat_id:
    raise ValueError(
      "The expected owner changed; inspect the current claim before transfer."
    )

  previous = row.owner_chat_id
  previous_goal_id = row.owner_goal_id
  previous_revision = row.revision
  now = now_naive_utc()
  changed = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.id == row.id,
    models.AgentWorkClaim.owner_chat_id == expected_owner_chat_id,
    models.AgentWorkClaim.revision == previous_revision,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).update({
    models.AgentWorkClaim.summary: clean_summary,
    models.AgentWorkClaim.previous_owner_chat_id: previous,
    models.AgentWorkClaim.owner_chat_id: chat_id,
    models.AgentWorkClaim.owner_run_id: run_id,
    models.AgentWorkClaim.owner_goal_id: goal_id,
    models.AgentWorkClaim.takeover_reason: reason,
    models.AgentWorkClaim.updated_at: now,
    models.AgentWorkClaim.revision: previous_revision + 1,
  }, synchronize_session=False)
  if changed != 1:
    db.rollback()
    raise ValueError("The claim changed during transfer; inspect it before retrying.")
  db.expire(row)
  db.refresh(row)
  _follow(db, row, chat_id=previous, goal_id=previous_goal_id)
  db.query(models.AgentWorkInterest).filter(
    models.AgentWorkInterest.claim_id == row.id,
    models.AgentWorkInterest.chat_id == chat_id,
    models.AgentWorkInterest.resolved_at.is_(None),
  ).update({models.AgentWorkInterest.resolved_at: now_naive_utc()})
  db.commit()
  db.refresh(row)
  result = _serialize(db, row, "transferred")
  result["previous_owner_chat_id"] = previous
  return result


@dataclass(frozen=True)
class FinishedClaim:
  claim: dict
  interested_chat_ids: list[str]


@dataclass(frozen=True)
class ReleasedClaim:
  """One unfinished exact action released by chat deletion."""

  claim_id: str
  work_key: str
  revision: int
  interested_chat_ids: list[str]


def stage_release_claims_for_chat(
  db: Session, chat_id: str,
) -> list[ReleasedClaim]:
  """Release a deleted chat's unfinished exact actions in the caller's txn.

  A chat owns its promised Goal, but each workspace claim owns only one shared
  action. Deleting the chat ends both responsibilities: followers may reclaim
  the action, while completed claims remain untouched as idempotency history.
  The caller commits the release atomically with the chat tombstone and then
  delivers the returned follower notices.
  """
  now = now_naive_utc()
  rows = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_chat_id == chat_id,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).all()
  released: list[ReleasedClaim] = []
  for row in rows:
    row.released_at = now
    row.updated_at = now
    row.outcome = "Owning chat was deleted before this action completed."
    row.revision += 1
    recipients = [
      interest.chat_id
      for interest in db.query(models.AgentWorkInterest).join(
        models.Chat, models.Chat.id == models.AgentWorkInterest.chat_id,
      ).filter(
        models.AgentWorkInterest.claim_id == row.id,
        models.AgentWorkInterest.resolved_at.is_(None),
        models.AgentWorkInterest.chat_id != chat_id,
        models.Chat.deleted_at.is_(None),
      ).all()
    ]
    released.append(ReleasedClaim(
      claim_id=row.id,
      work_key=row.work_key,
      revision=row.revision,
      interested_chat_ids=list(dict.fromkeys(recipients)),
    ))
  db.flush()
  return released


def finish_work(
  db: Session,
  *,
  owner_id: int,
  chat_id: str,
  work_key: str,
  outcome: str,
  release: bool,
) -> FinishedClaim:
  key = clean_work_key(work_key)
  clean_outcome = outcome.strip()
  if not clean_outcome or len(clean_outcome) > 1000:
    raise ValueError("outcome must be 1-1000 characters")
  row = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_id == owner_id,
    models.AgentWorkClaim.work_key == key,
  ).first()
  if row is None:
    raise ValueError("No work claim exists for this key.")
  if row.owner_chat_id != chat_id:
    raise ValueError("Only the current claim owner can finish or release it.")
  if row.completed_at is None and row.released_at is None:
    now = now_naive_utc()
    previous_revision = row.revision
    values = {
      models.AgentWorkClaim.outcome: clean_outcome,
      models.AgentWorkClaim.updated_at: now,
      models.AgentWorkClaim.revision: previous_revision + 1,
      (
        models.AgentWorkClaim.released_at if release
        else models.AgentWorkClaim.completed_at
      ): now,
    }
    changed = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.id == row.id,
      models.AgentWorkClaim.owner_chat_id == chat_id,
      models.AgentWorkClaim.revision == previous_revision,
      models.AgentWorkClaim.completed_at.is_(None),
      models.AgentWorkClaim.released_at.is_(None),
    ).update(values, synchronize_session=False)
    if changed != 1:
      db.rollback()
      raise ValueError("The claim changed while finishing; inspect it before retrying.")
    db.expire(row)
    db.refresh(row)
  interests = db.query(models.AgentWorkInterest).join(
    models.Chat, models.Chat.id == models.AgentWorkInterest.chat_id,
  ).filter(
    models.AgentWorkInterest.claim_id == row.id,
    models.AgentWorkInterest.resolved_at.is_(None),
    models.Chat.deleted_at.is_(None),
  ).all()
  recipients = []
  for interest in interests:
    if interest.chat_id != chat_id:
      recipients.append(interest.chat_id)
  db.commit()
  db.refresh(row)
  state = "completed" if row.completed_at is not None else "released"
  return FinishedClaim(
    claim=_serialize(db, row, state),
    interested_chat_ids=list(dict.fromkeys(recipients)),
  )


def acknowledge_notice(
  db: Session, *, claim_id: str, revision: int, resolve_interests: bool,
) -> None:
  row = db.get(models.AgentWorkClaim, claim_id)
  if row is None or row.revision != revision:
    return
  if row.notification_revision < revision:
    row.notification_revision = revision
  if resolve_interests:
    db.query(models.AgentWorkInterest).filter(
      models.AgentWorkInterest.claim_id == claim_id,
      models.AgentWorkInterest.resolved_at.is_(None),
    ).update({models.AgentWorkInterest.resolved_at: now_naive_utc()})
  db.commit()
