"""Pure parsing for owner-authored native Goal commands."""

from __future__ import annotations

import re


def _goal_match(text: str) -> re.Match[str] | None:
  """Match the same complete command boundary used by native dispatch."""
  normalized = (text or "").lstrip("\n")
  return re.fullmatch(r"/goal(?:\s+([\s\S]*))?", normalized)


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
