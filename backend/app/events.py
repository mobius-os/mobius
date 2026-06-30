"""Event processing for agent chat responses.

Pure data transforms that accumulate streaming events into the
assistant message structure.  No I/O — extracted from chat.py for
testability and clarity.

Tool events for a single tool MUST arrive in the order tool_start,
optional tool_input, tool_output, tool_end, with no events for other
tools interleaved between them.
"""

import copy
from dataclasses import dataclass, field
from typing import Literal


EventType = Literal[
  "text",
  "text_boundary",
  "tool_start",
  "tool_input",
  "tool_output",
  "tool_end",
  "skill_loaded",
  "question",
  "queued_turn_starting",
  "catch_up_done",
  "error",
  "done",
  "session_init",
]

SYSTEM_EVENT_TYPES: frozenset[str] = frozenset({
  "theme_updated",
  "app_updated",
  "shell_rebuilding",
  "shell_rebuilt",
  "shell_rebuild_failed",
  "chat_run_started",
  "chat_run_finished",
})


def _join_text_parts(parts: list[str]) -> str:
  """Join persisted text blocks without inventing duplicate whitespace."""
  out = ""
  for content in parts:
    if not content:
      continue
    if not out:
      out = content
    elif out[-1].isspace() or content[0].isspace():
      out += content
    else:
      out += "\n\n" + content
  return out


def process_event(event: dict, assistant_blocks: list) -> bool:
  """Accumulates a parsed event into the assistant blocks list.

  Updates assistant_blocks in place with text content, tool starts,
  tool input/output, and tool completion markers.  Returns True if the
  blocks changed and a DB save may be warranted.
  """
  event_type = event.get("type")

  if event_type == "text":
    content = event.get("content", "")
    # Append to last text block or create new one. A preceding internal
    # text_boundary marker means the provider started a new assistant
    # message item without a visible tool block; replace the marker with
    # the real text block instead of concatenating into the prior text.
    if (assistant_blocks
        and assistant_blocks[-1].get("type") == "text_boundary"):
      assistant_blocks[-1] = {"type": "text", "content": content}
    elif (assistant_blocks
        and assistant_blocks[-1].get("type") == "text"):
      assistant_blocks[-1]["content"] += content
    else:
      assistant_blocks.append(
        {"type": "text", "content": content}
      )
    return True

  if event_type == "text_boundary":
    # Provider streams can contain multiple assistant message items separated
    # by hidden/internal work that Möbius does not render as a tool block.
    # Preserve that provider boundary explicitly so later text starts a fresh
    # paragraph instead of becoming `previous.next`. The marker is internal:
    # build_assistant_message ignores it and finalize_blocks removes any
    # trailing boundary that never received text.
    if (assistant_blocks
        and assistant_blocks[-1].get("type") == "text"
        and assistant_blocks[-1].get("content")):
      assistant_blocks.append({"type": "text_boundary"})
      return True
    return False

  if event_type == "tool_start":
    assistant_blocks.append({
      "type": "tool",
      "tool": event.get("tool", ""),
      "input": event.get("input", ""),
      "output": "",
      "status": "running",
    })
    return True

  if event_type == "tool_input":
    # Backfill input summary from the assistant event (arrives after
    # content_block_start which created the tool block).  Match the
    # earliest tool block without input — the assistant event lists
    # tools in order, matching creation order.
    for blk in assistant_blocks:
      if blk.get("type") == "tool" and not blk.get("input"):
        blk["input"] = event.get("input", "")
        break
    return True

  if event_type == "tool_output":
    for blk in reversed(assistant_blocks):
      if (blk.get("type") == "tool"
          and blk.get("status") != "done"):
        blk["output"] = event.get("content", "")
        break
    return True

  if event_type == "tool_end":
    for blk in reversed(assistant_blocks):
      if (blk.get("type") == "tool"
          and blk.get("status") != "done"):
        blk["status"] = "done"
        break
    return True

  if event_type == "skill_loaded":
    # Skill observability: the runner emits this alongside the Skill
    # tool's tool_start. Stamp the skill name onto the most recent
    # Skill tool block so the persisted transcript carries the chip
    # data (the frontend reads `block.skill`); the same event drives
    # the activity-log append on the runner side. No skill name is a
    # no-op — an empty chip carries no signal.
    skill = event.get("skill") or ""
    if not skill:
      return False
    for blk in reversed(assistant_blocks):
      if blk.get("type") == "tool" and blk.get("tool") == "Skill":
        blk["skill"] = skill
        return True
    return False

  if event_type == "error":
    # Persist the error into the assistant transcript so users see
    # what went wrong when scrolling back. The same event is also
    # broadcast live for active SSE subscribers (the sink handles
    # both). Coalesce: a single error is enough — additional error
    # events on the same turn replace rather than stack.
    message = event.get("message", "") or ""
    if (assistant_blocks
        and assistant_blocks[-1].get("type") == "error"):
      assistant_blocks[-1]["message"] = message
    else:
      assistant_blocks.append({
        "type": "error",
        "message": message,
      })
    return True

  if event_type == "question":
    # Two partial deliveries for the same AskUserQuestion call may
    # straddle other events (a text token or tool boundary often
    # lands between them). Coalesce by stable identity — the SDK-
    # provided question id, falling back to the first question's
    # text — instead of "is the last block a question?". Adjacency-
    # based dedup left duplicate cards when anything interleaved.
    #
    # The runner now publishes a `question_id` (the PendingQuestion's
    # id) on the event. When present, stamp it on the block so the
    # answer routes can match the exact open question by identity
    # (fixing the wrong-block bug when two questions are open at once),
    # and prefer it as the coalescing key — it is the most stable
    # identity for the call, independent of the sub-questions' shape.
    # When absent (legacy/defensive), behaviour is unchanged: no
    # question_id key on the block and dedup by `question_block_key`.
    questions = event.get("questions", [])
    question_id = event.get("question_id")
    new_block = {"type": "question", "questions": questions}
    if question_id:
      new_block["question_id"] = question_id
    key = question_block_key(new_block)
    for i, existing in enumerate(assistant_blocks):
      if (existing.get("type") == "question"
          and question_block_key(existing) == key):
        existing["questions"] = questions
        if question_id:
          existing["question_id"] = question_id
        return True
    assistant_blocks.append(new_block)
    return True

  return False


def question_block_key(block: dict) -> tuple:
  """Stable identity for an AskUserQuestion call across partial events.

  Two question blocks compare equal iff they represent the same
  AskUserQuestion invocation. Prefer the block-level `question_id`
  (the PendingQuestion id the runner now publishes) — it is the most
  stable identity for the call and is unaffected by the sub-questions'
  shape. Fall back to the first sub-question's SDK-assigned id, then
  its text, so a defensive runner that omits the question_id still
  dedups correctly.

  The first question is enough — a single AskUserQuestion call can
  carry multiple sub-questions, but their order and first member
  are stable across the partial-message stream while the trailing
  list grows progressively.
  """
  question_id = block.get("question_id")
  if question_id:
    return ("question_id", question_id)
  questions = block.get("questions") or []
  if not questions:
    return ("empty",)
  first = questions[0] or {}
  if first.get("id"):
    return ("id", first["id"])
  return ("text", first.get("question") or first.get("text") or "")


@dataclass
class QuestionScrubReceipt:
  """What a single `process_event(question)` did to `assistant_blocks`.

  Captured by `capture_question_scrub` BEFORE the event is processed, so a
  failed QuestionCommit can be reverted by EXACT IDENTITY rather than the
  old tail-slice (`del blocks[blocks_before:]`). The slice was wrong on two
  counts: `process_event` may COALESCE the question into a pre-existing
  block (appending nothing, so the slice deletes the wrong thing or
  nothing), and a concurrent same-loop append after `blocks_before` would
  be deleted along with the orphan.

  - `kind == "appended"`: the event will append a NEW question block.
    `undo_question_scrub` removes ONLY that object by Python identity
    (`target_ref`), so prior + later blocks survive.
  - `kind == "coalesced"`: the event will mutate the pre-existing
    `target_ref` block in place. `undo_question_scrub` restores ONLY the
    fields this event touched (`questions`, and `question_id` when the
    event carried one), and ONLY when the field's current value still
    EQUALS what this event wrote — so a later same-loop event that mutated
    the same block again is not clobbered by the revert.
  """

  kind: Literal["appended", "coalesced"]
  target_ref: dict | None = None
  # The exact values this event wrote (used to confirm equality-still-holds
  # before restoring) — only meaningful for the coalesced kind.
  wrote_questions: list | None = None
  wrote_question_id: str | None = None
  # The pre-event values to restore on the coalesced target, with presence
  # flags so a field the block did NOT have before is removed (not set to
  # None) on revert.
  had_questions: bool = False
  prev_questions: list | None = None
  had_question_id: bool = False
  prev_question_id: str | None = None


def capture_question_scrub(
  event: dict, assistant_blocks: list
) -> QuestionScrubReceipt:
  """Capture how `process_event` would handle this question event.

  Replicates `process_event`'s question coalescing identity decision
  WITHOUT mutating `assistant_blocks`: build the candidate block the same
  way, compute its `question_block_key`, and scan for a pre-existing
  question block with the same key.

  - A match → COALESCED: capture the target block by identity plus a deep
    copy of the fields the event will overwrite (`questions`, and
    `question_id` when present) with presence flags.
  - No match → APPENDED: the receipt's `target_ref` is filled in by
    `commit_question_scrub` after `process_event` appends the new object.

  This is the question-specific helper the design calls for — it does NOT
  change `process_event`'s bool contract for ordinary callers.
  """
  questions = event.get("questions", [])
  question_id = event.get("question_id")
  candidate = {"type": "question", "questions": questions}
  if question_id:
    candidate["question_id"] = question_id
  key = question_block_key(candidate)
  for existing in assistant_blocks:
    if (
      existing.get("type") == "question"
      and question_block_key(existing) == key
    ):
      return QuestionScrubReceipt(
        kind="coalesced",
        target_ref=existing,
        wrote_questions=questions,
        wrote_question_id=question_id if question_id else None,
        had_questions="questions" in existing,
        prev_questions=copy.deepcopy(existing.get("questions")),
        had_question_id="question_id" in existing,
        prev_question_id=existing.get("question_id"),
      )
  return QuestionScrubReceipt(kind="appended")


def commit_question_scrub(
  receipt: QuestionScrubReceipt, assistant_blocks: list
) -> None:
  """After `process_event`, bind an APPENDED receipt to the new object.

  For the appended kind, the new question block is the one
  `process_event` just appended — capture it by identity (the last block,
  which is the freshly appended question object) so a later revert removes
  exactly it. The coalesced kind already holds its `target_ref`.
  """
  if receipt.kind == "appended" and assistant_blocks:
    receipt.target_ref = assistant_blocks[-1]


def undo_question_scrub(
  receipt: QuestionScrubReceipt, assistant_blocks: list
) -> None:
  """Revert what `process_event` did, by exact identity (failure path).

  APPENDED → remove ONLY the appended object by Python identity (no slice,
  no stale index) so prior + concurrently-appended later blocks survive.
  COALESCED → restore ONLY the touched fields on the target block, and a
  field ONLY when its current value still EQUALS what this event wrote
  (guards a later same-loop mutation of the same block); a field the block
  did not have before is removed rather than set to None.
  """
  target = receipt.target_ref
  if target is None:
    return
  if receipt.kind == "appended":
    for i, blk in enumerate(assistant_blocks):
      if blk is target:
        del assistant_blocks[i]
        return
    return
  # Coalesced: restore each touched field iff equality still holds.
  if target.get("questions") == receipt.wrote_questions:
    if receipt.had_questions:
      target["questions"] = receipt.prev_questions
    else:
      target.pop("questions", None)
  if receipt.wrote_question_id is not None:
    if target.get("question_id") == receipt.wrote_question_id:
      if receipt.had_question_id:
        target["question_id"] = receipt.prev_question_id
      else:
        target.pop("question_id", None)


def build_assistant_message(
  assistant_blocks: list,
) -> dict:
  """Converts accumulated blocks into a message dict for DB storage."""
  all_text = _join_text_parts([
    b["content"] for b in assistant_blocks
    if b.get("type") == "text" and b.get("content")
  ])
  # Drop the internal `text_boundary` marker from the persisted blocks. It is
  # a live-stream-only signal (the frontend's forceNewTextBlock) and is never
  # a renderable block; the throttled mid-turn PersistTranscript path writes
  # `blocks` as-is (only the terminal finalize_blocks strips it), so without
  # this a mid-turn snapshot could carry the marker into Chat.messages. Filter
  # a copy so the live broadcast still receives the marker.
  return {
    "role": "assistant",
    "content": all_text,
    "blocks": [b for b in assistant_blocks if b.get("type") != "text_boundary"],
  }


def blocks_have_renderable_content(assistant_blocks: list) -> bool:
  """True when the blocks carry something worth sealing as a message.

  A steer can cut over before the assistant produced any real output — the
  only accumulated block is an empty or whitespace-only text token (or just
  the internal `text_boundary` marker). Sealing that produces a stray empty
  assistant message sitting before the steered user row, which reads as an
  orphaned fragment (card 166). A single REAL token ("I ") IS renderable and
  is kept; any non-text block (tool/question/error) is always renderable.
  """
  for blk in assistant_blocks or []:
    btype = blk.get("type")
    if btype == "text_boundary":
      continue
    if btype == "text":
      if str(blk.get("content") or "").strip():
        return True
      continue
    return True
  return False


def finalize_blocks(assistant_blocks: list) -> None:
  """Force-completes running tools and removes unused internal markers."""
  assistant_blocks[:] = [
    blk for blk in assistant_blocks
    if blk.get("type") != "text_boundary"
  ]
  for blk in assistant_blocks:
    if (blk.get("type") == "tool"
        and blk.get("status") == "running"):
      blk["status"] = "done"
