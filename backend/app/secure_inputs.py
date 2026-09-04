"""One-use secure inputs with transient values and durable safe receipts.

The ordinary path gives an agent-authored local process programmatic access to
submitted values through stdin while keeping those values out of model context.
The registry never writes field values to a database, file, broadcast, result,
or diagnostic. Prompt labels and status are deliberately safe to persist as a
chat receipt. A separately marked reveal path exists for explicit owner-led
debugging; its tool result reaches the AI provider but is scrubbed before
Möbius broadcasts or persists it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from app.broadcast import get_broadcast
from app.owner_input import publish_owner_input_changed


COMPLETED_TTL_SECONDS = 2 * 60
FILLED_TTL_SECONDS = 2 * 60
CONSUMING_TTL_SECONDS = 2 * 60
MAX_REQUESTS = 32
MAX_FIELDS = 8
MAX_FIELD_VALUE_CHARS = 16 * 1024
MAX_TOTAL_VALUE_CHARS = 64 * 1024
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

REVEAL_BEGIN = "<<<MOBIUS_SECRET_REVEAL_BEGIN:"
REVEAL_END = "<<<MOBIUS_SECRET_REVEAL_END:"
REVEAL_NONCE_RE = re.compile(r"[0-9a-f]{32}")
REVEAL_REDACTION = (
  "[Secure input revealed to the model by explicit owner request; "
  "value omitted from Möbius chat and logs.]"
)


@dataclass
class SecureInputRequest:
  """Safe request metadata plus in-memory values and a one-way capability."""

  request_id: str
  chat_id: str
  mode: str
  title: str
  description: str
  fields: list[dict[str, Any]]
  capability_hash: bytes
  created_at: float
  status: str = "pending"
  values: dict[str, str] | None = None
  result: dict[str, Any] | None = None
  filled_at: float | None = None
  consuming_at: float | None = None
  settled_at: float | None = None
  event: asyncio.Event = field(default_factory=asyncio.Event)

  def public_event(self) -> dict[str, Any]:
    """Browser-safe card payload. Never includes a capability or field value."""
    return {
      "type": "secure_input_request",
      "request_id": self.request_id,
      "mode": self.mode,
      "title": self.title,
      "description": self.description,
      "fields": self.fields,
    }


_requests: dict[str, SecureInputRequest] = {}


def _schedule_cleanup(delay_seconds: float, request_id: str) -> None:
  """Arm lifecycle cleanup; access-time cleanup remains a fallback."""
  try:
    loop = asyncio.get_running_loop()
  except RuntimeError:
    return
  loop.call_later(delay_seconds, _scheduled_cleanup, request_id)


def _scheduled_cleanup(request_id: str) -> None:
  # Keeping the id in the callback, rather than the request object, never
  # extends the lifetime of submitted values. `_cleanup` owns every transition.
  if request_id in _requests:
    _cleanup()


def validate_request_spec(
  *, title: Any, description: Any, mode: Any, fields: Any,
) -> tuple[str, str, str, list[dict[str, Any]]]:
  """Return a bounded, browser-safe request spec or raise ValueError."""
  if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
    raise ValueError("Secure input title must be 1–80 characters.")
  if not isinstance(description, str) or len(description) > 240:
    raise ValueError("Secure input description is too long.")
  if mode not in {"sealed", "reveal"}:
    raise ValueError("Secure input mode must be sealed or reveal.")
  if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
    raise ValueError(f"Secure input requires 1–{MAX_FIELDS} fields.")

  normalized: list[dict[str, Any]] = []
  seen: set[str] = set()
  for raw in fields:
    if not isinstance(raw, dict):
      raise ValueError("Each secure input field must be an object.")
    name = raw.get("name")
    label = raw.get("label")
    input_type = raw.get("type", "password")
    autocomplete = raw.get("autocomplete", "off")
    if not isinstance(name, str) or not FIELD_NAME_RE.fullmatch(name):
      raise ValueError("Secure input field names must be lowercase identifiers.")
    if name in seen:
      raise ValueError("Secure input field names must be unique.")
    seen.add(name)
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
      raise ValueError("Secure input field labels must be 1–80 characters.")
    if input_type not in {"password", "text"}:
      raise ValueError("Secure input fields must be text or password inputs.")
    if not isinstance(autocomplete, str) or len(autocomplete) > 64:
      autocomplete = "off"
    normalized.append({
      "name": name,
      "label": label.strip(),
      "type": input_type,
      "autocomplete": autocomplete,
    })
  return title.strip(), description.strip(), mode, normalized


def validate_submitted_values(
  request: SecureInputRequest, values: Any,
) -> dict[str, str]:
  """Validate against the request shape without reflecting any value."""
  if not isinstance(values, dict):
    raise ValueError("Secure input fields are required.")
  expected = [field["name"] for field in request.fields]
  if set(values) != set(expected):
    raise ValueError("Secure input fields do not match this request.")
  normalized: dict[str, str] = {}
  total = 0
  for name in expected:
    value = values.get(name)
    if not isinstance(value, str) or not value:
      raise ValueError("Every secure input field is required.")
    if len(value) > MAX_FIELD_VALUE_CHARS:
      raise ValueError("A secure input value is too long.")
    total += len(value)
    if total > MAX_TOTAL_VALUE_CHARS:
      raise ValueError("Secure input submission is too large.")
    normalized[name] = value
  return normalized


def redact_reveal_markers(content: Any) -> Any:
  """Scrub explicit reveal envelopes before Möbius broadcast/persistence."""
  if not isinstance(content, str) or REVEAL_BEGIN not in content:
    return content
  output: list[str] = []
  cursor = 0
  while True:
    start = content.find(REVEAL_BEGIN, cursor)
    if start == -1:
      output.append(content[cursor:])
      break
    nonce_start = start + len(REVEAL_BEGIN)
    nonce_end = nonce_start + 32
    nonce = content[nonce_start:nonce_end]
    if (
      not REVEAL_NONCE_RE.fullmatch(nonce)
      or content[nonce_end:nonce_end + 3] != ">>>"
    ):
      # Not one of our envelopes. Preserve this prefix, then continue looking
      # rather than letting owner-provided text manufacture a scrub boundary.
      output.append(content[cursor:nonce_start])
      cursor = nonce_start
      continue
    output.append(content[cursor:start])
    end_token = f"{REVEAL_END}{nonce}>>>"
    end = content.find(end_token, nonce_end + 3)
    output.append(REVEAL_REDACTION)
    if end == -1:
      break
    cursor = end + len(end_token)
  return "".join(output)


def build_reveal_envelope(content: str) -> str:
  """Frame provider-visible content with an unguessable paired scrub nonce."""
  nonce = secrets.token_hex(16)
  return f"{REVEAL_BEGIN}{nonce}>>>{content}{REVEAL_END}{nonce}>>>"


def _capability_digest(capability: str) -> bytes:
  return hashlib.sha256(capability.encode("utf-8")).digest()


def _capability_matches(request: SecureInputRequest, capability: str) -> bool:
  if not isinstance(capability, str) or not capability:
    return False
  return hmac.compare_digest(
    request.capability_hash, _capability_digest(capability),
  )


def _publish(request: SecureInputRequest, event_type: str) -> None:
  event = (
    request.public_event()
    if event_type == "secure_input_request"
    else {
      "type": event_type,
      "request_id": request.request_id,
      "status": request.status,
    }
  )
  bc = get_broadcast(request.chat_id)
  chat_event_published = False
  if bc is not None and bc.running:
    # The live sink owns both broadcast and chat persistence. Import lazily to
    # avoid the module cycle: ChatEventSink imports this module's reveal scrub.
    from app.chat_event_sink import get_active_sink
    sink = get_active_sink(request.chat_id)
    if sink is not None and sink.bc is bc:
      sink.publish(event)
    else:
      # Defensive fallback for synthetic/tests or a teardown race. Values are
      # still absent; the active production path always has the owning sink.
      bc.publish(event)
    chat_event_published = True
  if event_type == "secure_input_request" and not chat_event_published:
    raise RuntimeError("This chat is not running.")


def _clear_values(request: SecureInputRequest) -> None:
  values = request.values
  request.values = None
  if values is not None:
    values.clear()


def _settle(
  request: SecureInputRequest,
  status: str,
  result: dict[str, Any],
) -> None:
  if request.status in {"completed", "failed", "cancelled", "expired"}:
    return
  was_waiting_for_owner = request.status == "pending"
  _clear_values(request)
  request.status = status
  request.result = result
  request.settled_at = time.monotonic()
  request.event.set()
  _publish(request, "secure_input_settled")
  if was_waiting_for_owner:
    publish_owner_input_changed(request.chat_id, None)


def _cleanup() -> None:
  now = time.monotonic()
  for request in list(_requests.values()):
    expired_filled = (
      request.status == "filled"
      and request.filled_at is not None
      and now - request.filled_at >= FILLED_TTL_SECONDS
    )
    expired_consumer = (
      request.status == "consuming"
      and request.consuming_at is not None
      and now - request.consuming_at >= CONSUMING_TTL_SECONDS
    )
    if expired_filled or expired_consumer:
      _settle(
        request,
        "expired",
        {"ok": False, "message": "Secure input expired before completion."},
      )
  for request_id, request in list(_requests.items()):
    if (
      request.settled_at is not None
      and now - request.settled_at >= COMPLETED_TTL_SECONDS
    ):
      _requests.pop(request_id, None)


def create_request(
  *,
  chat_id: str,
  mode: str,
  title: str,
  description: str,
  fields: list[dict[str, Any]],
) -> tuple[SecureInputRequest, str]:
  """Register one request and return it with its one-way access capability."""
  _cleanup()
  if any(
    request.chat_id == chat_id
    and request.status in {"pending", "filled", "consuming"}
    for request in _requests.values()
  ):
    raise ValueError("A secure input request is already open in this chat.")
  if len(_requests) >= MAX_REQUESTS:
    raise RuntimeError("Too many secure input requests are active.")

  capability = secrets.token_urlsafe(32)
  request = SecureInputRequest(
    request_id=secrets.token_urlsafe(18),
    chat_id=chat_id,
    mode=mode,
    title=title,
    description=description,
    fields=fields,
    capability_hash=_capability_digest(capability),
    created_at=time.monotonic(),
  )
  _requests[request.request_id] = request
  return request, capability


def get_request(request_id: str) -> SecureInputRequest | None:
  _cleanup()
  return _requests.get(request_id)


def pending_chat_ids() -> frozenset[str]:
  """Snapshot chats whose secure-input card still needs owner involvement."""
  _cleanup()
  return frozenset(
    request.chat_id
    for request in _requests.values()
    if request.status == "pending"
  )


def publish_request(request: SecureInputRequest) -> None:
  """Publish one newly registered prompt through both of its safe channels."""
  if request.status != "pending":
    raise ValueError("Secure input request is no longer open.")
  try:
    _publish(request, "secure_input_request")
    publish_owner_input_changed(request.chat_id, "secure_input")
  except Exception:
    # No value has been submitted yet. Remove an unpresented request so a
    # failed/racing publish cannot strand the chat behind an invisible card.
    if _requests.get(request.request_id) is request:
      _requests.pop(request.request_id, None)
    raise


def authorize(
  request_id: str, capability: str,
) -> SecureInputRequest | None:
  request = get_request(request_id)
  if request is None or not _capability_matches(request, capability):
    return None
  return request


def fill_request(request: SecureInputRequest, values: dict[str, str]) -> None:
  if request.status != "pending":
    raise ValueError("Secure input request is no longer open.")
  request.values = values
  request.status = "filled"
  request.filled_at = time.monotonic()
  request.event.set()
  _publish(request, "secure_input_filled")
  publish_owner_input_changed(request.chat_id, None)
  _schedule_cleanup(FILLED_TTL_SECONDS, request.request_id)


def consume_request(request: SecureInputRequest) -> dict[str, str]:
  """Move values out exactly once. The caller owns clearing the returned dict."""
  if request.status != "filled" or request.values is None:
    raise ValueError("Secure input is not ready to consume.")
  values = request.values
  request.values = None
  request.status = "consuming"
  request.consuming_at = time.monotonic()
  request.event.clear()
  _publish(request, "secure_input_consuming")
  _schedule_cleanup(CONSUMING_TTL_SECONDS, request.request_id)
  return values


def settle_request(
  request: SecureInputRequest, *, ok: bool, message: str,
) -> None:
  safe_message = str(message or "Secure input complete.")[:240]
  _settle(
    request,
    "completed" if ok else "failed",
    {"ok": bool(ok), "message": safe_message},
  )
  _schedule_cleanup(COMPLETED_TTL_SECONDS, request.request_id)


def cancel_request(request: SecureInputRequest) -> None:
  _settle(
    request,
    "cancelled",
    {"ok": False, "message": "Secure input was cancelled."},
  )
  _schedule_cleanup(COMPLETED_TTL_SECONDS, request.request_id)


def cancel_chat(chat_id: str) -> None:
  """Cancel every transient request owned by a stopped/deleted chat."""
  _cleanup()
  for request in list(_requests.values()):
    if (
      request.chat_id == chat_id
      and request.status in {"pending", "filled", "consuming"}
    ):
      cancel_request(request)


def secure_input_memory_diagnostics() -> dict[str, int]:
  """Cardinality-only diagnostics; never expose request metadata or values."""
  _cleanup()
  return {
    "request_count": len(_requests),
    "pending_count": sum(
      r.status in {"pending", "filled", "consuming"}
      for r in _requests.values()
    ),
  }
