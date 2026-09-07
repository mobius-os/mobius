"""Provider-neutral tool configuration owned by the Möbius platform.

Remote connectors are optional owner capabilities. These local tools are a
different class: run-bound control primitives plus one provider-neutral peer
network shared by top-level chats and durable delegated agents.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


CONTROL_SERVER_NAME = "mobius_control"
GOAL_TOOL_NAME = "promote_goal"
WAIT_TOOL_NAME = "declare_wait"
CANCEL_WAIT_TOOL_NAME = "cancel_wait"
APPROVAL_TOOL_NAME = "request_approval"
QUESTION_TOOL_NAME = "request_question"
LIST_PEERS_TOOL_NAME = "list_agent_peers"
SEND_MESSAGE_TOOL_NAME = "send_agent_message"
READ_MESSAGES_TOOL_NAME = "read_agent_messages"
COORDINATION_TOOL_NAMES = (
  LIST_PEERS_TOOL_NAME,
  SEND_MESSAGE_TOOL_NAME,
  READ_MESSAGES_TOOL_NAME,
)
DELEGATED_CONTROL_TOOL_NAMES = COORDINATION_TOOL_NAMES
CONTROL_TOOL_NAMES = (
  GOAL_TOOL_NAME,
  WAIT_TOOL_NAME,
  CANCEL_WAIT_TOOL_NAME,
  APPROVAL_TOOL_NAME,
  QUESTION_TOOL_NAME,
  *COORDINATION_TOOL_NAMES,
)
OWNER_CONTROL_TOOL_NAMES = (
  GOAL_TOOL_NAME,
  WAIT_TOOL_NAME,
  CANCEL_WAIT_TOOL_NAME,
  APPROVAL_TOOL_NAME,
  QUESTION_TOOL_NAME,
)
CONTROL_ENV_VARS = (
  "API_BASE_URL",
  "AGENT_TOKEN",
  "CHAT_ID",
  "MOBIUS_RUN_TOKEN",
  "MOBIUS_COORDINATION_ENABLED",
)


def _control_script() -> str:
  return str(
    Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  )


def expected_control_tool_names(
  *, top_level: bool, coordination_enabled: bool = True,
) -> tuple[str, ...]:
  """Tools the local server advertises for this agent authority level."""
  if not top_level:
    return DELEGATED_CONTROL_TOOL_NAMES
  if coordination_enabled:
    return CONTROL_TOOL_NAMES
  return OWNER_CONTROL_TOOL_NAMES


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
  top_level: bool = True,
  coordination_enabled: bool = True,
) -> dict[str, Any] | None:
  """Merge local control tools with one detached Codex connector snapshot."""
  servers: dict[str, Any] = {}
  if connector_plan is not None and connector_plan.codex_config:
    configured = connector_plan.codex_config.get("mcp_servers")
    if isinstance(configured, dict):
      servers.update(configured)
  if control_enabled:
    tool_names = expected_control_tool_names(
      top_level=top_level,
      coordination_enabled=coordination_enabled,
    )
    servers[CONTROL_SERVER_NAME] = {
      "command": sys.executable,
      "args": [_control_script()],
      # Delegated Codex turns deliberately use ApprovalMode.deny_all so a
      # child can never escape its sandbox.  Codex applies that same policy to
      # MCP calls unless the server config explicitly pre-approves them.  The
      # control server is a platform-owned, run-bound capability whose own
      # routes enforce exact chat/run authority, so approve only today's
      # named primitives rather than setting a blanket server default that
      # would silently bless a future tool.
      "tools": {
        name: {"approval_mode": "approve"}
        for name in tool_names
      },
      # Codex intentionally starts stdio MCP children with a minimal
      # environment. Forward only the run-bound names this trusted local
      # control needs; unlike an `env` mapping, `env_vars` keeps their values
      # out of thread configuration and process arguments.
      "env_vars": list(CONTROL_ENV_VARS),
      "startup_timeout_sec": 30,
    }
  return {"mcp_servers": servers} if servers else None
