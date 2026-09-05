"""Push subscription management endpoints."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import (
  Principal, get_principal, reject_cross_site,
  require_nondelegated_owner_control,
)
from app.push import get_public_key_base64url
from app.schemas import PushSubscribeRequest, PushUnsubscribeRequest

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid-key")
def vapid_key():
  """Return the VAPID public key for browser push subscription."""
  return {"publicKey": get_public_key_base64url()}


@router.post(
  "/subscribe", status_code=201, dependencies=[Depends(reject_cross_site)]
)
def subscribe(
  body: PushSubscribeRequest,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Register or update a push subscription."""
  require_nondelegated_owner_control(principal)
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
      owner_id=principal.owner.id,
      endpoint=body.endpoint,
      p256dh=body.keys.p256dh,
      auth=body.keys.auth,
      created_at=datetime.now(UTC),
    )
    db.add(sub)
  db.commit()
  return {"status": "subscribed"}


@router.delete(
  "/subscribe", status_code=204, dependencies=[Depends(reject_cross_site)]
)
def unsubscribe(
  body: PushUnsubscribeRequest,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Remove a push subscription."""
  require_nondelegated_owner_control(principal)
  db.query(models.PushSubscription).filter(
    models.PushSubscription.endpoint == body.endpoint
  ).delete()
  db.commit()
