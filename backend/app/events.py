"""Event processing for agent chat responses.

Pure data transforms that accumulate streaming events into the
assistant message structure.  No I/O — extracted from chat.py for
testability and clarity.

Tool events for a single tool MUST arrive in the order tool_start,
optional tool_input, tool_output, tool_end, with no events for other
tools interleaved between them.
"""

from typing import Literal


EventType = Literal[
  "text",
  "tool_start",
  "tool_input",
  "tool_output",
  "tool_end",
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
})


def process_event(event: dict, assistant_blocks: list) -> bool:
  """Accumulates a parsed event into the assistant blocks list.

  Updates assistant_blocks in place with text content, tool starts,
  tool input/output, and tool completion markers.  Returns True if the
  blocks changed and a DB save may be warranted.
  """
  event_type = event.get("type")

  if event_type == "text":
    content = event.get("content", "")
    # Append to last text block or create new one.
    if (assistant_blocks
        and assistant_blocks[-1].get("type") == "text"):
      assistant_blocks[-1]["content"] += content
    else:
      assistant_blocks.append(
        {"type": "text", "content": content}
      )
    return True

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
    questions = event.get("questions", [])
    new_block = {"type": "question", "questions": questions}
    key = question_block_key(new_block)
    for i, existing in enumerate(assistant_blocks):
      if (existing.get("type") == "question"
          and question_block_key(existing) == key):
        existing["questions"] = questions
        return True
    assistant_blocks.append(new_block)
    return True

  return False


def question_block_key(block: dict) -> tuple:
  """Stable identity for an AskUserQuestion call across partial events.

  Two question blocks compare equal iff they represent the same
  AskUserQuestion invocation. Prefer the SDK-assigned id (Claude
  and Codex both supply one); fall back to the first question's
  text so a defensive runner that omits ids still dedups correctly.

  The first question is enough — a single AskUserQuestion call can
  carry multiple sub-questions, but their order and first member
  are stable across the partial-message stream while the trailing
  list grows progressively.
  """
  questions = block.get("questions") or []
  if not questions:
    return ("empty",)
  first = questions[0] or {}
  if first.get("id"):
    return ("id", first["id"])
  return ("text", first.get("question") or first.get("text") or "")


def build_assistant_message(
  assistant_blocks: list,
) -> dict:
  """Converts accumulated blocks into a message dict for DB storage."""
  all_text = "".join(
    b["content"] for b in assistant_blocks
    if b.get("type") == "text"
  )
  return {
    "role": "assistant",
    "content": all_text,
    "blocks": assistant_blocks,
  }


def finalize_blocks(assistant_blocks: list) -> None:
  """Force-completes any tool blocks still marked as running."""
  for blk in assistant_blocks:
    if (blk.get("type") == "tool"
        and blk.get("status") == "running"):
      blk["status"] = "done"
