"""Pure parsing for owner-authored native Goal commands."""

from __future__ import annotations

import re


def goal_argument(text: str) -> str | None:
  """Return the argument of a complete leading ``/goal`` command."""
  match = re.match(r"^\s*/goal(?:\s+([\s\S]+))?\s*$", text or "")
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
