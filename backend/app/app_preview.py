"""Durable per-build acknowledgement for an app's owning-chat open button."""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models


def naive_utc(value: datetime) -> datetime:
  """Normalize an API datetime to the naive-UTC shape SQLite returns."""
  if value.tzinfo is not None:
    return value.astimezone(UTC).replace(tzinfo=None)
  return value


def _advance_existing(
  db: Session, app_id: int, seen_updated_at: datetime, seen_as_final: bool,
) -> bool:
  """Advance one row without letting an older acknowledgement move it back."""
  advanced = db.execute(
    update(models.AppPreviewState)
    .where(
      models.AppPreviewState.app_id == app_id,
      models.AppPreviewState.seen_updated_at < seen_updated_at,
    )
    .values(
      seen_updated_at=seen_updated_at,
      seen_as_final=seen_as_final,
    )
  )
  if advanced.rowcount:
    return True
  if seen_as_final:
    promoted = db.execute(
      update(models.AppPreviewState)
      .where(
        models.AppPreviewState.app_id == app_id,
        models.AppPreviewState.seen_updated_at == seen_updated_at,
        models.AppPreviewState.seen_as_final.is_(False),
      )
      .values(seen_as_final=True)
    )
    if promoted.rowcount:
      return True
  return db.get(models.AppPreviewState, app_id) is not None


def mark_seen(
  db: Session,
  app_id: int,
  seen_updated_at: datetime,
  *,
  seen_as_final: bool,
) -> None:
  """Acknowledge only the build the opening shell actually observed.

  An older request may arrive after a newer build was opened on another device.
  The monotonic timestamp update keeps that late request from hiding or
  downgrading the newer acknowledgement.
  """
  seen_updated_at = naive_utc(seen_updated_at)
  if _advance_existing(db, app_id, seen_updated_at, seen_as_final):
    return
  try:
    with db.begin_nested():
      db.add(models.AppPreviewState(
        app_id=app_id,
        seen_updated_at=seen_updated_at,
        seen_as_final=seen_as_final,
      ))
      db.flush()
  except IntegrityError:
    # Two devices acknowledged the first visible build concurrently. The
    # savepoint preserves the outer request; replay the monotonic update.
    _advance_existing(db, app_id, seen_updated_at, seen_as_final)


def annotate_apps(db: Session, apps: list[models.App]) -> list[models.App]:
  """Attach response-only preview acknowledgement fields to app rows."""
  ids = [app.id for app in apps]
  state_by_id = {}
  if ids:
    state_by_id = {
      row.app_id: row
      for row in db.query(models.AppPreviewState).filter(
        models.AppPreviewState.app_id.in_(ids)
      ).all()
    }
  for app in apps:
    state = state_by_id.get(app.id)
    app.preview_seen_updated_at = state.seen_updated_at if state else None
    app.preview_seen_final = bool(state and state.seen_as_final)
  return apps
