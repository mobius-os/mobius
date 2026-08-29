#!/usr/bin/env python3
"""Stdio MCP server for run-bound Möbius control-plane operations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from mcp.server.fastmcp import FastMCP


def _script_module(filename: str, module_name: str) -> ModuleType:
  path = Path(__file__).with_name(filename)
  spec = importlib.util.spec_from_file_location(module_name, path)
  if spec is None or spec.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError(f"{filename} is unavailable")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


_GOALS = _script_module("goal_promote.py", "mobius_goal_promote")
_WAITS = _script_module("chat_wait.py", "mobius_chat_wait")
server = FastMCP(
  "Möbius control",
  instructions=(
    "Run-bound platform controls for the current top-level owner turn. "
    "Never use these tools from a delegated child."
  ),
  log_level="ERROR",
)


def _promote_goal(objective: str) -> dict:
  try:
    payload = _GOALS.promote_goal(objective)
  except SystemExit as exc:
    raise RuntimeError(str(exc)) from exc
  return {
    "state": payload["state"],
    "objective": payload["objective"],
    "goal_id": payload["root_run_id"],
    "run_id": payload["run_id"],
  }


def _declare_wait(
  description: str,
  *,
  command: str | None = None,
  delay_secs: int | None = None,
  interval_secs: int | None = None,
  deadline_secs: int | None = None,
) -> dict:
  try:
    return _WAITS.declare_wait(
      description,
      command=command,
      delay_secs=delay_secs,
      interval_secs=interval_secs,
      deadline_secs=deadline_secs,
    )
  except SystemExit as exc:
    raise RuntimeError(str(exc)) from exc


@server.tool(
  name="promote_goal",
  description=(
    "Promote the current ordinary top-level owner turn into a durable, "
    "platform-owned Goal after the goal-planning criteria are satisfied. "
    "Use at task start or when an owner choice, investigation, or discovery "
    "turns bounded work into a multi-stage outcome. Do not use for questions, "
    "honest one-turn work, or delegated children."
  ),
)
def promote_goal(objective: str) -> dict:
  """Promote and verify the current physical run with one concise objective."""
  return _promote_goal(objective)


@server.tool(
  name="declare_wait",
  description=(
    "Persist a cross-turn wait so this chat resumes automatically after an "
    "external condition or timer, including across server restarts. Call this "
    "before ending a turn whenever you promise to continue later and no "
    "provider-native task already owns that lifecycle. Supply exactly one of "
    "command or delay_secs. A command must be a read-only check: exit 0 means "
    "met, silent exit 1 means not yet, and any other result wakes the chat as "
    "a failed check. Timers and polling intervals have a 60-second minimum. "
    "The default interval is 300 seconds; the default deadline is one day and "
    "the maximum is seven days. A deadline always wakes the chat instead of "
    "leaving it stuck forever."
  ),
)
def declare_wait(
  description: str,
  command: str | None = None,
  delay_secs: int | None = None,
  interval_secs: int | None = None,
  deadline_secs: int | None = None,
) -> dict:
  """Declare one durable command or timer wait for the current chat."""
  return _declare_wait(
    description,
    command=command,
    delay_secs=delay_secs,
    interval_secs=interval_secs,
    deadline_secs=deadline_secs,
  )


if __name__ == "__main__":
  server.run(transport="stdio")
