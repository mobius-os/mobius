"""Recognize the bounded receipt returned after a saved owner card is made."""

import json


def owner_card_receipt_id(content: object) -> str | None:
  """Return a saved-card id from the small provider result shapes we emit."""
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
    question_id = value.get("question_id")
    if (
      value.get("state") in {"waiting_for_owner", "answered"}
      and isinstance(question_id, str)
      and question_id
      and len(question_id) <= 64
      and isinstance(value.get("next_action"), str)
    ):
      return question_id
    if value.get("isError") is True:
      continue
    for key in ("content", "result", "structuredContent", "text", "output"):
      nested = value.get(key)
      if isinstance(nested, (dict, list, str)):
        pending.append(nested)
  return None
