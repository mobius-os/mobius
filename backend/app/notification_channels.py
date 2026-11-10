"""Copy owner notifications to outside channels: Slack, Discord, ntfy, webhooks.

A channel is one more delivery destination beside browser and phone push; it
is selected in ``push._prepare_owner_notification`` under the same rules (not
while the owner watches the source, never for quiet maintenance notices), so
"notify me" means the same thing everywhere. This module owns everything
channel-specific: recognising a link, keeping it encrypted, shaping each
service's message, and the SSRF-safe send.

The webhook link is a credential (anyone holding it can post), so it is
stored encrypted and never returned. Message text is opt-in per channel:
by default only the title and a link back leave this installation.
"""

import base64
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlparse, urlunparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.net_utils import validate_url_safe
from app.timeutil import now_naive_utc

logger = logging.getLogger(__name__)

KIND_NAMES = {
  "slack": "Slack", "discord": "Discord", "ntfy": "ntfy", "webhook": "Webhook",
}
_TIMEOUT = httpx.Timeout(10.0)
_BODY_MAX = 500
_SENDS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify-channel")


@dataclass(frozen=True)
class Message:
  title: str
  body: str | None
  url: str
  notification_id: str
  source_type: str
  source_id: str | None


@dataclass(frozen=True)
class Target:
  """What a worker thread needs to deliver one channel; no ORM objects."""
  id: str
  kind: str
  url_encrypted: str
  include_body: bool


# ── the stored link ─────────────────────────────────────────────────────────

def _fernet() -> Fernet:
  material = f"mobius-notification-channel-v1:{get_settings().secret_key}".encode()
  return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def encrypt_url(url: str) -> str:
  return _fernet().encrypt(url.encode()).decode()


def decrypt_url(token: str) -> str:
  return _fernet().decrypt(token.encode()).decode()


def detect_kind(url: str) -> str:
  host = (urlparse(url).hostname or "").lower()
  if host == "hooks.slack.com":
    return "slack"
  if host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com") \
      and "/api/webhooks/" in urlparse(url).path:
    return "discord"
  if host == "ntfy.sh":
    return "ntfy"
  return "webhook"


def url_hint(url: str) -> str:
  """Enough to recognise a link in Settings without revealing a usable one."""
  parsed = urlparse(url)
  tail = parsed.path.rstrip("/")[-4:]
  return f"{parsed.hostname}/…{tail}" if tail else (parsed.hostname or "")


# A Slack or Discord link copied by hand is easily cut short, and the service
# then only refuses it at the first send. Their shapes are fixed, so check them
# when the link is saved.
_COMPLETE_PATH = {
  "slack": re.compile(r"/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{8,}/?"),
  "discord": re.compile(r"/api/webhooks/\d+/[A-Za-z0-9_-]{8,}/?"),
}
_INCOMPLETE = {
  "slack": "This Slack link looks cut off. In Slack, use the Copy button next to your channel's webhook.",
  "discord": "This Discord link looks cut off. In Discord, use Copy Webhook URL.",
}


def check_url(url: str, kind: str) -> str:
  """Normalise and validate a link before it is saved; raises HTTPException 400."""
  url = url.strip()
  parsed = urlparse(url)
  if parsed.scheme != "https" or not parsed.hostname:
    raise HTTPException(400, "Use the full https:// link the service gave you.")
  if kind in _COMPLETE_PATH and not _COMPLETE_PATH[kind].fullmatch(parsed.path):
    raise HTTPException(400, _INCOMPLETE[kind])
  if kind == "ntfy" and not parsed.path.strip("/"):
    raise HTTPException(400, "Add the topic to the ntfy link, like https://ntfy.sh/your-topic.")
  validate_url_safe(url)
  return url


# ── per-service messages ────────────────────────────────────────────────────

def _clip(text: str | None, limit: int = _BODY_MAX) -> str:
  text = (text or "").strip()
  return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _slack_escape(text: str) -> str:
  # Slack's only control characters. Escaping them also defuses <!channel>.
  return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_DISCORD_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#-])")


def _discord_escape(text: str) -> str:
  return _DISCORD_MARKDOWN.sub(r"\\\1", text)


def build_request(kind: str, url: str, message: Message, include_body: bool) -> tuple[str, dict]:
  """Return ``(post_url, json_body)`` for one service's webhook."""
  title = _clip(message.title, 200)
  body = _clip(message.body) if include_body else ""
  if kind == "slack":
    lines = [f"*{_slack_escape(title)}*"]
    if body:
      lines.append(_slack_escape(body))
    lines.append(f"<{message.url}|Open in Möbius>")
    return url, {"text": "\n".join(lines), "unfurl_links": False, "unfurl_media": False}
  if kind == "discord":
    lines = [f"**{_discord_escape(title)}**"]
    if body:
      lines.append(_discord_escape(body))
    # Angle brackets keep Discord from unfurling a preview of the shell.
    lines.append(f"[Open in Möbius](<{message.url}>)")
    return url, {
      "content": "\n".join(lines)[:2000],
      "username": "Möbius",
      # Remote senders choose titles and text: never let them ping anyone.
      "allowed_mentions": {"parse": []},
    }
  if kind == "ntfy":
    # JSON publishing goes to the server root with the topic in the body;
    # that keeps non-ASCII titles out of HTTP headers.
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    root, _, topic = path.rpartition("/")
    post_url = urlunparse(parsed._replace(path=(root or "") + "/"))
    payload = {"topic": topic, "title": title, "message": body or title, "click": message.url}
    return post_url, payload
  return url, {
    "id": message.notification_id,
    "title": title,
    "body": body or None,
    "url": message.url,
    "source": {"type": message.source_type, "id": message.source_id},
    "sent_at": datetime.now(UTC).isoformat(),
  }


# ── delivery ────────────────────────────────────────────────────────────────

class _DeliveryError(Exception):
  pass


def _post(url: str, payload: dict) -> None:
  """POST JSON to a public address, pinned to the address that was checked."""
  pinned_url, host_header, sni_host = validate_url_safe(url)
  with httpx.Client(
    follow_redirects=False, timeout=_TIMEOUT,
    cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
  ) as client:
    response = client.post(
      pinned_url, json=payload,
      headers={"Host": host_header, "User-Agent": "Mobius-Notifications/1"},
      extensions={"sni_hostname": sni_host},
    )
  if response.status_code >= 300:
    raise _DeliveryError(f"The service answered {response.status_code}.")


def deliver(target: Target, message: Message) -> str | None:
  """Send one message; returns None on success or a short owner-facing error."""
  try:
    url = decrypt_url(target.url_encrypted)
    post_url, payload = build_request(target.kind, url, message, target.include_body)
    _post(post_url, payload)
    return None
  except InvalidToken:
    return "This link can't be read any more. Remove it and add it again."
  except HTTPException as exc:
    return str(exc.detail)[:300]
  except _DeliveryError as exc:
    return str(exc)
  except httpx.TimeoutException:
    return "The service didn't answer in time."
  except httpx.RequestError:
    return "The service couldn't be reached."
  except Exception:
    logger.exception("notification channel %s delivery failed", target.id[:8])
    return "Sending failed."


def record_outcome(channel_id: str, error: str | None) -> None:
  with SessionLocal() as db:
    db.query(models.NotificationChannel).filter(
      models.NotificationChannel.id == channel_id,
    ).update({
      "last_sent_at": now_naive_utc(),
      "last_error": error,
    }, synchronize_session=False)
    db.commit()


def _deliver_all(targets: tuple[Target, ...], message: Message) -> None:
  for target in targets:
    error = deliver(target, message)
    if error:
      # Channel ids only: links and message text stay out of logs.
      logger.warning("notification channel %s: %s", target.id[:8], error)
    try:
      record_outcome(target.id, error)
    except Exception:
      logger.exception("notification channel %s: outcome not recorded", target.id[:8])


def send_later(targets: tuple[Target, ...], message: Message) -> None:
  """Deliver off the caller's thread, like phone push; no retry."""
  if targets:
    _SENDS.submit(_deliver_all, targets, message)


def targets_for(db: Session, owner_id: int, source_type: str, source_id: str | None) -> tuple[Target, ...]:
  """The owner's channels that want a notification from this source."""
  rows = db.query(models.NotificationChannel).filter(
    models.NotificationChannel.owner_id == owner_id,
  ).all()
  chosen = []
  for row in rows:
    if row.app_ids is not None:
      if source_type != "app" or source_id not in {str(app_id) for app_id in row.app_ids}:
        continue
    chosen.append(Target(
      id=row.id, kind=row.kind, url_encrypted=row.url_encrypted,
      include_body=bool(row.include_body),
    ))
  return tuple(chosen)
