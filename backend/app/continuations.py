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

# A later ready boot resumes the logical work that declared the typed Restart
# card. It is separate from generic command/timer waits because its writer
# admission may pass only its own linked owner-input barrier.
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


def continuation_protocol_source(
  *, reason: str, control_id: str, run_token: str,
  source_work_id: str | None = None, goal_id: str | None = None,
) -> dict:
  """Build provider-only input for a typed continuation control.

  This value may enter one provider request but must never be appended to a
  chat transcript or owner pending queue. ChatRun.continuation_json is the
  durable source of the same control identity.
  """
  prompts = {
    "manual": "Resume the interrupted owner work from its saved state.",
    "restart": "Resume the interrupted owner work after the planned server restart.",
    "usage_limit": "Resume the interrupted owner work now that provider usage is available.",
    "memory": "Resume the interrupted owner work now that memory pressure has cleared.",
    "storage": "Resume the interrupted owner work now that storage pressure has cleared.",
    "model_capacity": "Resume the interrupted owner work now that the selected model may be available.",
  }
  source = {
    "role": "user",
    "content": prompts.get(
      reason, "Resume the interrupted owner work from its saved state."
    ),
    "kind": "continuation",
    "continuation_reason": reason,
    "hidden": True,
    "cid": control_id,
    "_run_token": run_token,
  }
  if source_work_id is not None:
    source["source_work_id"] = source_work_id
  if goal_id is not None:
    source["goal_id"] = goal_id
  return source


def continuation_control_envelope(
  *, reason: str, control_id: str,
  source_work_id: str | None = None, goal_id: str | None = None,
  supersedes_run_token: str | None = None,
) -> dict:
  """Return the bounded durable half of a provider-only continuation."""
  envelope = {
    "reason": reason,
    "control_id": control_id,
  }
  if source_work_id is not None:
    envelope["source_work_id"] = source_work_id
  if goal_id is not None:
    envelope["goal_id"] = goal_id
  if supersedes_run_token is not None:
    envelope["supersedes_run_token"] = supersedes_run_token
  return envelope


def manual_continuation_run_token(chat_id: str, control_id: str) -> str:
  """Stable physical identity for an idempotent Resume control."""
  digest = hashlib.sha256(
    f"{chat_id}\0{control_id}".encode("utf-8")
  ).hexdigest()[:48]
  return f"manual-resume-{digest}"


def recovery_reasons_by_run_id(
  db, chat_id: str, run_ids: list[str],
) -> dict[str, str]:
  """Map each recovery run among ``run_ids`` to its continuation reason.

  A physical recovery keeps its control only in ``ChatRun.continuation_json``,
  so chat detail projects the reason onto the answer that run wrote; the shell
  marks why that answer started without a transcript row.
  """
  if not run_ids:
    return {}
  from app import models  # keep this low-level module free of ORM imports
  rows = db.query(models.ChatRun.id, models.ChatRun.continuation_json).filter(
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.id.in_(run_ids),
    models.ChatRun.continuation_json.is_not(None),
  ).all()
  return {
    run_id: control["reason"]
    for run_id, control in rows
    if isinstance(control, dict) and isinstance(control.get("reason"), str)
  }
