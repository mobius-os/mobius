"""VAPID key management and Web Push delivery."""

import asyncio
import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from py_vapid import Vapid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, presence
from app.config import get_settings

logger = logging.getLogger(__name__)

_vapid: Vapid | None = None

_QUIET_PUSH_SOURCE_TYPES = frozenset({
  "platform_conflict",
  "platform_update",
  "shell",
  "shell_rebuild",
  "shell_rebuilding",
  "shell_rebuilt",
  "shell_rebuild_failed",
})


def _is_quiet_maintenance_push(*, source_type: str | None) -> bool:
  """Return True for shell/platform maintenance notices.

  These remain in notification history but should not become OS/browser
  popups. User-facing agent/app notifications still use the normal push path.

  Suppression is decided PURELY by `source_type` membership, never by the
  free text of the title/body. The invariant: a push's copy must never change
  whether it pops. An earlier version substring-matched "platform update" in
  the text, which meant a legitimate resume push ("Your turn was paused for an
  update — tap to resume.", source_type="system") survived only by an accident
  of wording — rephrasing it toward "platform update" would have silently
  swallowed a recovery notification the owner needs. Any push that must stay
  quiet declares a maintenance source_type; everything else is delivered.
  """
  source = (source_type or "").strip().lower()
  return source in _QUIET_PUSH_SOURCE_TYPES


def _key_dir() -> Path:
  settings = get_settings()
  return Path(settings.data_dir) / "push"


def init_vapid():
  """Load or generate VAPID keys. Call once at startup."""
  global _vapid
  d = _key_dir()
  d.mkdir(parents=True, exist_ok=True)
  priv = d / "private_key.pem"
  pub = d / "public_key.pem"
  v = Vapid()
  if priv.exists():
    # Best-effort tighten perms; if the key was created by a previous
    # boot under a different uid (or by an entrypoint root step), the
    # chmod can EPERM. Don't crash startup over a perm hygiene step —
    # the key is still readable, which is what matters for boot.
    try:
      priv.chmod(0o600)
    except PermissionError:
      logger.warning(
        "Could not chmod 0o600 on existing VAPID private key at %s "
        "(owned by another uid?). Proceeding with existing perms.",
        priv,
      )
    v = Vapid.from_pem(priv.read_bytes())
  else:
    v.generate_keys()
    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
      f.write(v.private_pem())
    pub.write_bytes(v.public_pem())
    logger.info("Generated new VAPID keys in %s", d)
  _vapid = v


def get_public_key_base64url() -> str:
  """Return the VAPID public key as a base64url-encoded string."""
  if _vapid is None:
    raise RuntimeError("VAPID not initialized — call init_vapid() first")
  raw = _vapid.public_key.public_bytes(
    encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.X962,
    format=__import__("cryptography").hazmat.primitives.serialization.PublicFormat.UncompressedPoint,
  )
  return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def get_vapid_claims() -> dict:
  """Return VAPID claims dict for pywebpush."""
  settings = get_settings()
  return {"sub": f"mailto:admin@{settings.domain}"}


def send_push(subscription_info: dict, payload: dict) -> bool:
  """Send a Web Push notification. Returns True on success, False on gone."""
  if _vapid is None:
    raise RuntimeError("VAPID not initialized — call init_vapid() first")
  # Startup and suppressed notifications do not need the delivery stack.
  # Import it only once a push is actually ready to be sent.
  from pywebpush import WebPushException, webpush

  try:
    # Pass the Vapid instance directly — pywebpush accepts it and
    # avoids the PEM-vs-raw-key parsing ambiguity in from_string().
    webpush(
      subscription_info=subscription_info,
      data=json.dumps(payload),
      vapid_private_key=_vapid,
      vapid_claims=get_vapid_claims(),
      content_encoding="aes128gcm",
    )
    return True
  except WebPushException as e:
    if e.response is not None and e.response.status_code == 410:
      return False
    logger.error("Web Push failed: %s", e)
    raise


@dataclass(frozen=True)
class _PreparedPush:
  notification_id: str
  payload: dict
  subscriptions: tuple[dict, ...]


def _prepare_owner_notification(
  db: Session,
  owner_id: int,
  *,
  title: str,
  body: str | None,
  source_type: str = "system",
  source_id: str | None = None,
  icon: str | None = None,
  target: str | None = None,
  actions: list[dict] | None = None,
  notification_id: str | None = None,
) -> tuple[str, _PreparedPush | None]:
  """Persist and announce one notification, returning optional push work.

  This loop-owned half performs database work and publishes live system-bus
  events. Remote Web Push is returned as inert scalar data so async callers can
  move only that blocking I/O to a worker thread without touching asyncio
  queues or a SQLAlchemy Session from the wrong thread.

  THE producer seam for owner notifications — any subsystem that wants a
  row in the notification preview calls this and nothing else. One call =
  one history row + a `notification_created` badge nudge on the system
  bus + a (possibly suppressed) Web Push. Callers never touch the
  broadcast or push machinery directly; a transient system-bus event
  that should ALSO persist in the bell gets a one-line call here at its
  publish site. Contract:
    - source_type (<=16 chars): 'system' (platform), 'agent' (chat
      agents; the /send default), 'app' + source_id=str(app_id) (also
      lights the drawer activity dot). New subsystems add a short
      stable slug.
    - target: in-scope deep-link only — '/shell/?app=<id>' or
      '/shell/?chat=<id>' (legacy '/app/:id' and '/chat/:id' still parse).
      Clients treat it as UNTRUSTED and fail closed on anything else.
  """
  notification_id = notification_id or str(uuid.uuid4())
  if db.query(models.Notification.id).filter(
    models.Notification.id == notification_id,
  ).first() is not None:
    return notification_id, None
  notif = models.Notification(
    id=notification_id,
    owner_id=owner_id,
    source_type=source_type,
    source_id=source_id,
    title=title,
    body=body,
    icon=icon,
    target=target,
    actions=actions,
    sent_at=datetime.now(UTC),
  )
  db.add(notif)
  # App background jobs already attribute their success/failure notification
  # to the app. Make that canonical completion edge own the drawer dot too,
  # rather than requiring every cron script to remember a parallel signal.
  from app.app_activity import mark_from_notification
  activity_app_id = mark_from_notification(
    db, source_type=source_type, source_id=source_id,
  )
  try:
    db.commit()
  except IntegrityError:
    # A deterministic producer may be repaired concurrently (for example by
    # startup and a status read). The primary key is the authority: losing
    # that insert race is ordinary idempotent attach, not a failed push. The
    # winning transaction owns the live bus nudge and remote delivery.
    db.rollback()
    if db.query(models.Notification.id).filter(
      models.Notification.id == notification_id,
    ).first() is not None:
      return notification_id, None
    raise
  except Exception:
    # Persist failure → SKIP push delivery. Sending a push for a
    # notification that has no history row creates a state-mismatch
    # the user can't reason about (the push exists in their OS but
    # no in-app record). Consistency wins over loud-over-silent here.
    #
    # Log loudly so the agent can find it in chat.log and react
    # (e.g. re-emit a question via the chat surface, or surface a
    # banner). The caller's path (chat-turn loop) is not broken —
    # the function still returns an id and the runner continues.
    logger.error(
      "notify_owner: persist FAILED — push SKIPPED for consistency "
      "(owner=%s source_type=%s source_id=%s title=%r). "
      "Agent should consider re-emitting via the chat surface.",
      owner_id, source_type, source_id, title,
    )
    try:
      db.rollback()
    except Exception:
      pass
    return notification_id, None

  if activity_app_id is not None:
    # The row above is durable; this replay-free event only makes a live shell
    # refetch immediately. A reconnect/boot refetch recovers a missed event.
    from app.broadcast import get_system_broadcast
    get_system_broadcast().publish({
      "type": "app_activity", "appId": str(activity_app_id),
    })

  # Replay-free nudge so a live shell's bell badge refetches immediately;
  # an SSE-reconnect refetch recovers any missed event. Deliberately BEFORE
  # the push-suppression early returns below: the bell is the in-app surface
  # those suppressions defer to, so a quiet-maintenance or watched-source
  # notification must still update the badge. Payload carries no title/body —
  # the bus is not a content channel. NOT in SYSTEM_EVENT_TYPES: agents must
  # not be able to spoof badge nudges via POST /api/notify.
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({
    "type": "notification_created", "id": notification_id,
  })

  # Skip push when a live SSE subscriber is already watching the
  # source chat — the in-tab UX surfaces the event there. presence
  # owns this contract so we don't have to reach across modules
  # into broadcast internals.
  if source_id and presence.has_watchers(source_id):
    return notification_id, None

  if _is_quiet_maintenance_push(source_type=source_type):
    return notification_id, None

  payload = {
    "id": notification_id,
    "title": title,
    "body": body,
    "icon": icon,
    "target": target,
    "actions": actions,
  }

  subscriptions = [
    {
      "id": sub.id,
      "endpoint": sub.endpoint,
      "p256dh": sub.p256dh,
      "auth": sub.auth,
    }
    for sub in (
      db.query(models.PushSubscription)
      .filter(models.PushSubscription.owner_id == owner_id)
      .all()
    )
  ]
  # Web Push is remote I/O and may wait on several endpoints. Copy the four
  # scalar fields we need, then release the checkout before delivery. If stale
  # subscriptions are found, delivery opens a fresh short cleanup transaction.
  db.close()
  return notification_id, _PreparedPush(
    notification_id=notification_id,
    payload=payload,
    subscriptions=tuple(subscriptions),
  )


def _deliver_prepared_push(prepared: _PreparedPush) -> None:
  """Perform remote delivery from scalar data; safe to call in a worker."""
  stale_ids = []
  for sub in prepared.subscriptions:
    sub_info = {
      "endpoint": sub["endpoint"],
      "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
      alive = send_push(sub_info, prepared.payload)
      if not alive:
        stale_ids.append(sub["id"])
    except Exception:
      logger.exception("push delivery failed for sub %s", sub["id"][:8])

  if stale_ids:
    from app.database import SessionLocal
    with SessionLocal() as cleanup_db:
      cleanup_db.query(models.PushSubscription).filter(
        models.PushSubscription.id.in_(stale_ids)
      ).delete(synchronize_session=False)
      cleanup_db.commit()


def notify_owner(
  db: Session,
  owner_id: int,
  *,
  title: str,
  body: str | None,
  source_type: str = "system",
  source_id: str | None = None,
  icon: str | None = None,
  target: str | None = None,
  actions: list[dict] | None = None,
  notification_id: str | None = None,
) -> str:
  """Save a notification and synchronously deliver its optional Web Push."""
  notification_id, prepared = _prepare_owner_notification(
    db,
    owner_id,
    title=title,
    body=body,
    source_type=source_type,
    source_id=source_id,
    icon=icon,
    target=target,
    actions=actions,
    notification_id=notification_id,
  )
  if prepared is not None:
    _deliver_prepared_push(prepared)
  return notification_id


async def notify_owner_async(
  db: Session,
  owner_id: int,
  *,
  title: str,
  body: str | None,
  source_type: str = "system",
  source_id: str | None = None,
  icon: str | None = None,
  target: str | None = None,
  actions: list[dict] | None = None,
  notification_id: str | None = None,
) -> str:
  """Save a notification, then deliver Web Push without blocking the loop."""
  notification_id, prepared = _prepare_owner_notification(
    db,
    owner_id,
    title=title,
    body=body,
    source_type=source_type,
    source_id=source_id,
    icon=icon,
    target=target,
    actions=actions,
    notification_id=notification_id,
  )
  if prepared is not None:
    await asyncio.to_thread(_deliver_prepared_push, prepared)

  return notification_id
