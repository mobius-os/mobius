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
UPDATE_GOAL_TOOL_NAME = "update_goal"
WAIT_TOOL_NAME = "declare_wait"
CANCEL_WAIT_TOOL_NAME = "cancel_wait"
APPROVAL_TOOL_NAME = "request_approval"
QUESTION_TOOL_NAME = "request_question"
RESTART_TOOL_NAME = "request_restart"
CHECKPOINT_CHAT_TOOL_NAME = "checkpoint_chat"
# The control owns this declaration; the MCP listing and quiet-write host
# consume it. Result-bearing controls (including cards and Goal reads) stay
# ordinary tools. Apps opt in through their reviewed tool declarations.
RESULT_INDEPENDENT_META = "mobius/resultIndependent"
RESULT_INDEPENDENT_CONTROL_TOOLS = frozenset({CHECKPOINT_CHAT_TOOL_NAME})
NOTIFY_OWNER_TOOL_NAME = "notify_owner"
OPEN_ITEM_TOOL_NAME = "open_item"
SECRET_TOOL_NAME = "request_secret"
APP_BUILD_TOOL_NAMES = ("list_apps", "apply_app", "screenshot")
LIST_PEERS_TOOL_NAME = "list_agent_peers"
SEND_MESSAGE_TOOL_NAME = "send_agent_message"
CLAIM_WORK_TOOL_NAME = "claim_agent_work"
FINISH_WORK_TOOL_NAME = "finish_agent_work"
SPAWN_AGENT_TOOL_NAME = "spawn_agent"
MESSAGE_AGENT_TOOL_NAME = "message_agent"
STOP_AGENT_TOOL_NAME = "stop_agent"
LIST_AGENTS_TOOL_NAME = "list_agents"
# Codex's native image viewer is switched off in favor of this tool, which
# binds each view to a chat-owned snapshot of the bytes the provider received
# (app/viewed_images.py). Claude's Read result already carries those bytes.
VIEW_IMAGE_TOOL_NAME = "view_image"
# Möbius-owned helpers replace the providers' built-in helper tools for every
# agent, including helpers themselves (nesting).
HELPER_TOOL_NAMES = (
  SPAWN_AGENT_TOOL_NAME,
  MESSAGE_AGENT_TOOL_NAME,
  STOP_AGENT_TOOL_NAME,
  LIST_AGENTS_TOOL_NAME,
)
PEER_TOOL_NAMES = (
  LIST_PEERS_TOOL_NAME,
  SEND_MESSAGE_TOOL_NAME,
)
WORK_OWNERSHIP_TOOL_NAMES = (
  CLAIM_WORK_TOOL_NAME,
  FINISH_WORK_TOOL_NAME,
)
OWNER_CONTROL_TOOL_NAMES = (
  *HELPER_TOOL_NAMES,
  GOAL_TOOL_NAME,
  UPDATE_GOAL_TOOL_NAME,
  WAIT_TOOL_NAME,
  CANCEL_WAIT_TOOL_NAME,
  APPROVAL_TOOL_NAME,
  QUESTION_TOOL_NAME,
  RESTART_TOOL_NAME,
  *WORK_OWNERSHIP_TOOL_NAMES,
  CHECKPOINT_CHAT_TOOL_NAME,
  NOTIFY_OWNER_TOOL_NAME,
  OPEN_ITEM_TOOL_NAME,
  SECRET_TOOL_NAME,
  *APP_BUILD_TOOL_NAMES,
)
DELEGATED_CONTROL_TOOL_NAMES = (
  *HELPER_TOOL_NAMES, *PEER_TOOL_NAMES, *WORK_OWNERSHIP_TOOL_NAMES,
  CHECKPOINT_CHAT_TOOL_NAME, *APP_BUILD_TOOL_NAMES,
)
CONTROL_TOOL_NAMES = (*OWNER_CONTROL_TOOL_NAMES, *PEER_TOOL_NAMES)
# Above app_tools.TOOL_TIMEOUT_SECONDS and the control server's own HTTP wait,
# so the innermost limit is the one that reports.
CONTROL_TOOL_TIMEOUT_SECONDS = 630
CONTROL_ENV_VARS = (
  "API_BASE_URL",
  # view_image stores its snapshot under this chat's media.
  "DATA_DIR",
  "AGENT_TOKEN",
  "CHAT_ID",
  "MOBIUS_RUN_TOKEN",
  "MOBIUS_COORDINATION_ENABLED",
  # spawn_agent defaults a helper to the delegating agent's own provider.
  "MOBIUS_AGENT_PROVIDER",
  "MOBIUS_AGENT_MODEL",
  "MOBIUS_AGENT_EFFORT",
  "MOBIUS_DELEGATION_ID",
  # The screenshot tool captures at the owner's viewport in this chat's
  # browser session.
  "VIEWPORT_WIDTH",
  "VIEWPORT_HEIGHT",
  "VIEWPORT_PIXEL_RATIO",
  "AGENT_BROWSER_SESSION",
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


def codex_control_tool_names(
  *, top_level: bool, coordination_enabled: bool = True,
) -> tuple[str, ...]:
  """The same tools plus view_image, which replaces Codex's native viewer."""
  return (
    *expected_control_tool_names(
      top_level=top_level, coordination_enabled=coordination_enabled,
    ),
    VIEW_IMAGE_TOOL_NAME,
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
      # Milliseconds; the same wall-clock limit Codex gets as tool_timeout_sec.
      "timeout": CONTROL_TOOL_TIMEOUT_SECONDS * 1000,
    },
  }


def codex_turn_mcp_config(
  connector_plan: Any | None,
  *,
  control_enabled: bool,
  top_level: bool = True,
  coordination_enabled: bool = True,
  app_tool_names: tuple[str, ...] = (),
) -> dict[str, Any] | None:
  """Merge local control tools with one detached Codex connector snapshot.

  ``app_tool_names`` are the install-reviewed tools live apps serve through the
  same control server (app/app_tools.py); they are approved by exact name like
  the platform's own primitives. The app still receives ``actor.access`` for
  the published service contract; new helpers use the trusted mode.
  """
  servers: dict[str, Any] = {}
  if connector_plan is not None and connector_plan.codex_config:
    configured = connector_plan.codex_config.get("mcp_servers")
    if isinstance(configured, dict):
      servers.update(configured)
  if control_enabled:
    tool_names = codex_control_tool_names(
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
        for name in (*tool_names, *app_tool_names)
      },
      # A fixed, non-secret switch: the server offers view_image only here.
      "env": {"MOBIUS_IMAGE_VIEWER": "1"},
      # Codex intentionally starts stdio MCP children with a minimal
      # environment. Forward only the run-bound names this trusted local
      # control needs; unlike an `env` mapping, `env_vars` keeps their values
      # out of thread configuration and process arguments.
      "env_vars": list(CONTROL_ENV_VARS),
      "startup_timeout_sec": 30,
      # Codex otherwise abandons any MCP call after 60 seconds; installed-app
      # tools may legitimately run for minutes (app_tools.TOOL_TIMEOUT_SECONDS).
      "tool_timeout_sec": CONTROL_TOOL_TIMEOUT_SECONDS,
    }
  return {"mcp_servers": servers} if servers else None
