"""Shared semantics for durable synthetic chat-continuation markers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# ``auto_continuation`` is the durable legacy value already stored in partner
# transcripts. New writes use the origin-neutral name because a manual Resume
# is the same provider-facing continuation with different product attribution.
CONTINUATION_MESSAGE_KINDS = frozenset({
  "continuation",
  "auto_continuation",
})

# Product-owned child results travel through the ordinary user-message slot so
# both provider transports receive them without a second execution channel.
# They are hidden from the owner transcript and carry their own semantic kind
# so Goal identity, summaries, and recency never mistake them for owner speech.
DELEGATION_RESULT_MESSAGE_KIND = "delegation_result"

# A durable declared wait resuming its own chat travels the same hidden
# user-message slot: product-owned, never owner speech.
WAIT_RESULT_MESSAGE_KIND = "wait_result"


def is_continuation_message(message: Mapping[str, Any] | None) -> bool:
  return bool(
    isinstance(message, Mapping)
    and message.get("kind") in CONTINUATION_MESSAGE_KINDS
  )


def continuation_reason(message: Mapping[str, Any] | None) -> str:
  if not is_continuation_message(message):
    return ""
  reason = str(message.get("continuation_reason") or "").strip()
  if reason:
    return reason
  return "automatic recovery"


def continuation_actor_label(message: Mapping[str, Any] | None) -> str:
  """Return provider/history attribution without treating a marker as speech."""
  reason = continuation_reason(message)
  if reason == "manual":
    return "Manual continuation"
  return f"Automatic continuation ({reason})"
