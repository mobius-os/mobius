"""Recognize the bounded receipts whose tool results end a turn.

A saved owner card and a confirmed closing save are the two turn-ending
results. Each receipt only names an id; the run's sink decides whether this
exact turn produced it.
"""

import json
from typing import Callable


def owner_card_receipt_id(content: object) -> str | None:
  """Return a saved-card id from the small provider result shapes we emit."""
  return _find_receipt(content, _card_id)


def turn_end_receipt_id(content: object) -> str | None:
  """Return the id of a saved-card or closing-save receipt, if any."""
  return _find_receipt(content, lambda value: _card_id(value) or _closing_save_id(value))


def _card_id(value: dict) -> str | None:
  question_id = value.get("question_id")
  state = value.get("state")
  # Ordinary tool results (including Studio status) also carry `state`,
  # often as an object. Require a receipt's string status before set lookup.
  if (
    isinstance(state, str)
    and state in {"waiting_for_owner", "answered"}
    and isinstance(question_id, str)
    and question_id
    and len(question_id) <= 64
    and isinstance(value.get("next_action"), str)
  ):
    return question_id
  return None


def _closing_save_id(value: dict) -> str | None:
  turn_end_id = value.get("turn_end_id")
  if (
    value.get("state") == "saved_turn_ends"
    and isinstance(turn_end_id, str)
    and turn_end_id
    and len(turn_end_id) <= 64
  ):
    return turn_end_id
  return None


def _find_receipt(content: object, match: Callable[[dict], str | None]) -> str | None:
  pending = [content]
  visited = 0
  while pending and visited < 24:
    value = pending.pop()
    visited += 1
    if isinstance(value, str):
      text = value.strip()
      candidates = [text]
      if "\n" in text:
        candidates.extend(
          line.strip() for line in text.splitlines()[-8:] if line.strip()
        )
      for candidate in candidates:
        if (
          not candidate
          or len(candidate) > 32_768
          or candidate[0] not in "[{"
        ):
          continue
        try:
          pending.append(json.loads(candidate))
        except (json.JSONDecodeError, RecursionError):
          pass
      continue
    if isinstance(value, list):
      pending.extend(value[:12])
      continue
    if not isinstance(value, dict):
      continue
    found = match(value)
    if found is not None:
      return found
    if value.get("isError") is True:
      continue
    # `stdout` is the Claude CLI's Bash tool_response shape
    # ({"stdout": ..., "stderr": ..., "interrupted": ...}), which the
    # PostToolUse turn-end hook inspects for the helper-script card path.
    for key in (
      "content", "result", "structuredContent", "text", "output", "stdout",
    ):
      nested = value.get(key)
      if isinstance(nested, (dict, list, str)):
        pending.append(nested)
  return None
