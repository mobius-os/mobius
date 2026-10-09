"""App-reported unread counts shown as a pill on the app's sidebar row.

The app owns the number; the platform stores and displays it. Reports carry an
optional ``revision``: the app's own state revision, captured together with the
count. A report whose revision is not newer than the stored one is stale and
ignored, so concurrent reports that land out of order cannot regress the
count. An unrevisioned report always applies and resets the ordering; an app
whose revisions restart lower (for example after restoring its data from a
backup) sends one to recover. Wiping the app's data forgets its badge.

Every mutation runs under ``fs_locks.app_storage_lock(app_id)`` — the boundary
that data wipes and uninstalls already hold — after the caller rechecks the
installation identity there. Within that lock the read, the stale check, the
change decision, and the write form one serialized transition.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import models

# SQLite INTEGER is a signed 64-bit value; counts and revisions use its range.
# The pill itself shows "99+" past 99 (frontend appBadge.js).
MAX_BADGE_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class BadgeReport:
  """The stored badge after a report, and what the report did."""

  count: int
  revision: int | None
  applied: bool
  changed: bool


def apply_report(
  db: Session, app_id: int, count: int, revision: int | None,
) -> BadgeReport:
  """Apply one report; the caller holds the app's storage lock and commits."""
  row = (
    db.query(models.AppBadgeState)
    .populate_existing()
    .filter(models.AppBadgeState.app_id == app_id)
    .first()
  )
  if (
    row is not None and revision is not None and row.revision is not None
    and revision <= row.revision
  ):
    return BadgeReport(row.count, row.revision, applied=False, changed=False)
  previous = row.count if row is not None else 0
  if row is None:
    row = models.AppBadgeState(app_id=app_id)
    db.add(row)
  row.count = count
  row.revision = revision
  return BadgeReport(count, revision, applied=True, changed=previous != count)


def clear(db: Session, app_id: int) -> None:
  """Forget the app's badge, e.g. when its data is wiped or it is removed."""
  db.query(models.AppBadgeState).filter(
    models.AppBadgeState.app_id == app_id,
  ).delete(synchronize_session=False)


def annotate_apps(db: Session, apps: list[models.App]) -> list[models.App]:
  """Attach the response-only ``badge_count`` to app rows."""
  ids = [app.id for app in apps]
  counts = {}
  if ids:
    counts = dict(
      db.query(models.AppBadgeState.app_id, models.AppBadgeState.count)
      .filter(models.AppBadgeState.app_id.in_(ids))
      .all()
    )
  for app in apps:
    app.badge_count = counts.get(app.id, 0)
  return apps
