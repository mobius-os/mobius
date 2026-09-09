"""Bounded transcript metadata for Möbius peer-network sends.

Claude and Codex expose the same platform-owned MCP tools with different wire
names and result envelopes. This module is the single provider boundary for
recognizing those calls and reducing their results to the small, durable facts
the owner-facing transcript needs. Historic read markers remain displayable,
but next-turn context is now the only inbound delivery path.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent_coordination import MESSAGE_KINDS as _KINDS


CLAUDE_SEND_TOOL = "mcp__mobius_control__send_agent_message"
CODEX_SEND_TOOL = "mobius_control:send_agent_message"

MAX_PEER_NOTES = 8
MAX_RESULT_MESSAGES = 200  # bounds both current receipts and historic markers
MAX_RESULT_CONTENT_ITEMS = 8
MAX_RESULT_ENVELOPE_CHARS = 512 * 1024
# The card preserves the FULL note (the platform caps a note at 4,000 chars),
# so an owner never mistakes an excerpt for the whole note. body_truncated only
# trips on the defensive over-limit case.
MAX_BODY_CHARS = 4000
# The provider-handoff transcript stays tight regardless — a coordination note
# is summarized there, not reproduced in full, to protect the context budget.
MAX_COMPACTION_BODY_CHARS = 400
MAX_NAME_CHARS = 120
MAX_KIND_CHARS = 24

DIRECTION_SEND = "send"
# Persisted transcripts may contain read markers created before inbound
# delivery became push-only. Keep their projection contract without exposing
# or recognizing a new model-facing read operation.
DIRECTION_READ = "read"

STATUS_SENDING = "sending"
STATUS_READING = "reading"
STATUS_SENT = "sent"
STATUS_RECEIVED = "received"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"

_DEFAULT_KIND = "note"
# Kind taxonomy is owned by the coordination domain; import it so a new kind
# there can never be silently downgraded here.
_SEND_TOOLS = frozenset((CLAUDE_SEND_TOOL, CODEX_SEND_TOOL))
_STATUSES = frozenset((
  STATUS_SENDING, STATUS_READING, STATUS_SENT, STATUS_RECEIVED,
  STATUS_EMPTY, STATUS_FAILED,
))


def peer_call_direction(tool_name: Any) -> str | None:
  """Return the shared action represented by a provider's exact tool name."""
  if tool_name in _SEND_TOOLS:
    return DIRECTION_SEND
  return None


def peer_message_from_call(tool_name: Any) -> dict | None:
  """Build the provisional marker visible while the MCP call is running."""
  direction = peer_call_direction(tool_name)
  if direction == DIRECTION_SEND:
    return {"direction": DIRECTION_SEND, "status": STATUS_SENDING}
  return None


def _clip(value: Any, limit: int) -> str:
  if not isinstance(value, str):
    return ""
  return value.strip()[:limit]


def _flatten(value: Any, limit: int) -> str:
  """Collapse ALL whitespace to single spaces, then clip.

  Used where a bounded string is spliced into a single logical line — a name in
  a header, or a note body joined into the provider-handoff transcript. An
  untrusted note body carrying "\\n\\nUSER:" must not be able to forge a turn in
  the compaction source, so newlines never survive there. The card keeps the
  raw (newline-preserving) body for display.
  """
  if not isinstance(value, str):
    return ""
  return " ".join(value.split())[:limit]


MAX_REASON_CHARS = 200


def _is_failure_exit(code: Any) -> bool:
  """A tool's own nonzero exit is the authoritative signal that it failed."""
  return isinstance(code, (int, float)) and not isinstance(code, bool) and code != 0


def _failure_reason(output: Any, exit_code: Any = None) -> str:
  """Extract a bounded, single-line failure reason from a tool result.

  Restores the observability the generic "Ran commands" block gave on expand:
  the owner most needs the error text exactly when a coordination call failed
  (e.g. "recipient is unavailable"). Bounded and whitespace-flattened.
  """
  data = _decode_object(output)
  if isinstance(data, dict):
    error = data.get("error")
    if isinstance(error, dict):
      message = _flatten(error.get("message"), MAX_REASON_CHARS)
      if message:
        return message
    if data.get("isError") and isinstance(data.get("content"), list):
      for item in data["content"][:MAX_RESULT_CONTENT_ITEMS]:
        text = item if isinstance(item, str) else (
          item.get("text") if isinstance(item, dict) else None
        )
        flattened = _flatten(text, MAX_REASON_CHARS)
        if flattened:
          return flattened
    # Codex serializes item.error as a TOP-LEVEL {"message": ...} object; a
    # success carries "messages", so a top-level "message" is unambiguously an
    # error.
    return _flatten(data.get("message"), MAX_REASON_CHARS)
  # Not JSON. A raw string is a reason ONLY when the tool's own exit proves it
  # failed (Claude MCP errors surface their message as plain content) — never
  # dump raw bytes from a merely-unparseable success.
  if _is_failure_exit(exit_code):
    return _flatten(output, MAX_REASON_CHARS)
  return ""


def _failed(direction: Any, output: Any, exit_code: Any = None) -> dict:
  marker = {"direction": direction, "status": STATUS_FAILED}
  reason = _failure_reason(output, exit_code)
  if reason:
    marker["reason"] = reason
  return marker


def _decode_object(value: Any) -> dict | None:
  """Decode one bounded JSON object without recursively chasing wrappers."""
  if isinstance(value, dict):
    return value
  if not isinstance(value, str) or len(value) > MAX_RESULT_ENVELOPE_CHARS:
    return None
  try:
    decoded = json.loads(value)
  except (ValueError, TypeError, RecursionError):
    return None
  return decoded if isinstance(decoded, dict) else None


def _result_payload(output: Any) -> dict | None:
  """Return the direct control result from a Claude or Codex MCP envelope.

  Claude normally supplies the control server's JSON text directly. Codex
  supplies an MCP result object whose authoritative object may be under
  ``structuredContent`` or inside a text content item. Inspect only one wrapper
  layer and a fixed number of content items so malformed provider output cannot
  create an unbounded parse walk.
  """
  root = _decode_object(output)
  if root is None:
    return None
  if isinstance(root.get("messages"), list):
    return root

  for key in ("structuredContent", "structured_content"):
    payload = _decode_object(root.get(key))
    if payload is not None and isinstance(payload.get("messages"), list):
      return payload

  content = root.get("content")
  if not isinstance(content, list):
    return None
  for item in content[:MAX_RESULT_CONTENT_ITEMS]:
    if isinstance(item, str):
      text = item
    elif isinstance(item, dict) and item.get("type") == "text":
      text = item.get("text")
    else:
      continue
    payload = _decode_object(text)
    if payload is not None and isinstance(payload.get("messages"), list):
      return payload
  return None


def _bounded_message_rows(messages: list[Any]) -> list[dict]:
  """Return displayable rows (one per valid message, up to the API page)."""
  rows = []
  for message in messages[:MAX_RESULT_MESSAGES]:
    if not isinstance(message, dict):
      continue
    raw = message.get("body")
    stripped = raw.strip() if isinstance(raw, str) else ""
    body = stripped[:MAX_BODY_CHARS]
    # The platform contract requires a non-empty body. Do not manufacture an
    # expandable card from a malformed empty row.
    if not body:
      continue
    # The card shows the full note (MAX_BODY_CHARS == the platform's note cap);
    # body_truncated only trips on a defensive over-limit note.
    rows.append({
      **message, "body": body, "body_truncated": len(stripped) > MAX_BODY_CHARS,
    })
  return rows


def _safe_count(value: Any, minimum: int = 0) -> int:
  if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
    return min(value, MAX_RESULT_MESSAGES)
  return minimum


def _kind(value: Any) -> str:
  candidate = _clip(value, MAX_KIND_CHARS)
  return candidate if candidate in _KINDS else _DEFAULT_KIND


def bounded_peer_message(value: Any) -> dict | None:
  """Validate and re-bound a persisted marker before read-side projection."""
  if not isinstance(value, dict) or value.get("status") not in _STATUSES:
    return None
  status = value["status"]
  direction = value.get("direction")
  if direction not in (DIRECTION_SEND, DIRECTION_READ):
    return None
  if status in (STATUS_SENDING, STATUS_SENT) and direction != DIRECTION_SEND:
    return None
  if status in (STATUS_READING, STATUS_RECEIVED, STATUS_EMPTY) \
      and direction != DIRECTION_READ:
    return None

  if status == STATUS_SENT:
    peers: list[str] = []
    raw_peers = value.get("peers") if isinstance(value.get("peers"), list) else []
    for raw in raw_peers:
      name = _clip(raw, MAX_NAME_CHARS)
      if name and name not in peers:
        peers.append(name)
      if len(peers) >= MAX_PEER_NOTES:
        break
    return {
      "direction": DIRECTION_SEND,
      "status": STATUS_SENT,
      "peers": peers,
      "count": _safe_count(value.get("count"), len(peers)),
      "kind": _kind(value.get("kind")),
      "body": _clip(value.get("body"), MAX_BODY_CHARS),
      "body_truncated": bool(value.get("body_truncated")),
      "broadcast": bool(value.get("broadcast")),
    }

  if status == STATUS_RECEIVED:
    notes: list[dict] = []
    source = value.get("notes") if isinstance(value.get("notes"), list) else []
    for note in source[:MAX_PEER_NOTES]:
      if not isinstance(note, dict):
        continue
      body = _clip(note.get("body"), MAX_BODY_CHARS)
      if not body:
        continue
      notes.append({
        "sender": _clip(note.get("sender"), MAX_NAME_CHARS),
        "kind": _kind(note.get("kind")),
        "body": body,
        "body_truncated": bool(note.get("body_truncated")),
      })
    return {
      "direction": DIRECTION_READ,
      "status": STATUS_RECEIVED,
      "count": _safe_count(value.get("count"), len(notes)),
      "notes": notes,
    }

  marker = {"direction": direction, "status": status}
  if status == STATUS_FAILED:
    reason = _flatten(value.get("reason"), MAX_REASON_CHARS)
    if reason:
      marker["reason"] = reason
  return marker


def settle_peer_message(
  pending: dict,
  output: Any,
  output_exit_code: Any = None,
) -> dict:
  """Settle a provisional marker from a completed provider tool result."""
  direction = pending.get("direction")
  if direction != DIRECTION_SEND:
    return _failed(direction, output, output_exit_code)
  if _is_failure_exit(output_exit_code):
    return _failed(direction, output, output_exit_code)

  data = _result_payload(output)
  if data is None:
    return _failed(direction, output)
  messages = data.get("messages")
  if not isinstance(messages, list):
    return _failed(direction, output)
  rows = _bounded_message_rows(messages)

  if not rows:
    return _failed(DIRECTION_SEND, output)
  peers: list[str] = []
  recipient_ids: set[str] = set()
  broadcast = bool(data.get("broadcast"))
  for index, message in enumerate(rows):
    if message.get("broadcast"):
      broadcast = True
    name = _clip(message.get("recipient_name"), MAX_NAME_CHARS)
    if name and name not in peers:
      peers.append(name)
    rid = message.get("recipient_chat_id")
    recipient_ids.add(rid if isinstance(rid, str) and rid else f"__row{index}")
  receipt_names = data.get("recipient_names")
  if isinstance(receipt_names, list):
    for raw_name in receipt_names[:MAX_RESULT_MESSAGES]:
      name = _clip(raw_name, MAX_NAME_CHARS)
      if name and name not in peers:
        peers.append(name)
  visible_peers = peers[:MAX_PEER_NOTES]
  # Count distinct recipient CHATS, not deduped display names: two chats can
  # share a title/task key, and an unresolved name must not collapse to one.
  count = _safe_count(
    data.get("recipient_count"),
    max(len(recipient_ids), len(visible_peers)),
  )
  first = rows[0]
  return {
    "direction": DIRECTION_SEND,
    "status": STATUS_SENT,
    "peers": visible_peers,
    "count": count,
    "kind": _kind(first.get("kind")),
    "body": first["body"],
    "body_truncated": bool(first.get("body_truncated")),
    "broadcast": broadcast,
  }


def peer_message_compaction_lines(value: Any) -> list[str]:
  """Render bounded coordination facts for a provider-handoff transcript."""
  marker = bounded_peer_message(value)
  if marker is None:
    return []
  # Every interpolated value is whitespace-flattened: these lines are joined
  # into the provider-handoff transcript as `ROLE: content`, so an untrusted
  # note body must not be able to inject a newline and forge a turn.
  if marker["status"] == STATUS_SENT and marker.get("body"):
    peers = "the scope" if marker.get("broadcast") else ", ".join(
      _flatten(name, MAX_NAME_CHARS) for name in (marker.get("peers") or [])
    )
    destination = peers or "another agent"
    return [
      f"AGENT COORDINATION: sent {marker['kind']} to {destination}: "
      f"{_flatten(marker['body'], MAX_COMPACTION_BODY_CHARS)}"
    ]
  if marker["status"] == STATUS_RECEIVED:
    return [
      f"AGENT COORDINATION: received {note['kind']}"
      f"{' from ' + _flatten(note['sender'], MAX_NAME_CHARS) if note.get('sender') else ''}: "
      f"{_flatten(note['body'], MAX_COMPACTION_BODY_CHARS)}"
      for note in marker.get("notes") or []
    ]
  if marker["status"] == STATUS_FAILED:
    reason = marker.get("reason")
    if reason:
      return [f"AGENT COORDINATION: peer-message call failed: {reason}"]
    return ["AGENT COORDINATION: peer-message call failed"]
  return []
