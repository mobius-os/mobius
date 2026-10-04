"""Shared semantics for durable synthetic chat-continuation markers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any

from app.chat_message_identity import assistant_message_run_id


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

GOAL_SETTLEMENT_UNFINISHED_MESSAGE = (
  "The Goal is still unfinished: the agent ended without recording an "
  "outcome or saving its next handoff. Automatic settlement cannot safely "
  "continue. Resume to recover this execution; "
  "the Goal has not been declared impossible."
)


def recovery_attempted(db, run, *, reason: str) -> bool:
  """Fail closed when recovery was used or its exact lineage cannot be proven.

  Manual owner Resume resets the budget. A broken chain is not evidence that
  an attempt happened, only that another automatic attempt is not justified.
  """
  from app import models
  seen = set()
  while run is not None:
    if run.id in seen:
      return True
    seen.add(run.id)
    control = run.continuation_json or {}
    if control.get("reason") == reason:
      return True
    if control.get("reason") == "manual":
      return False
    predecessor = control.get("supersedes_run_token")
    if not predecessor:
      return False
    previous = db.get(models.ChatRun, predecessor)
    if previous is None or previous.chat_id != run.chat_id or previous.goal_id != run.goal_id:
      return True
    run = previous
  return False


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


def is_retired_goal_handoff(message: Mapping[str, Any] | None) -> bool:
  """A queued automatic-Goal control the pre-2026-09-27 writer left behind.

  The former revision-budget loop is retired. Such a row may still sit behind
  owner input; it must not become the new bounded settlement recovery.
  """
  return continuation_reason(message) == "goal_handoff"


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
    "usage_limit": "Resume the interrupted owner work after a provider-limit check. Provider availability is not yet confirmed.",
    "memory": "Resume the interrupted owner work now that memory pressure has cleared.",
    "storage": "Resume the interrupted owner work now that storage pressure has cleared.",
    "compaction": (
      "The oversized provider session was replaced using the saved detailed handoff "
      "and uncovered conversation. Continue the interrupted work from that briefing, "
      "checking existing results before repeating any actions. This is the only "
      "automatic size-recovery attempt for this logical turn."
    ),
    "model_capacity": "Resume the interrupted owner work now that the selected model may be available.",
    "goal_settlement": (
      "The exact Goal remains open after a clean execution ended without an outcome or durable handoff. "
      "This is one targeted settlement recovery, not permission to redo verified work or shrink the objective. "
      "Read the saved Goal and reconcile its checklist. Complete only if the original promised outcome is verified. "
      "Otherwise continue necessary authorized work in this run, or save a concrete owner question/approval "
      "with instructions and meaningful choices. If unreachable, explain why, efforts and partial results, "
      "and seek actionable owner input before declaring Cannot complete. Use a durable Wait/helper only "
      "when it actually owns continuation. Do not end with optional Unpause or a prose promise. "
      "This recovery will not automatically repeat."
    ),
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
  goal_revision: int | None = None,
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
  if goal_revision is not None:
    envelope["goal_revision"] = goal_revision
  return envelope


def manual_resume_matches(control, *, control_id, run_id=None,
                          goal_id=None, goal_revision=None):
  """A lost Resume receipt can acknowledge only the original exact target."""
  if control.get("control_id") != control_id or control.get("reason") != "manual":
    return False
  if goal_id is not None or "goal_revision" in control:
    return (goal_id == control.get("goal_id")
            and goal_revision == control.get("goal_revision") and run_id is None)
  recorded_run = control.get("supersedes_run_token")
  return run_id is None or recorded_run is None or recorded_run == run_id


def manual_continuation_run_token(chat_id: str, control_id: str) -> str:
  """Stable physical identity for an idempotent Resume control."""
  digest = hashlib.sha256(
    f"{chat_id}\0{control_id}".encode("utf-8")
  ).hexdigest()[:48]
  return f"manual-resume-{digest}"


def recovery_reasons_by_message_index(
  db, chat_id: str, messages: list[dict], *,
  message_start: int = 0, message_end: int | None = None,
) -> dict[int, str]:
  """Mark the first visible answer of each physical recovery, exactly once.

  A physical recovery keeps its control only in ``ChatRun.continuation_json``,
  so chat detail projects the reason without a transcript write. An early
  steer can skip the empty root answer: a valid sink segment then owns the
  notice. Resolve that owner across history before filtering to the page, so
  loading a later segment cannot repeat or move an earlier notice.
  """
  first_answer_by_run: dict[str, int] = {}
  for index, message in enumerate(messages):
    if (not isinstance(message, dict) or message.get("role") != "assistant"
        or message.get("hidden") is True):
      continue
    run_id = assistant_message_run_id(message.get("id"))
    if run_id is not None:
      first_answer_by_run.setdefault(run_id, index)
  candidates = {
    run_id: index for run_id, index in first_answer_by_run.items()
    if index >= message_start and (message_end is None or index < message_end)
  }
  if not candidates:
    return {}
  from app import models  # keep this low-level module free of ORM imports
  rows = db.query(models.ChatRun.id, models.ChatRun.continuation_json).filter(
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.id.in_(candidates),
    models.ChatRun.continuation_json.is_not(None),
  ).all()
  return {
    candidates[run_id]: control["reason"]
    for run_id, control in rows
    if isinstance(control, dict) and isinstance(control.get("reason"), str)
  }
