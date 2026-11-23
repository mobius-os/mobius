"""Outside channels: Slack, Discord, ntfy and webhooks Möbius posts to.

A channel can copy owner notifications (``copy_notifications``) and accept
posts sent on purpose (``accept_posts``), such as a release note. The owner,
their top-level agent, and an app holding ``channels_manage`` (the Channels
app) manage them here. The link is a credential: it goes in once and is never
read back.
"""

import uuid
from datetime import UTC
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models, notification_channels
from app.database import get_db
from app.deps import get_owner_or_app_with_channels_manage, reject_cross_site
from app.push import tap_url

router = APIRouter(prefix="/api/channels", tags=["channels"])
_MAX_CHANNELS = 10
_LINK_MAX = 2000


class ChannelCreate(BaseModel):
  url: str = Field(min_length=8, max_length=2000)
  kind: Literal["slack", "discord", "ntfy", "webhook"] | None = None
  label: str | None = Field(default=None, max_length=80)
  copy_notifications: bool = True
  # None: every notification. A list: only notifications from those apps.
  app_ids: list[int] | None = Field(default=None, max_length=200)
  include_body: bool = False
  accept_posts: bool = False


class ChannelUpdate(BaseModel):
  # A replacement link; like on add, it is checked, encrypted and never returned.
  url: str | None = Field(default=None, min_length=8, max_length=2000)
  label: str | None = Field(default=None, max_length=80)
  copy_notifications: bool | None = None
  app_ids: list[int] | None = Field(default=None, max_length=200)
  all_notifications: bool | None = None
  include_body: bool | None = None
  accept_posts: bool | None = None


class ChannelTest(BaseModel):
  link: str | None = Field(default=None, max_length=_LINK_MAX)


class ChannelPost(BaseModel):
  title: str = Field(min_length=1, max_length=200)
  text: str | None = Field(default=None, max_length=notification_channels.POST_TEXT_MAX)
  # A Möbius page ("/shell/?app=12") or any https address.
  link: str | None = Field(default=None, max_length=_LINK_MAX)


def _iso(value):
  return value.replace(tzinfo=UTC).isoformat() if value else None


def _channel_out(row: models.NotificationChannel) -> dict:
  return {
    "id": row.id,
    "kind": row.kind,
    "label": row.label,
    "urlHint": row.url_hint,
    "copyNotifications": bool(row.copy_notifications),
    "appIds": row.app_ids,
    "includeBody": bool(row.include_body),
    "acceptPosts": bool(row.accept_posts),
    "createdAt": _iso(row.created_at),
    "lastSentAt": _iso(row.last_sent_at),
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


def _link(value: str | None) -> tuple[str | None, str]:
  """The address a message links to, and the words for it."""
  value = (value or "").strip()
  if not value:
    return None, ""
  if value.startswith("/") and not value.startswith("//"):
    return tap_url(value), "Open in Möbius"
  parsed = urlparse(value)
  if parsed.scheme != "https" or not parsed.hostname:
    raise HTTPException(400, "A link must be a Möbius page like /shell/?app=12 or an https address.")
  return value, "Open link"


def _message(kind: str, title: str, text: str | None, link: str | None) -> notification_channels.Message:
  url, link_text = _link(link)
  return notification_channels.Message(
    title=title, body=text, url=url, notification_id=f"{kind}-{uuid.uuid4()}",
    source_type="system", source_id=None, kind=kind,
    link_text=link_text or "Open in Möbius",
  )


@router.get("")
def list_channels(
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
  db: Session = Depends(get_db),
):
  rows = db.query(models.NotificationChannel).filter(
    models.NotificationChannel.owner_id == owner.id,
  ).order_by(models.NotificationChannel.created_at).all()
  return [_channel_out(row) for row in rows]


@router.post("", status_code=201, dependencies=[Depends(reject_cross_site)])
def create_channel(
  body: ChannelCreate,
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
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
    copy_notifications=body.copy_notifications,
    app_ids=sorted(set(body.app_ids)) if body.app_ids is not None else None,
    include_body=body.include_body,
    accept_posts=body.accept_posts,
  )
  db.add(row)
  db.commit()
  return _channel_out(row)


@router.patch("/{channel_id}", dependencies=[Depends(reject_cross_site)])
def update_channel(
  channel_id: str,
  body: ChannelUpdate,
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
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
  if body.copy_notifications is not None:
    row.copy_notifications = body.copy_notifications
  if body.all_notifications:
    row.app_ids = None
  elif body.app_ids is not None:
    row.app_ids = sorted(set(body.app_ids))
  if body.include_body is not None:
    row.include_body = body.include_body
  if body.accept_posts is not None:
    row.accept_posts = body.accept_posts
  db.commit()
  return _channel_out(row)


@router.delete("/{channel_id}", status_code=204, dependencies=[Depends(reject_cross_site)])
def delete_channel(
  channel_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
  db: Session = Depends(get_db),
):
  row = _owned_channel(db, owner.id, channel_id)
  db.query(models.ChannelDelivery).filter(
    models.ChannelDelivery.channel_id == row.id,
  ).delete(synchronize_session=False)
  db.delete(row)
  db.commit()


@router.get("/{channel_id}/activity")
def channel_activity(
  channel_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
  db: Session = Depends(get_db),
):
  """The channel's recent sends, newest first: titles and outcomes only."""
  row = _owned_channel(db, owner.id, channel_id)
  sends = db.query(models.ChannelDelivery).filter(
    models.ChannelDelivery.channel_id == row.id,
  ).order_by(models.ChannelDelivery.id.desc()).limit(notification_channels.ACTIVITY_KEPT).all()
  return [
    {"sentAt": _iso(send.sent_at), "source": send.source, "title": send.title, "error": send.error}
    for send in sends
  ]


@router.post("/{channel_id}/test", dependencies=[Depends(reject_cross_site)])
def test_channel(
  channel_id: str,
  body: ChannelTest | None = None,
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
  db: Session = Depends(get_db),
):
  """Send a sample message now and report how it went."""
  row = _owned_channel(db, owner.id, channel_id)
  target = notification_channels.Target.of(row, include_body=True)
  message = _message(
    "test", "Möbius is connected",
    "Messages you choose in Channels will appear here.", body.link if body else None,
  )
  db.close()
  error = notification_channels.send_now(target, message)
  return {"ok": error is None, "error": error}


@router.post("/{channel_id}/send", dependencies=[Depends(reject_cross_site)])
def post_to_channel(
  channel_id: str,
  body: ChannelPost,
  owner: models.Owner = Depends(get_owner_or_app_with_channels_manage),
  db: Session = Depends(get_db),
):
  """Post a message on purpose, such as a release note, and report how it went.

  Only to a channel whose owner turned on ``accept_posts``: a channel added
  to copy notifications does not also become a place agents may speak.
  """
  row = _owned_channel(db, owner.id, channel_id)
  if not row.accept_posts:
    raise HTTPException(
      409, f"{row.label} doesn't accept posts. Turn on “Allow posts” for it in Channels.",
    )
  target = notification_channels.Target.of(row, include_body=True)
  message = _message("post", body.title.strip(), body.text, body.link)
  db.close()
  error = notification_channels.send_now(target, message)
  return {"ok": error is None, "error": error}
