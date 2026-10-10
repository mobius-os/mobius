"""Recognize the bounded receipts whose tool results end a turn.

A saved owner card and a helper's recorded question are the two turn-ending
results. Each receipt only names an id; the run's sink decides whether this
exact turn produced it.
"""

import json
from typing import Callable

_MAX_RECEIPT_TEXT = 32_768
_MAX_VISITED = 24
_MAX_LIST_ITEMS = 12
_MAX_FRAMES = 8


def owner_card_receipt_id(content: object) -> str | None:
  """Return a saved-card id from the small provider result shapes we emit."""
  return _find_receipt(content, _card_id)


def turn_end_receipt_id(content: object) -> str | None:
  """Return the id of a saved-card or recorded-question turn_end receipt, if any."""
  return _find_receipt(content, lambda value: _card_id(value) or _recorded_turn_end_id(value))


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
    # Receipt ids follow the turn-end route's protocol identifier bound.
    and len(question_id) <= 64
    and isinstance(value.get("next_action"), str)
  ):
    return question_id
  return None


def _recorded_turn_end_id(value: dict) -> str | None:
  turn_end_id = value.get("turn_end_id")
  if (
    value.get("state") == "turn_end"
    and isinstance(turn_end_id, str)
    and turn_end_id
    # Receipt ids follow the turn-end route's protocol identifier bound.
    and len(turn_end_id) <= 64
  ):
    return turn_end_id
  return None


def _find_receipt(content: object, match: Callable[[dict], str | None]) -> str | None:
  pending = [content]
  visited = 0
  while pending and visited < _MAX_VISITED:
    value = pending.pop()
    visited += 1
    if isinstance(value, str):
      if len(value) > _MAX_RECEIPT_TEXT:
        continue
      text = value.strip()
      decoder = json.JSONDecoder()
      frames = 0
      while text and frames < _MAX_FRAMES:
        frames += 1
        if text[:1] in {"{", "["}:
          try:
            record, end = decoder.raw_decode(text)
          except (json.JSONDecodeError, RecursionError):
            pass
          else:
            # Consume the WHOLE framed record, including nested lines. A
            # parsed refusal envelope must never leak a nested receipt.
            pending.append(record)
            text = text[end:].lstrip()
            continue
        # Tool wrappers may surround complete JSON records with status lines.
        _, separator, text = text.partition("\n")
        text = text.lstrip() if separator else ""
      continue
    if isinstance(value, list):
      pending.extend(value[:_MAX_LIST_ITEMS])
      continue
    if not isinstance(value, dict):
      continue
    if (value.get("isError") is True or value.get("is_error") is True
        or value.get("success") is False or value.get("interrupted") is True):
      continue
    found = match(value)
    if found is not None:
      return found
    # `stdout` is the Claude CLI's Bash tool_response shape
    # ({"stdout": ..., "stderr": ..., "interrupted": ...}), which the
    # PostToolUse card-end hook inspects for the helper-script card path.
    for key in (
      "content", "result", "structuredContent", "text", "output", "stdout",
    ):
      nested = value.get(key)
      if isinstance(nested, (dict, list, str)):
        pending.append(nested)
  return None
