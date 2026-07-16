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
