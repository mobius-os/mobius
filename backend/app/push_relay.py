"""Client for the mobius.you native push relay — the whole wire contract.

The relay holds the FCM credentials for every Möbius installation, so this
installation keeps no push secret. Requests go over the root-owned identity
broker's Unix socket; the broker mints a short-lived capability bound to this
installation, its linked mobius.you account and the exact request, and forwards
it to the relay (``PUSH_ROUTES`` in ``backend/runtime/identity_broker.py``).
Every route needs an ``Idempotency-Key``; each call uses a fresh one because
nothing here retries.

``POST /v1/push/devices/bind``   ``{provider: "fcm", token}``
    This installation now owns the token; the last installation to bind it
    wins, and the relay refuses sends to a token this installation has not
    bound.
``POST /v1/push/devices/unbind`` ``{token}``
``POST /v1/push/send``
    ``{targets: [{provider: "fcm", token}], notification: {title, body},
       data: {url, notificationId}, options: {collapseKey, ttlSeconds, priority}}``
    → ``{results: [{index, status: ok|invalid_token|retry|error}]}``
"""

from __future__ import annotations

import json
import uuid

import httpx

from app.runtime_identity import broker_client


BIND_PATH = "/v1/push/devices/bind"
UNBIND_PATH = "/v1/push/devices/unbind"
SEND_PATH = "/v1/push/send"
# The broker mints a capability at mobius.you, then forwards to the relay
# with its own 30 second ceiling; this bound covers both hops.
_TIMEOUT = httpx.Timeout(35.0, connect=3.0)
_STATUSES = frozenset({"ok", "invalid_token", "retry", "error"})
# The relay refuses a notification past these limits (FCM allows 4 KB for the
# notification and data together), and Möbius notifications may be longer, so
# the text is shortened to fit instead.
MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 2000
MAX_PAYLOAD_BYTES = 4096
_ELLIPSIS = "…"


class RelayError(Exception):
  """The relay could not be reached or refused the request."""

  def __init__(self, code: str, *, status: int | None = None) -> None:
    super().__init__(code)
    self.code = code
    self.status = status

  @property
  def not_linked(self) -> bool:
    # The broker cannot mint a capability until a mobius.you account is linked.
    return self.status == 401


def _clip(text: str, limit: int) -> str:
  return text if len(text) <= limit else text[:limit - 1] + _ELLIPSIS


def _payload_bytes(title: str, body: str, data: dict[str, str]) -> int:
  # Measured the way the relay measures it.
  return len(json.dumps(
    {"title": title, "body": body, "data": data},
    separators=(",", ":"), ensure_ascii=False,
  ).encode())


def _shrink(text: str, excess: int, *, keep: int) -> str:
  # Every character is at least one byte, so dropping ``excess`` characters
  # (plus room for the three-byte ellipsis) removes at least that many bytes.
  cut = text[:max(keep, len(text) - excess - len(_ELLIPSIS.encode()))]
  return cut + _ELLIPSIS if cut else ""


def fit_notification(
  title: str, body: str, data: dict[str, str],
) -> tuple[str, str]:
  """Shorten body, then title, so the relay accepts the notification.

  ``data`` is sent unchanged; the caller keeps it small (a link and an id).
  The relay requires a title, so at least its first character stays.
  """
  title = _clip(title, MAX_TITLE_CHARS)
  body = _clip(body, MAX_BODY_CHARS)
  while (excess := _payload_bytes(title, body, data) - MAX_PAYLOAD_BYTES) > 0:
    if body:
      body = _shrink(body, excess, keep=0)
    elif len(title) > 2:
      title = _shrink(title, excess, keep=1)
    else:
      break
  return title, body


def _post(path: str, payload: dict) -> httpx.Response:
  body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
  try:
    with broker_client(timeout=_TIMEOUT) as client:
      response = client.post(path, content=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": f"push:{uuid.uuid4().hex}",
      })
  except (httpx.HTTPError, OSError) as exc:
    raise RelayError(f"unreachable:{type(exc).__name__}") from None
  if response.status_code >= 400:
    raise RelayError(f"http_{response.status_code}", status=response.status_code)
  return response


def bind_device(token: str) -> None:
  _post(BIND_PATH, {"provider": "fcm", "token": token})


def unbind_device(token: str) -> None:
  _post(UNBIND_PATH, {"token": token})


def send(
  tokens: list[str], *, title: str, body: str, data: dict[str, str],
  collapse_key: str, ttl_seconds: int = 24 * 3600,
) -> list[str]:
  """Send one notification; returns one status per token, in order.

  A missing or malformed result counts as ``error``, never ``ok``.
  """
  data = {key: str(value) for key, value in data.items()}
  title, body = fit_notification(title, body, data)
  response = _post(SEND_PATH, {
    "targets": [{"provider": "fcm", "token": token} for token in tokens],
    "notification": {"title": title, "body": body},
    "data": data,
    "options": {
      "collapseKey": collapse_key, "ttlSeconds": ttl_seconds, "priority": "high",
    },
  })
  statuses = ["error"] * len(tokens)
  try:
    results = response.json().get("results")
  except (ValueError, AttributeError):
    return statuses
  for item in results if isinstance(results, list) else ():
    if not isinstance(item, dict):
      continue
    index, status = item.get("index"), item.get("status")
    if (
      isinstance(index, int) and not isinstance(index, bool)
      and 0 <= index < len(tokens)
      and isinstance(status, str) and status in _STATUSES
    ):
      statuses[index] = status
  return statuses
