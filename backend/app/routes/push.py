"""Push subscription management endpoints."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import get_current_owner_or_app
from app.push import get_public_key_base64url
from app.schemas import PushSubscribeRequest, PushUnsubscribeRequest

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid-key")
def vapid_key():
  """Return the VAPID public key for browser push subscription."""
  return {"publicKey": get_public_key_base64url()}


@router.post("/subscribe", status_code=201)
def subscribe(
  body: PushSubscribeRequest,
  owner: models.Owner = Depends(get_current_owner_or_app),
  db: Session = Depends(get_db),
):
  """Register or update a push subscription."""
  existing = (
    db.query(models.PushSubscription)
    .filter(models.PushSubscription.endpoint == body.endpoint)
    .first()
  )
  if existing:
    existing.p256dh = body.keys.p256dh
    existing.auth = body.keys.auth
  else:
    sub = models.PushSubscription(
      id=str(uuid.uuid4()),
      owner_id=owner.id,
      endpoint=body.endpoint,
      p256dh=body.keys.p256dh,
      auth=body.keys.auth,
      created_at=datetime.now(UTC),
    )
    db.add(sub)
  db.commit()
  return {"status": "subscribed"}


@router.delete("/subscribe", status_code=204)
def unsubscribe(
  body: PushUnsubscribeRequest,
  _owner: models.Owner = Depends(get_current_owner_or_app),
  db: Session = Depends(get_db),
):
  """Remove a push subscription."""
  db.query(models.PushSubscription).filter(
    models.PushSubscription.endpoint == body.endpoint
  ).delete()
  db.commit()
