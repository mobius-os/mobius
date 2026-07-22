"""Durable per-app unread activity derived from app-attributed notifications."""

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


def mark_from_notification(
  db: Session, *, source_type: str, source_id: str | None,
) -> int | None:
  """Mark a live app unread inside the caller's notification transaction.

  Returns the app id when a durable marker was written. The update-first,
  savepoint-backed insert is race-safe when an existing app receives its first
  two notifications concurrently, without committing the caller's transaction.
  """
  if source_type != "app" or source_id is None:
    return None
  try:
    app_id = int(source_id)
  except (TypeError, ValueError):
    return None
  app = db.get(models.App, app_id)
  if app is None or app.deleted_at is not None:
    return None

  now = now_naive_utc()
  result = db.execute(
    update(models.AppActivityState)
    .where(models.AppActivityState.app_id == app_id)
    .values(
      activity_at=now,
      activity_version=models.AppActivityState.activity_version + 1,
      unseen=True,
    )
  )
  if result.rowcount:
    return app_id

  try:
    with db.begin_nested():
      db.add(models.AppActivityState(
        app_id=app_id, activity_at=now, unseen=True,
      ))
      db.flush()
  except IntegrityError:
    # Another first notification inserted the singleton row after our UPDATE.
    # The savepoint kept the outer Notification insert intact; make this event
    # the winning latest marker.
    db.execute(
      update(models.AppActivityState)
      .where(models.AppActivityState.app_id == app_id)
      .values(
        activity_at=now,
        activity_version=models.AppActivityState.activity_version + 1,
        unseen=True,
      )
    )
  return app_id


def mark_seen(db: Session, app_id: int, seen_through_version: int) -> None:
  """Acknowledge only activity the opening shell actually observed.

  A newer notification can race the acknowledgement request. Bounding the
  update by its observed monotonic version keeps that newer event unread instead of
  letting a late acknowledgement erase it.
  """
  db.execute(
    update(models.AppActivityState)
    .where(
      models.AppActivityState.app_id == app_id,
      models.AppActivityState.activity_version <= seen_through_version,
    )
    .values(unseen=False)
  )


def annotate_apps(db: Session, apps: list[models.App]) -> list[models.App]:
  """Attach the response-only ``has_unseen_activity`` flag to app rows."""
  ids = [app.id for app in apps]
  unseen_by_id = {}
  if ids:
    unseen_by_id = {
      row.app_id: row.activity_version
      for row in db.query(
        models.AppActivityState.app_id,
        models.AppActivityState.activity_version,
      ).filter(
        models.AppActivityState.app_id.in_(ids),
        models.AppActivityState.unseen.is_(True),
      ).all()
    }
  for app in apps:
    app.has_unseen_activity = app.id in unseen_by_id
    app.unseen_activity_version = unseen_by_id.get(app.id)
  return apps
