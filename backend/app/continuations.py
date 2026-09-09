"""Shared semantics for durable synthetic chat-continuation markers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
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

# Exact loaded-source activation resumes the logical work that declared the
# typed Restart card. It is separate from generic command/timer waits because
# its writer admission may pass only its own linked owner-input barrier.
PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND = "platform_activation_result"

PRODUCT_RESULT_MESSAGE_KINDS = frozenset({
  DELEGATION_RESULT_MESSAGE_KIND,
  WAIT_RESULT_MESSAGE_KIND,
  PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND,
})

# A peer's direct delivery=interrupt waking an idle chat with a paused
# Goal travels the same slot and resumes under that Goal's identity.
PEER_MESSAGE_WAKE_KIND = "peer_message"


def pending_message_group_key(message: Mapping[str, Any]) -> tuple:
  """Return the causal turn boundary for one queued message."""
  kind = message.get("kind")
  key = (
    bool(message.get("hidden")), kind, message.get("source_work_id"),
  )
  # Each product row already coalesces its own domain batch and owns one
  # independent delivery latch. Never merge two such durable receipts.
  if kind in PRODUCT_RESULT_MESSAGE_KINDS:
    return (*key, message.get("cid"))
  return key


def product_result_run_token(
  chat_id: str, message: Mapping[str, Any],
) -> str | None:
  """Return one stable physical identity for a queued product result."""
  kind = message.get("kind")
  source_work_id = message.get("source_work_id")
  cid = message.get("cid")
  explicit = message.get("_product_run_token")
  if (
    kind not in PRODUCT_RESULT_MESSAGE_KINDS
    or not isinstance(cid, str) or not cid
  ):
    return None
  if kind == WAIT_RESULT_MESSAGE_KIND and cid.startswith("wait-result-"):
    return f"wait-resume-{cid.removeprefix('wait-result-')}"
  if isinstance(explicit, str) and explicit:
    return explicit
  if not isinstance(source_work_id, str) or not source_work_id:
    return None
  digest = hashlib.sha256(
    "\0".join((chat_id, kind, source_work_id, cid)).encode("utf-8")
  ).hexdigest()[:48]
  return f"product-result-{digest}"


def continues_logical_root(message: Mapping[str, Any] | None) -> bool:
  """Whether a product-generated message continues its originating work."""
  return bool(
    isinstance(message, Mapping)
    and message.get("kind") in (
      *CONTINUATION_MESSAGE_KINDS,
      DELEGATION_RESULT_MESSAGE_KIND,
      WAIT_RESULT_MESSAGE_KIND,
      PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND,
    )
  )


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
