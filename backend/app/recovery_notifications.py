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


RecoveryResource = Literal["chat", "app", "project"]
logger = logging.getLogger(__name__)


def stage_recovery_notification(
  db: Session,
  *,
  owner_id: int,
  resource_type: RecoveryResource,
  resource_id: str,
) -> str:
  """Stage the Undo receipt in the caller's deletion transaction."""
  notification_id = str(uuid.uuid4())
  db.add(models.Notification(
    id=notification_id,
    owner_id=owner_id,
    source_type="shell",
    source_id=None,
    title=f"{resource_type.capitalize()} deleted",
    body="Undo is available here for 7 days.",
    actions=[{
      "action": f"recover_{resource_type}",
      "title": "Undo",
      "resource_type": resource_type,
      "resource_id": resource_id,
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
) -> tuple[models.Notification, int, NotificationAction]:
  notification = db.query(models.Notification).filter(
    models.Notification.id == notification_id,
    models.Notification.owner_id == owner_id,
  ).one_or_none()
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
      return notification, index, action
  raise HTTPException(status_code=404, detail="Recovery receipt not found.")


def recovery_action_completed_at(
  db: Session,
  *,
  owner_id: int,
  notification_id: str,
  resource_type: RecoveryResource,
  resource_id: str,
) -> datetime | None:
  """Read the exact owner-scoped receipt for idempotent recovery retries."""
  _, _, action = _find_recovery_action(
    db,
    owner_id=owner_id,
    notification_id=notification_id,
    resource_type=resource_type,
    resource_id=resource_id,
  )
  return action.completed_at


def complete_recovery_action(
  db: Session,
  *,
  owner_id: int,
  notification_id: str,
  resource_type: RecoveryResource,
  resource_id: str,
) -> datetime:
  """Stage completion of the exact receipt in the restore transaction."""
  notification, index, action = _find_recovery_action(
    db,
    owner_id=owner_id,
    notification_id=notification_id,
    resource_type=resource_type,
    resource_id=resource_id,
  )
  completed_at = action.completed_at or datetime.now(UTC)
  actions = list(notification.actions or [])
  actions[index] = {
    **action.model_dump(exclude_none=True),
    "completed_at": completed_at.isoformat(),
  }
  notification.actions = actions
  flag_modified(notification, "actions")
  return completed_at
