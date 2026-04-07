"""Notification send and history endpoints."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import get_current_owner_or_app
from app.broadcast import get_broadcast
from app.push import send_push
from app.schemas import NotificationOut, NotificationSendRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

limiter = Limiter(key_func=get_remote_address)


@router.post("/send")
@limiter.limit("10/minute")
def send_notification(
  request: Request,
  body: NotificationSendRequest,
  owner: models.Owner = Depends(get_current_owner_or_app),
  db: Session = Depends(get_db),
):
  """Send a push notification to all owner subscriptions."""
  notification_id = str(uuid.uuid4())
  actions_list = (
    [a.model_dump() for a in body.actions] if body.actions else None
  )

  # Save to DB.
  notif = models.Notification(
    id=notification_id,
    owner_id=owner.id,
    source_type=body.source_type,
    source_id=body.source_id,
    title=body.title,
    body=body.body,
    icon=body.icon,
    target=body.target,
    actions=actions_list,
    sent_at=datetime.now(UTC),
  )
  db.add(notif)
  db.commit()

  # Build the push payload.
  payload = {
    "id": notification_id,
    "title": body.title,
    "body": body.body,
    "icon": body.icon,
    "target": body.target,
    "actions": actions_list,
  }

  # Skip push if the user is watching this chat right now — the SSE
  # stream already delivers the agent's output in real time.
  bc = get_broadcast(body.source_id) if body.source_id else None
  if bc and bc.subscribers:
    return {"id": notification_id}

  # Deliver to all subscriptions. Remove stale ones.
  subs = (
    db.query(models.PushSubscription)
    .filter(models.PushSubscription.owner_id == owner.id)
    .all()
  )
  stale_ids = []
  for sub in subs:
    sub_info = {
      "endpoint": sub.endpoint,
      "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
    }
    try:
      alive = send_push(sub_info, payload)
      if not alive:
        stale_ids.append(sub.id)
    except Exception:
      logger.exception("push delivery failed for sub %s", sub.id[:8])

  if stale_ids:
    db.query(models.PushSubscription).filter(
      models.PushSubscription.id.in_(stale_ids)
    ).delete(synchronize_session=False)
    db.commit()

  return {"id": notification_id}


@router.get("")
def list_notifications(
  owner: models.Owner = Depends(get_current_owner_or_app),
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
    ref = db.query(models.Notification).get(before)
    if ref:
      q = q.filter(models.Notification.sent_at < ref.sent_at)
  return [
    NotificationOut.model_validate(n) for n in q.limit(limit).all()
  ]
