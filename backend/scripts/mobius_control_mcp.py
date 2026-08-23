#!/usr/bin/env python3
"""Stdio MCP server for run-bound Möbius control-plane operations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from mcp.server.fastmcp import FastMCP


def _goal_promote_module() -> ModuleType:
  path = Path(__file__).with_name("goal_promote.py")
  spec = importlib.util.spec_from_file_location("mobius_goal_promote", path)
  if spec is None or spec.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError("goal promotion helper is unavailable")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


_GOALS = _goal_promote_module()
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


if __name__ == "__main__":
  server.run(transport="stdio")
