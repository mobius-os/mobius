"""Push subscription (Web Push) and native mobile device endpoints."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, push_relay
from app.database import SessionLocal, get_db
from app.deps import get_current_owner_for_lifecycle_control, reject_cross_site
from app.push import get_public_key_base64url
from app.schemas import (
  PushDeviceRequest,
  PushSubscribeRequest,
  PushUnsubscribeRequest,
)
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/push", tags=["push"])
# Enough to tell phones apart in Settings without exposing a usable token.
_TOKEN_TAIL = 6


@router.get("/vapid-key")
def vapid_key():
  """Return the VAPID public key for browser push subscription."""
  return {"publicKey": get_public_key_base64url()}


@router.post(
  "/subscribe", status_code=201, dependencies=[Depends(reject_cross_site)]
)
def subscribe(
  body: PushSubscribeRequest,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Register or update a push subscription.

  A subscription receives every owner notification, so apps and delegated
  agents cannot register or remove one.
  """
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


@router.delete(
  "/subscribe", status_code=204, dependencies=[Depends(reject_cross_site)]
)
def unsubscribe(
  body: PushUnsubscribeRequest,
  _: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Remove a push subscription."""
  db.query(models.PushSubscription).filter(
    models.PushSubscription.endpoint == body.endpoint
  ).delete()
  db.commit()


# Native push devices: the shell inside the Möbius mobile app registers the
# token that ``MobiusNative.registerPush()`` returned, on every load. Like a Web
# Push subscription, a device receives every owner notification, so only the
# owner may manage one. Tokens travel in request bodies, never in URLs, to stay
# out of logs.


@router.put("/devices", dependencies=[Depends(reject_cross_site)])
def register_device(
  body: PushDeviceRequest,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Register this phone; a token another owner had moves to the caller.

  The relay learns that this installation owns the token before it is
  stored, so a device row always means the relay will accept sends to it.
  Re-registering on every load also re-claims a phone that was bound to
  another installation in between.
  """
  try:
    push_relay.bind_device(body.token)
  except push_relay.RelayError as exc:
    if exc.not_linked:
      raise HTTPException(
        409, "Link this Möbius to your mobius.you account to get phone notifications.",
      )
    raise HTTPException(502, "The notification service could not be reached.")
  device = db.query(models.PushDevice).filter(
    models.PushDevice.token == body.token,
  ).one_or_none()
  if device is None:
    device = models.PushDevice(id=str(uuid.uuid4()), token=body.token)
    db.add(device)
  device.owner_id = owner.id
  device.registered_at = now_naive_utc()
  db.commit()
  return {"status": "registered"}


@router.get("/devices")
def list_devices(
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """The owner's registered phones for Settings; never the full token."""
  devices = db.query(models.PushDevice).filter(
    models.PushDevice.owner_id == owner.id,
  ).order_by(models.PushDevice.registered_at.desc()).all()
  return [
    {
      "id": device.id,
      "tokenTail": device.token[-_TOKEN_TAIL:],
      "registeredAt": device.registered_at.replace(tzinfo=UTC).isoformat(),
    }
    for device in devices
  ]


def _remove(
  db: Session, background: BackgroundTasks, owner_id: int, *criteria,
) -> None:
  observed = db.query(
    models.PushDevice.id, models.PushDevice.token, models.PushDevice.registered_at,
  ).filter(models.PushDevice.owner_id == owner_id, *criteria).all()
  removed = _remove_observed(db, owner_id, observed)
  if removed:
    # The rows are gone, so nothing is sent to these phones any more. The
    # relay unbind is best effort and can take a while; it must not hold up
    # the response (sign-out waits for it).
    background.add_task(_unbind_removed, removed)


def _remove_observed(db: Session, owner_id: int, observed) -> list[str]:
  """Delete observed phones that are unchanged; returns their tokens.

  A phone that registered again (or moved to another owner) since it was
  observed keeps its row.
  """
  removed = []
  for device_id, token, registered_at in observed:
    deleted = db.query(models.PushDevice).filter(
      models.PushDevice.id == device_id,
      models.PushDevice.owner_id == owner_id,
      models.PushDevice.registered_at == registered_at,
    ).delete(synchronize_session=False)
    if deleted:
      removed.append(token)
  db.commit()
  return removed


def _unbind_removed(tokens: list[str]) -> None:
  """Unbind removed phones at the relay, unless one registered again meanwhile.

  A registration that lands between the deletion and this unbind has stored
  its row; the unbind would leave that row unbound, so it is bound again.
  """
  for token in tokens:
    try:
      push_relay.unbind_device(token)
    except push_relay.RelayError:
      # Nothing is sent to a removed device; the relay keeps only a binding.
      continue
    with SessionLocal() as db:
      registered_again = db.query(models.PushDevice.id).filter(
        models.PushDevice.token == token,
      ).first()
    if registered_again:
      try:
        push_relay.bind_device(token)
      except push_relay.RelayError:
        # Its next send reports invalid_token; the phone re-registers on open.
        pass


@router.delete(
  "/devices", status_code=204, dependencies=[Depends(reject_cross_site)],
)
def forget_device(
  body: PushDeviceRequest,
  background: BackgroundTasks,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Stop sending to this phone (sign-out); repeating it is harmless."""
  _remove(db, background, owner.id, models.PushDevice.token == body.token)


@router.delete(
  "/devices/{device_id}", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def remove_device(
  device_id: str,
  background: BackgroundTasks,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Remove one of the owner's phones (API; Settings switches phones off from the app)."""
  _remove(db, background, owner.id, models.PushDevice.id == device_id)
