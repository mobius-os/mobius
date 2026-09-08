"""Pure parsing for owner-authored native Goal commands."""

from __future__ import annotations

import re

from app.chat_message_content import strip_upload_augmentation


def _goal_match(text: str) -> re.Match[str] | None:
  """Match the same complete command boundary used by native dispatch."""
  normalized = strip_upload_augmentation(text or "").lstrip("\n")
  return re.fullmatch(r"/goal(?:\s+([\s\S]*))?", normalized)


def is_goal_continue(text: str) -> bool:
  """Whether owner-visible text is the Goal resume control word."""
  return strip_upload_augmentation(text or "").strip().lower() == "continue"


def is_natural_goal_resume(text: str) -> bool:
  """Whether the whole owner message is an unambiguous resume request.

  This deliberately is not a fuzzy intent classifier.  A Goal may have been
  stopped on purpose, and an unrelated later question must not silently
  inherit it.  These short complete utterances are the conversational spelling
  of the visible Continue control; longer messages stay ordinary turns.
  """
  normalized = strip_upload_augmentation(text or "").strip().lower()
  normalized = re.sub(r"[.!]+$", "", normalized).strip()
  normalized = re.sub(r"\s+", " ", normalized)
  return normalized in {
    "continue please",
    "keep going",
    "keep going please",
    "please continue",
    "please keep going",
  }


def is_goal_command(text: str) -> bool:
  """Whether ``text`` is a complete owner-authored native Goal command."""
  return _goal_match(text) is not None


def goal_argument(text: str) -> str | None:
  """Return the argument of a complete leading ``/goal`` command."""
  match = _goal_match(text)
  if match is None:
    return None
  return (match.group(1) or "").strip() or None


def goal_clear_requested(text: str) -> bool:
  """Whether the command explicitly clears the provider-native Goal."""
  argument = goal_argument(text)
  return bool(argument and argument.lower() == "clear")


def goal_objective(text: str) -> str | None:
  """Return the clean objective from a leading ``/goal`` command."""
  objective = goal_argument(text)
  if objective is None or objective.lower() == "clear":
    return None
  return objective
