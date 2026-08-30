"""Durable chat-to-app artifact touches and Brain acknowledgements."""

from datetime import UTC, datetime

from sqlalchemy import or_, update
from sqlalchemy.orm import Session, defer

from app import models


def _naive_utc(value: datetime) -> datetime:
  """Match SQLite's persisted DateTime shape without losing instant order."""
  if value.tzinfo is not None:
    return value.astimezone(UTC).replace(tzinfo=None)
  return value


def record_touch(
  db: Session,
  *,
  chat_id: str,
  app_id: int,
  touched_at: datetime,
) -> None:
  """Advance the app artifact for one successful chat-owned apply."""
  key = (str(chat_id), int(app_id))
  touched_at = _naive_utc(touched_at)
  row = db.get(models.ChatAppArtifact, key)
  if row is None:
    db.add(models.ChatAppArtifact(
      chat_id=key[0],
      app_id=key[1],
      touched_at=touched_at,
    ))
    return
  if _naive_utc(row.touched_at) < touched_at:
    row.touched_at = touched_at


def mark_chat_touches_seen(
  db: Session,
  *,
  chat_id: str,
  touches: list[tuple[int, datetime]],
) -> None:
  """Acknowledge only the exact app touches shown when the Brain opens.

  Each cursor is matched against the current ``touched_at`` value so an update
  arriving while the acknowledgement request is in flight remains unread.
  """
  latest_by_app = {
    int(app_id): _naive_utc(touched_at)
    for app_id, touched_at in touches
  }
  for app_id, touched_at in latest_by_app.items():
    db.execute(
      update(models.ChatAppArtifact)
      .where(
        models.ChatAppArtifact.chat_id == str(chat_id),
        models.ChatAppArtifact.app_id == app_id,
        models.ChatAppArtifact.touched_at == touched_at,
        or_(
          models.ChatAppArtifact.seen_at.is_(None),
          models.ChatAppArtifact.seen_at < touched_at,
        ),
      )
      .values(seen_at=touched_at)
    )


def list_for_chat(db: Session, chat_id: str) -> list[tuple]:
  """Return live apps touched by this chat, newest touch first."""
  return (
    db.query(models.ChatAppArtifact, models.App)
    .join(models.App, models.App.id == models.ChatAppArtifact.app_id)
    .options(
      defer(models.App.jsx_source),
      defer(models.App.icon_png),
      defer(models.App.icon_override_png),
    )
    .filter(
      models.ChatAppArtifact.chat_id == str(chat_id),
      models.App.deleted_at.is_(None),
    )
    .order_by(models.ChatAppArtifact.touched_at.desc())
    .all()
  )
