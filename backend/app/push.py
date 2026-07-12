"""VAPID key management and Web Push delivery."""

import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from py_vapid import Vapid
from pywebpush import webpush, WebPushException
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
) -> str:
  """Saves a Notification row and fires Web Push to the owner.

  The shared implementation behind `POST /api/notifications/send`,
  factored out so a non-request caller (e.g. a scheduled task) can
  reuse it without a `Request`. Push delivery is suppressed when the
  owner is currently subscribed to the SSE stream for `source_id` —
  the in-tab UX already shows the event. The notification row is
  saved either way so history is consistent. Returns the
  notification id.
  """
  notification_id = str(uuid.uuid4())
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
  try:
    db.commit()
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
    return notification_id

  # Skip push when a live SSE subscriber is already watching the
  # source chat — the in-tab UX surfaces the event there. presence
  # owns this contract so we don't have to reach across modules
  # into broadcast internals.
  if source_id and presence.has_watchers(source_id):
    return notification_id

  if _is_quiet_maintenance_push(source_type=source_type):
    return notification_id

  payload = {
    "id": notification_id,
    "title": title,
    "body": body,
    "icon": icon,
    "target": target,
    "actions": actions,
  }

  subs = (
    db.query(models.PushSubscription)
    .filter(models.PushSubscription.owner_id == owner_id)
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

  return notification_id
