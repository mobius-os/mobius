"""Push subscription (Web Push) and native mobile device endpoints."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, notification_channels, push_relay
from app.database import SessionLocal, get_db
from app.deps import get_current_owner_for_lifecycle_control, reject_cross_site
from app.push import tap_url, get_public_key_base64url
from app.schemas import (
  PushDeviceRequest,
  PushSubscribeRequest,
  PushUnsubscribeRequest,
)
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/push", tags=["push"])
# Enough to tell phones apart in Settings without exposing a usable token.
_TOKEN_TAIL = 6
_MAX_CHANNELS = 10


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
  # Two registrations of the same new token can race to insert it; the one
  # that loses the unique constraint updates the winner's row instead.
  for attempt in range(2):
    device = _find_device(db, body.token)
    if device is None:
      device = models.PushDevice(id=str(uuid.uuid4()), token=body.token)
      db.add(device)
    device.owner_id = owner.id
    device.registered_at = now_naive_utc()
    try:
      db.commit()
      break
    except IntegrityError:
      db.rollback()
      if attempt:
        raise
  return {"status": "registered"}


def _find_device(db: Session, token: str) -> models.PushDevice | None:
  return db.query(models.PushDevice).filter(
    models.PushDevice.token == token,
  ).one_or_none()


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
  """Disconnect one of the owner's phones (API; there is no Settings UI, the phone's own notification setting decides)."""
  _remove(db, background, owner.id, models.PushDevice.id == device_id)


# Outside channels (Slack, Discord, ntfy, any webhook). A channel receives
# owner notifications, so like a subscription only the owner may manage one.
# The link is a credential: it goes in once and is never read back.


class ChannelCreate(BaseModel):
  url: str = Field(min_length=8, max_length=2000)
  kind: Literal["slack", "discord", "ntfy", "webhook"] | None = None
  label: str | None = Field(default=None, max_length=80)
  # None: every notification. A list: only notifications from those apps.
  app_ids: list[int] | None = Field(default=None, max_length=200)
  include_body: bool = False


class ChannelUpdate(BaseModel):
  # A replacement link; like on add, it is checked, encrypted and never returned.
  url: str | None = Field(default=None, min_length=8, max_length=2000)
  label: str | None = Field(default=None, max_length=80)
  app_ids: list[int] | None = Field(default=None, max_length=200)
  all_notifications: bool | None = None
  include_body: bool | None = None


def _channel_out(row: models.NotificationChannel) -> dict:
  def iso(value):
    return value.replace(tzinfo=UTC).isoformat() if value else None
  return {
    "id": row.id,
    "kind": row.kind,
    "label": row.label,
    "urlHint": row.url_hint,
    "appIds": row.app_ids,
    "includeBody": bool(row.include_body),
    "createdAt": iso(row.created_at),
    "lastSentAt": iso(row.last_sent_at),
    "lastError": row.last_error,
  }


def _owned_channel(db: Session, owner_id: int, channel_id: str) -> models.NotificationChannel:
  row = db.query(models.NotificationChannel).filter(
    models.NotificationChannel.id == channel_id,
    models.NotificationChannel.owner_id == owner_id,
  ).one_or_none()
  if row is None:
    raise HTTPException(404, "Channel not found.")
  return row


@router.get("/channels")
def list_channels(
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  rows = db.query(models.NotificationChannel).filter(
    models.NotificationChannel.owner_id == owner.id,
  ).order_by(models.NotificationChannel.created_at).all()
  return [_channel_out(row) for row in rows]


@router.post("/channels", status_code=201, dependencies=[Depends(reject_cross_site)])
def create_channel(
  body: ChannelCreate,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Add a channel; the link must be a public https address."""
  kind = body.kind or notification_channels.detect_kind(body.url.strip())
  url = notification_channels.check_url(body.url, kind)
  if db.query(models.NotificationChannel).filter(
    models.NotificationChannel.owner_id == owner.id,
  ).count() >= _MAX_CHANNELS:
    raise HTTPException(409, f"Up to {_MAX_CHANNELS} channels can be added.")
  row = models.NotificationChannel(
    id=str(uuid.uuid4()),
    owner_id=owner.id,
    kind=kind,
    label=(body.label or "").strip() or notification_channels.KIND_NAMES[kind],
    url_encrypted=notification_channels.encrypt_url(url),
    url_hint=notification_channels.url_hint(url),
    app_ids=sorted(set(body.app_ids)) if body.app_ids is not None else None,
    include_body=body.include_body,
  )
  db.add(row)
  db.commit()
  return _channel_out(row)


@router.patch("/channels/{channel_id}", dependencies=[Depends(reject_cross_site)])
def update_channel(
  channel_id: str,
  body: ChannelUpdate,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  row = _owned_channel(db, owner.id, channel_id)
  if body.url is not None:
    kind = notification_channels.detect_kind(body.url.strip())
    url = notification_channels.check_url(body.url, kind)
    if row.label == notification_channels.KIND_NAMES[row.kind]:
      row.label = notification_channels.KIND_NAMES[kind]
    row.kind = kind
    row.url_encrypted = notification_channels.encrypt_url(url)
    row.url_hint = notification_channels.url_hint(url)
    # The old link's failure says nothing about the new one.
    row.last_error = None
  if body.label is not None:
    row.label = body.label.strip() or notification_channels.KIND_NAMES[row.kind]
  if body.all_notifications:
    row.app_ids = None
  elif body.app_ids is not None:
    row.app_ids = sorted(set(body.app_ids))
  if body.include_body is not None:
    row.include_body = body.include_body
  db.commit()
  return _channel_out(row)


@router.delete(
  "/channels/{channel_id}", status_code=204, dependencies=[Depends(reject_cross_site)],
)
def delete_channel(
  channel_id: str,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  db.delete(_owned_channel(db, owner.id, channel_id))
  db.commit()


@router.post("/channels/{channel_id}/test", dependencies=[Depends(reject_cross_site)])
def test_channel(
  channel_id: str,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Send a sample message now and report how it went."""
  row = _owned_channel(db, owner.id, channel_id)
  target = notification_channels.Target(
    id=row.id, kind=row.kind, url_encrypted=row.url_encrypted,
    include_body=True,
  )
  db.close()
  message = notification_channels.Message(
    title="Möbius notifications are connected",
    body="Notifications you choose in Settings will appear here.",
    url=tap_url("/shell/?settings=notifications-elsewhere"),
    notification_id=f"test-{uuid.uuid4()}", source_type="system", source_id=None,
  )
  error = notification_channels.deliver(target, message)
  notification_channels.record_outcome(channel_id, error)
  return {"ok": error is None, "error": error}
