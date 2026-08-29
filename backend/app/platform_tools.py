"""Provider-neutral tool configuration owned by the Möbius platform.

Remote connectors are optional owner capabilities.  These local tools are a
different class: small control-plane primitives that every ordinary top-level
agent should see with the same name and contract, regardless of provider.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


CONTROL_SERVER_NAME = "mobius_control"
GOAL_TOOL_NAME = "promote_goal"
WAIT_TOOL_NAME = "declare_wait"
CONTROL_TOOL_NAMES = (GOAL_TOOL_NAME, WAIT_TOOL_NAME)
CONTROL_ENV_VARS = (
  "API_BASE_URL",
  "AGENT_TOKEN",
  "CHAT_ID",
  "MOBIUS_RUN_TOKEN",
)


def _control_script() -> str:
  return str(
    Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  )


def claude_control_servers(*, enabled: bool) -> dict[str, dict[str, Any]]:
  """Return Claude's stdio configuration for ordinary owner turns."""
  if not enabled:
    return {}
  return {
    CONTROL_SERVER_NAME: {
      "type": "stdio",
      "command": sys.executable,
      "args": [_control_script()],
    },
  }


def codex_turn_mcp_config(
  connector_plan: Any | None,
  *,
  control_enabled: bool,
) -> dict[str, Any] | None:
  """Merge local control tools with one detached Codex connector snapshot."""
  servers: dict[str, Any] = {}
  if connector_plan is not None and connector_plan.codex_config:
    configured = connector_plan.codex_config.get("mcp_servers")
    if isinstance(configured, dict):
      servers.update(configured)
  if control_enabled:
    servers[CONTROL_SERVER_NAME] = {
      "command": sys.executable,
      "args": [_control_script()],
      # Codex intentionally starts stdio MCP children with a minimal
      # environment. Forward only the run-bound names this trusted local
      # control needs; unlike an `env` mapping, `env_vars` keeps their values
      # out of thread configuration and process arguments.
      "env_vars": list(CONTROL_ENV_VARS),
      "startup_timeout_sec": 30,
    }
  return {"mcp_servers": servers} if servers else None
