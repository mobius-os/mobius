"""Durable per-app open recency without mutating executable bundle versions."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


def _advance_existing(
  db: Session, app_id: int, opened_at: datetime,
) -> bool:
  """Advance one row monotonically; report whether the singleton exists."""
  advanced = db.execute(
    update(models.AppRecencyState)
    .where(
      models.AppRecencyState.app_id == app_id,
      models.AppRecencyState.last_opened_at < opened_at,
    )
    .values(last_opened_at=opened_at)
  )
  if advanced.rowcount:
    return True
  return db.get(models.AppRecencyState, app_id) is not None


def mark_opened(db: Session, app_id: int) -> None:
  """Record an app open inside the caller's transaction.

  Update-first plus a savepoint-backed insert keeps simultaneous first opens
  from two devices race-safe without committing the caller's session.
  """
  opened_at = now_naive_utc()
  if _advance_existing(db, app_id, opened_at):
    return
  try:
    with db.begin_nested():
      db.add(models.AppRecencyState(
        app_id=app_id,
        last_opened_at=opened_at,
      ))
      db.flush()
  except IntegrityError:
    # Another first open inserted the singleton row after our UPDATE.
    _advance_existing(db, app_id, opened_at)


def annotate_apps(db: Session, apps: list[models.App]) -> list[models.App]:
  """Attach the response-only ``last_opened_at`` field to app rows."""
  ids = [app.id for app in apps]
  opened_by_id = {}
  if ids:
    opened_by_id = {
      row.app_id: row.last_opened_at
      for row in db.query(
        models.AppRecencyState.app_id,
        models.AppRecencyState.last_opened_at,
      ).filter(models.AppRecencyState.app_id.in_(ids)).all()
    }
  for app in apps:
    app.last_opened_at = opened_by_id.get(app.id)
  return apps
