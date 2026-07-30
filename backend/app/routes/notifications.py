"""Notification send and history endpoints."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import (
  Principal,
  get_current_owner,
  get_principal,
  reject_cross_site,
)
from app.push import notify_owner
from app.schemas import NotificationOut, NotificationSendRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

limiter = Limiter(key_func=get_remote_address)


@router.post("/send", dependencies=[Depends(reject_cross_site)])
@limiter.limit("10/minute")
def send_notification(
  request: Request,
  body: NotificationSendRequest,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Send a push notification to all owner subscriptions."""
  actions_list = (
    [a.model_dump() for a in body.actions] if body.actions else None
  )
  # An app-scoped caller can't spoof the notification's source: force it to be
  # attributed to the app itself, so a mini-app can't masquerade as the system
  # or another app in a push (a phishing vector). Owner tokens keep full control.
  if principal.app_id is not None:
    source_type, source_id = "app", str(principal.app_id)
  else:
    source_type, source_id = body.source_type, body.source_id
  notification_id = notify_owner(
    db,
    principal.owner.id,
    title=body.title,
    body=body.body,
    source_type=source_type,
    source_id=source_id,
    icon=body.icon,
    target=body.target,
    actions=actions_list,
  )
  return {"id": notification_id}


@router.get("/unread-count")
def unread_count(
  # Owner-only, matching the list endpoint: the bell badge is the owner's
  # surface and app tokens have no need to observe it.
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Count of notifications not yet seen via the notification preview."""
  n = (
    db.query(func.count(models.Notification.id))
    .filter(
      models.Notification.owner_id == owner.id,
      models.Notification.read_at.is_(None),
    )
    .scalar()
  )
  return {"count": int(n or 0)}


@router.post("/read-all", dependencies=[Depends(reject_cross_site)])
def read_all(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Seen-on-open: mark every unread notification read. Idempotent.

  Only rows with read_at NULL are touched, so a notification that commits
  concurrently with this UPDATE simply stays unread and is picked up by the
  next call — no lost-update window.
  """
  updated = (
    db.query(models.Notification)
    .filter(
      models.Notification.owner_id == owner.id,
      models.Notification.read_at.is_(None),
    )
    .update(
      {"read_at": datetime.now(UTC)}, synchronize_session=False,
    )
  )
  db.commit()
  return {"updated": int(updated)}


@router.delete("", dependencies=[Depends(reject_cross_site)])
def clear_notifications(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Delete all stored notifications for the owner. Idempotent."""
  deleted = (
    db.query(models.Notification)
    .filter(models.Notification.owner_id == owner.id)
    .delete(synchronize_session=False)
  )
  db.commit()
  return {"deleted": int(deleted)}


@router.get("")
def list_notifications(
  # Owner-only: the notification history is the owner's. App tokens have no
  # need to read it and previously could enumerate the full history.
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
  limit: int = Query(20, ge=1, le=100),
  before: str | None = Query(None),
):
  """Return notification history, paginated."""
  q = (
    db.query(models.Notification)
    .filter(models.Notification.owner_id == owner.id)
    .order_by(
      models.Notification.sent_at.desc(),
      models.Notification.id.desc(),
    )
  )
  if before:
    ref = (
      db.query(models.Notification)
      .filter(
        models.Notification.owner_id == owner.id,
        models.Notification.id == before,
      )
      .one_or_none()
    )
    if ref is None:
      raise HTTPException(status_code=400, detail="Invalid notification cursor.")
    q = q.filter(or_(
      models.Notification.sent_at < ref.sent_at,
      and_(
        models.Notification.sent_at == ref.sent_at,
        models.Notification.id < ref.id,
      ),
    ))
  return [
    NotificationOut.model_validate(n) for n in q.limit(limit).all()
  ]
