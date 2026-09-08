"""Durable, race-safe drawer attention for genuine chat-run failures."""

from sqlalchemy import update
from sqlalchemy.orm import Session

from app import models


def mark_failed(
  db: Session,
  *,
  chat_id: str,
  run_id: str,
  failed_at,
) -> None:
  """Mark one newly terminal failure inside the caller's transaction.

  Chat-run terminal transitions are serialized by ``chat_writer``, so the
  singleton row can use an ordinary read/update without adding another lock.
  A repeated close for the same exact run is idempotent.
  """
  state = db.get(models.ChatFailureActivity, chat_id)
  if state is None:
    db.add(models.ChatFailureActivity(
      chat_id=chat_id,
      run_id=run_id,
      failed_at=failed_at,
      unseen=True,
    ))
    return
  if state.run_id == run_id:
    return
  state.run_id = run_id
  state.failed_at = failed_at
  state.activity_version = int(state.activity_version or 0) + 1
  state.unseen = True


def mark_seen(db: Session, chat_id: str, seen_through_version: int) -> None:
  """Acknowledge only the failure version the opening shell observed."""
  db.execute(
    update(models.ChatFailureActivity)
    .where(
      models.ChatFailureActivity.chat_id == chat_id,
      models.ChatFailureActivity.activity_version <= seen_through_version,
    )
    .values(unseen=False)
  )


def unseen_versions(db: Session, chat_ids) -> dict[str, int]:
  """Return the compact unread-failure projection for owner chat rows."""
  ids = [str(chat_id) for chat_id in chat_ids]
  if not ids:
    return {}
  return {
    row.chat_id: row.activity_version
    for row in db.query(
      models.ChatFailureActivity.chat_id,
      models.ChatFailureActivity.activity_version,
    ).filter(
      models.ChatFailureActivity.chat_id.in_(ids),
      models.ChatFailureActivity.unseen.is_(True),
    ).all()
  }
