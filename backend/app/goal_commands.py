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
  """Recognize only complete owner-visible aliases of the Continue control.

  This is not fuzzy intent classification: unrelated text stays an ordinary
  turn, and pending owner questions or armed Waits still block attachment.
  """
  normalized = strip_upload_augmentation(text or "").strip().lower()
  normalized = re.sub(r"[.!]+$", "", normalized).strip()
  normalized = re.sub(r"\s+", " ", normalized)
  return normalized in {
    "continue please", "keep going", "keep going please",
    "please continue", "please keep going",
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


def goal_request_for_agent(text: str) -> str:
  """The agent's copy of an owner ``/goal <objective>`` message.

  Möbius has already turned the command into its Goal, so the agent receives
  the objective as a plain request. A provider must never see ``/goal`` as its
  own slash command: Claude's CLI has a native ``/goal`` that would echo the
  hidden context Möbius appends and arm a second, parallel goal loop.
  """
  if goal_objective(text) is None:
    return text
  return re.sub(r"^\n*/goal\b", "Goal:", text, count=1)
