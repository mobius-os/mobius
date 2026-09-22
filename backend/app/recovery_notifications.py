"""Transaction-bound notification receipts for reversible deletion."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.broadcast import get_system_broadcast
from app.schemas import NotificationAction
from app.timeutil import now_naive_utc


RecoveryResource = Literal["chat", "app", "project"]
logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
  return (
    value.replace(tzinfo=UTC)
    if value.tzinfo is None
    else value.astimezone(UTC)
  )


def stage_recovery_notification(
  db: Session,
  *,
  owner_id: int,
  resource_type: RecoveryResource,
  resource_id: str,
  resource_generation: datetime,
  deleted_at: datetime,
  expires_at: datetime,
  resource_name: str,
) -> str:
  """Stage the Undo receipt in the caller's deletion transaction."""
  notification_id = str(uuid.uuid4())
  db.add(models.Notification(
    id=notification_id,
    owner_id=owner_id,
    source_type="shell",
    source_id=None,
    title=f"{resource_type.capitalize()} deleted",
    body=resource_name,
    actions=[{
      "action": f"recover_{resource_type}",
      "title": "Undo",
      "resource_type": resource_type,
      "resource_id": resource_id,
      "resource_generation": _as_utc(resource_generation).isoformat(),
      "deleted_at": _as_utc(deleted_at).isoformat(),
      "expires_at": _as_utc(expires_at).isoformat(),
    }],
    sent_at=datetime.now(UTC),
  ))
  return notification_id


def publish_recovery_notification(notification_id: str) -> None:
  """Wake live shells after commit; reconnect also refetches the durable row."""
  try:
    get_system_broadcast().publish({
      "type": "notification_created",
      "notificationId": notification_id,
    })
  except Exception:
    # The row is already durable. A delivery failure must not turn a committed
    # deletion into an ambiguous HTTP failure; reconnect will refetch history.
    logger.exception(
      "Recovery notification %s committed but could not be broadcast",
      notification_id,
    )


def _find_recovery_action(
  db: Session,
  *,
  owner_id: int,
  notification_id: str,
  resource_type: RecoveryResource,
  resource_id: str,
  resource_generation: datetime,
) -> tuple[models.Notification, int, NotificationAction]:
  notification = db.query(models.Notification).filter(
    models.Notification.id == notification_id,
    models.Notification.owner_id == owner_id,
  ).populate_existing().one_or_none()
  if notification is None:
    raise HTTPException(status_code=404, detail="Recovery receipt not found.")

  expected_action = f"recover_{resource_type}"
  for index, raw in enumerate(notification.actions or []):
    try:
      action = NotificationAction.model_validate(raw)
    except ValueError:
      continue
    if (
      action.action == expected_action
      and action.resource_type == resource_type
      and action.resource_id == resource_id
    ):
      expected_generation = _as_utc(resource_generation)
      if action.resource_generation.astimezone(UTC) != expected_generation:
        raise HTTPException(409, detail={
          "code": "recovery_superseded",
          "message": "This Undo belongs to an earlier item with the same identity.",
        })
      return notification, index, action
  raise HTTPException(status_code=404, detail="Recovery receipt not found.")


def validate_recovery_action(
  db: Session,
  *,
  owner_id: int,
  notification_id: str,
  resource_type: RecoveryResource,
  resource_id: str,
  resource_generation: datetime,
  deleted_at: datetime | None,
) -> datetime | None:
  """Validate a receipt against the current tombstone under its lifecycle lock.

  A resource id can be deleted, purged, and reused. The row generation rejects
  that successor before completion retries can run any post-commit cleanup;
  the tombstone timestamp then selects one deletion of the matching row.
  """
  _, _, action = _find_recovery_action(
    db,
    owner_id=owner_id,
    notification_id=notification_id,
    resource_type=resource_type,
    resource_id=resource_id,
    resource_generation=resource_generation,
  )
  receipt_deleted_at = action.deleted_at.astimezone(UTC).replace(tzinfo=None)
  if deleted_at is not None and receipt_deleted_at != deleted_at:
    raise HTTPException(409, detail={
      "code": "recovery_superseded",
      "message": "This Undo belongs to an earlier deletion. Use the latest receipt.",
    })
  if action.completed_at is not None:
    return action.completed_at
  if now_naive_utc() >= action.expires_at.astimezone(UTC).replace(tzinfo=None):
    raise HTTPException(410, detail="Recovery window has expired.")
  if deleted_at is None:
    raise HTTPException(409, detail={
      "code": "recovery_already_restored",
      "message": "This item has already been restored.",
    })
  return None


def complete_recovery_action(
  db: Session,
  *,
  owner_id: int,
  notification_id: str,
  resource_type: RecoveryResource,
  resource_id: str,
  resource_generation: datetime,
) -> datetime:
  """Stage completion of the exact receipt in the restore transaction."""
  notification, index, action = _find_recovery_action(
    db,
    owner_id=owner_id,
    notification_id=notification_id,
    resource_type=resource_type,
    resource_id=resource_id,
    resource_generation=resource_generation,
  )
  completed_at = action.completed_at or datetime.now(UTC)
  actions = list(notification.actions or [])
  actions[index] = {
    **action.model_dump(mode="json", exclude_none=True),
    "completed_at": completed_at.isoformat(),
  }
  notification.actions = actions
  flag_modified(notification, "actions")
  return completed_at
