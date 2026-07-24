"""Read-side projection for immutable chat history plus the live turn."""

from __future__ import annotations


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
