"""Read-side projection for immutable chat history plus the live turn."""

from __future__ import annotations

import re


_QUESTION_TOOLS = {"AskUserQuestion", "request_user_input"}
_IMAGE_PATH_RE = re.compile(
  r"\.(?:avif|bmp|gif|heic|heif|jpe?g|png|svg|webp)(?:[?#].*)?$",
  re.IGNORECASE,
)
_MAX_COMPACT_SOURCES = 24
MAX_ACTIVITY_DETAIL_BLOCKS = 2000


def materialized_messages(chat) -> list[dict]:
  """Return history with a visible in-flight assistant snapshot overlaid."""
  messages = list(chat.messages or [])
  live = chat.live_assistant
  if not isinstance(live, dict) or live.get("role") != "assistant":
    return messages
  blocks = live.get("blocks")
  # StartTurn allocates a stable timestamp with an empty stub; do not expose a
  # blank assistant row before the provider emits content.
  if not isinstance(blocks, list) or not blocks:
    return messages
  if messages and messages[-1].get("role") == "assistant":
    messages[-1] = live
  else:
    messages.append(live)
  return messages


def project_messages_for_detail(
  messages: list[dict],
  *,
  fetchable_tool_output_ids: set[str],
  live_message: dict | None = None,
) -> list[dict]:
  """Remove non-live large-output excerpts that already have a sidecar.

  Large tool output is stored once in ``tool_outputs`` and exposed lazily when
  its disclosure opens.  The current live assistant message keeps its bounded
  excerpts so the streaming surface stays self-contained.  Historical messages
  do not need to repeat those excerpts merely because a newer turn is running.

  Copy only messages and blocks that change.  This keeps the persisted JSON and
  live-assistant snapshot immutable, and preserves the common small-chat path.
  A legacy truncated block without a stable sidecar key stays inline.
  """
  projected: list[dict] | None = None
  for message_index, message in enumerate(messages):
    if message is live_message:
      continue
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
      continue

    next_blocks: list[dict] | None = None
    for block_index, block in enumerate(blocks):
      if not (
        isinstance(block, dict)
        and block.get("type") == "tool"
        and block.get("output_truncated") is True
        and isinstance(block.get("tool_use_id"), str)
        and block["tool_use_id"]
        and block["tool_use_id"] in fetchable_tool_output_ids
        and "output" in block
      ):
        continue

      if next_blocks is None:
        next_blocks = list(blocks)
      next_block = dict(block)
      next_block.pop("output", None)
      next_blocks[block_index] = next_block

    if next_blocks is None:
      continue
    if projected is None:
      projected = list(messages)
    next_message = dict(message)
    next_message["blocks"] = next_blocks
    projected[message_index] = next_message

  return projected if projected is not None else messages


def historical_tool_output_ids(
  messages: list[dict],
  *,
  live_message: dict | None = None,
) -> set[str]:
  """Return stable tool ids whose historical excerpts could be projected."""
  ids: set[str] = set()
  for message in messages:
    if message is live_message:
      continue
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
      continue
    for block in blocks:
      if not (
        isinstance(block, dict)
        and block.get("type") == "tool"
        and block.get("output_truncated") is True
        and isinstance(block.get("tool_use_id"), str)
        and block["tool_use_id"]
        and "output" in block
      ):
        continue
      ids.add(block["tool_use_id"])
  return ids


def _distinctive_activity(block: dict) -> bool:
  """Keep notable one-line activity beats out of a folded metadata run."""
  if block.get("type") != "tool" or block.get("tool") != "Read":
    return False
  raw = block.get("input")
  if isinstance(raw, dict):
    raw = raw.get("file_path") or raw.get("path") or ""
  return isinstance(raw, str) and bool(_IMAGE_PATH_RE.search(raw))


def _compact_activity_item(block: dict) -> dict:
  """Return only the metadata needed to paint a collapsed activity line."""
  if block.get("type") == "thinking":
    return {
      "type": "thinking",
      **(
        {"thinking_id": block["thinking_id"]}
        if isinstance(block.get("thinking_id"), str)
        and block["thinking_id"]
        else {}
      ),
      **(
        {"duration_ms": block["duration_ms"]}
        if isinstance(block.get("duration_ms"), (int, float))
        else {}
      ),
    }

  tool = {
    "type": "tool",
    "tool": block.get("tool") or "Tool",
    # This projection never touches the live assistant. A stale persisted
    # "running" flag belongs to an interrupted historical step, not current
    # liveness, so normalize it at the read boundary.
    "status": (
      "done" if block.get("status") == "running"
      else block.get("status") or "done"
    ),
  }
  for key in ("tool_use_id", "output_exit_code", "subagent"):
    if key in block:
      tool[key] = block[key]
  # Read's path is the only input that affects the collapsed presentation:
  # image reads are intentionally a distinctive beat. Keep it bounded; full
  # tool input remains in the on-demand activity detail.
  if block.get("tool") == "Read" and isinstance(block.get("input"), str):
    tool["input"] = block["input"][:2048]
  return tool


def _compact_activity_entries(
  blocks: list[tuple[int, dict]],
) -> list[dict]:
  """Bound header metadata by activity variety, not raw step count.

  Repeated shell/edit loops are the pathological transcripts this projection
  exists for. The collapsed label needs first-seen activity order and whether
  an activity occurred once or repeatedly, so two entries per tool name are
  sufficient. Keep every helper-bearing or failed entry because those facts
  remain visible while collapsed. Thinking contributes one entry with the
  run's total measured duration.
  """
  entries: list[dict] = []
  tool_counts: dict[str, int] = {}
  first_thinking_entry: dict | None = None
  thinking_duration = 0.0
  has_thinking_duration = False

  for raw_index, block in blocks:
    if block.get("type") == "thinking":
      duration = block.get("duration_ms")
      if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        thinking_duration += duration
        has_thinking_duration = True
      if first_thinking_entry is None:
        first_thinking_entry = {
          "item": _compact_activity_item(block),
          "idx": raw_index,
        }
        entries.append(first_thinking_entry)
      continue

    tool_name = block.get("tool") or "Tool"
    occurrence = tool_counts.get(tool_name, 0) + 1
    tool_counts[tool_name] = occurrence
    has_helpers = isinstance(block.get("subagent"), dict) and block["subagent"]
    exit_code = block.get("output_exit_code")
    failed = (
      isinstance(exit_code, (int, float))
      and not isinstance(exit_code, bool)
      and exit_code != 0
    )
    if occurrence <= 2 or has_helpers or failed:
      entries.append({
        "item": _compact_activity_item(block),
        "idx": raw_index,
      })

  if first_thinking_entry is not None:
    item = first_thinking_entry["item"]
    if has_thinking_duration:
      item["duration_ms"] = thinking_duration
    else:
      item.pop("duration_ms", None)
  return entries


def _compact_activity_run(
  blocks: list[tuple[int, dict]],
  *,
  message_index: int,
) -> dict:
  sources: list[dict] = []
  seen_source_urls: set[str] = set()
  for _, block in blocks:
    for source in block.get("sources") or []:
      if not isinstance(source, dict):
        continue
      url = source.get("url")
      if not isinstance(url, str) or not url or url in seen_source_urls:
        continue
      seen_source_urls.add(url)
      sources.append(source)
      if len(sources) >= _MAX_COMPACT_SOURCES:
        break
    if len(sources) >= _MAX_COMPACT_SOURCES:
      break

  start = blocks[0][0]
  end = blocks[-1][0] + 1
  return {
    "type": "activity",
    "activity_id": f"{message_index}:{start}:{end}",
    "message_index": message_index,
    "start": start,
    "end": end,
    "entries": _compact_activity_entries(blocks),
    "tool_count": sum(
      block.get("type") == "tool" for _, block in blocks
    ),
    **({"sources": sources} if sources else {}),
  }


def compact_messages_for_detail(
  messages: list[dict],
  *,
  message_offset: int,
  live_message: dict | None = None,
) -> list[dict]:
  """Project settled activity runs into small, lazily expandable summaries.

  Stored ``Chat.messages`` remains the full source of truth. The normal chat
  read only needs prose, cards, and the metadata that paints each collapsed
  activity header. Expanding a header reads its original block range through
  the activity-detail endpoint.

  Single activity entries stay inline: introducing a network boundary for one
  ordinary tool/thought would cost more complexity than it saves. Distinctive
  image-view beats also stay independent, preserving the transcript's visual
  punctuation. Question-tool twins are omitted when the message already owns
  the canonical question card, matching the frontend's historical repair.
  """
  projected: list[dict] | None = None
  for page_index, message in enumerate(messages):
    if message is live_message or message.get("role") != "assistant":
      continue
    blocks = message.get("blocks")
    if not isinstance(blocks, list) or len(blocks) < 2:
      continue

    has_question = any(
      isinstance(block, dict) and block.get("type") == "question"
      for block in blocks
    )
    next_blocks: list[dict] = []
    run: list[tuple[int, dict]] = []
    changed = False

    def flush() -> None:
      nonlocal changed
      while run:
        chunk = run[:MAX_ACTIVITY_DETAIL_BLOCKS]
        del run[:MAX_ACTIVITY_DETAIL_BLOCKS]
        if len(chunk) >= 2:
          next_blocks.append(_compact_activity_run(
            chunk,
            message_index=message_offset + page_index,
          ))
          changed = True
        else:
          next_blocks.extend(block for _, block in chunk)
      run.clear()

    for raw_index, block in enumerate(blocks):
      activity = (
        isinstance(block, dict)
        and block.get("type") in {"tool", "thinking"}
      )
      question_twin = (
        activity
        and block.get("type") == "tool"
        and has_question
        and block.get("tool") in _QUESTION_TOOLS
      )
      if question_twin:
        flush()
        changed = True
        continue
      if activity and not _distinctive_activity(block):
        run.append((raw_index, block))
        continue
      flush()
      next_blocks.append(block)
    flush()

    if not changed:
      continue
    if projected is None:
      projected = list(messages)
    next_message = dict(message)
    next_message["blocks"] = next_blocks
    # Assistant content duplicates its text blocks. Once blocks are present,
    # copying and rendering already read those blocks, so the duplicate string
    # only inflates parse/cache cost.
    next_message.pop("content", None)
    projected[page_index] = next_message

  return projected if projected is not None else messages
