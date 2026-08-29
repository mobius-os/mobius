"""Provider-neutral Möbius controls are exposed consistently and safely."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import platform_tools


def test_control_server_configs_share_one_script_and_no_secret_arguments():
  claude = platform_tools.claude_control_servers(enabled=True)
  codex = platform_tools.codex_turn_mcp_config(None, control_enabled=True)

  claude_server = claude[platform_tools.CONTROL_SERVER_NAME]
  codex_server = codex["mcp_servers"][platform_tools.CONTROL_SERVER_NAME]
  assert claude_server["command"] == sys.executable
  assert codex_server["command"] == sys.executable
  assert claude_server["args"] == codex_server["args"]
  assert claude_server["args"][0].endswith("/scripts/mobius_control_mcp.py")
  assert "env" not in claude_server
  assert "env" not in codex_server
  assert "env_vars" not in claude_server
  assert codex_server["env_vars"] == list(platform_tools.CONTROL_ENV_VARS)
  assert set(codex_server["env_vars"]) == {
    "API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN",
  }


def test_codex_control_merges_without_mutating_remote_connector_snapshot():
  remote = {"mcp_servers": {"search": {"url": "https://mcp.example/mcp"}}}
  plan = SimpleNamespace(codex_config=remote)

  merged = platform_tools.codex_turn_mcp_config(plan, control_enabled=True)

  assert set(merged["mcp_servers"]) == {"search", "mobius_control"}
  assert remote == {
    "mcp_servers": {"search": {"url": "https://mcp.example/mcp"}},
  }
  assert platform_tools.codex_turn_mcp_config(
    None, control_enabled=False,
  ) is None


def _control_module():
  path = (
    Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  )
  spec = importlib.util.spec_from_file_location("mobius_control_mcp_test", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_promote_goal_tool_returns_verified_platform_identity(monkeypatch):
  control = _control_module()
  monkeypatch.setattr(control._GOALS, "promote_goal", lambda objective: {
    "state": "promoted",
    "objective": objective,
    "root_run_id": "goal-1",
    "run_id": "run-1",
  })

  assert control._promote_goal("Ship and verify") == {
    "state": "promoted",
    "objective": "Ship and verify",
    "goal_id": "goal-1",
    "run_id": "run-1",
  }


def test_promote_goal_tool_preserves_helper_rejection(monkeypatch):
  control = _control_module()

  def reject(_objective):
    raise SystemExit("goal promotion failed: wrong physical run")

  monkeypatch.setattr(control._GOALS, "promote_goal", reject)
  with pytest.raises(RuntimeError, match="wrong physical run"):
    control._promote_goal("Ship and verify")


def test_control_server_advertises_goal_and_durable_wait_tools():
  control = _control_module()

  tools = {
    tool.name: tool for tool in asyncio.run(control.server.list_tools())
  }

  assert set(tools) == {
    platform_tools.GOAL_TOOL_NAME,
    platform_tools.WAIT_TOOL_NAME,
  }
  wait_schema = tools[platform_tools.WAIT_TOOL_NAME].inputSchema
  assert wait_schema["required"] == ["description"]
  assert set(wait_schema["properties"]) == {
    "description",
    "command",
    "delay_secs",
    "interval_secs",
    "deadline_secs",
  }
  assert "server restarts" in tools[platform_tools.WAIT_TOOL_NAME].description
  assert "silent exit 1" in tools[platform_tools.WAIT_TOOL_NAME].description


def test_wait_tool_reuses_the_chat_wait_client(monkeypatch):
  control = _control_module()
  calls = []

  def fake_call(method, path, payload=None):
    calls.append((method, path, payload))
    return {"id": "wait-1", "status": "armed"}

  monkeypatch.setattr(control._WAITS, "_call", fake_call)

  result = control._declare_wait(
    "CI becomes green",
    command="gh pr checks 123 --watch=false >/dev/null",
    interval_secs=120,
    deadline_secs=3600,
  )

  assert result == {"id": "wait-1", "status": "armed"}
  assert calls == [("POST", "/api/chat-waits", {
    "description": "CI becomes green",
    "kind": "command",
    "command": "gh pr checks 123 --watch=false >/dev/null",
    "delay_secs": None,
    "interval_secs": 120,
    "deadline_secs": 3600,
  })]

  with pytest.raises(RuntimeError, match="exactly one"):
    control._declare_wait("ambiguous")
