"""Notification send and history endpoints."""

import logging

from fastapi import APIRouter, Depends, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
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
    .order_by(models.Notification.sent_at.desc())
  )
  if before:
    ref = db.get(models.Notification, before)
    if ref:
      q = q.filter(models.Notification.sent_at < ref.sent_at)
  return [
    NotificationOut.model_validate(n) for n in q.limit(limit).all()
  ]
