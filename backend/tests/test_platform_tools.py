"""Provider-neutral Möbius controls are exposed consistently and safely."""

import importlib.util
import json
import os
import subprocess
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
    "next_action": (
      "If this outcome has multiple verifiable stages or branches, publish "
      "its Goal plan now. The Goal record does not execute a prose checklist."
    ),
  }


def test_promote_goal_tool_preserves_helper_rejection(monkeypatch):
  control = _control_module()

  def reject(_objective):
    raise SystemExit("goal promotion failed: wrong physical run")

  monkeypatch.setattr(control._GOALS, "promote_goal", reject)
  with pytest.raises(RuntimeError, match="wrong physical run"):
    control._promote_goal("Ship and verify")


def test_control_protocol_advertises_every_run_bound_tool(monkeypatch):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  control = _control_module()

  initialized = control._dispatch_message({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18"},
  })
  assert initialized["result"]["protocolVersion"] == "2025-06-18"
  assert initialized["result"]["capabilities"] == {
    "tools": {"listChanged": False},
  }

  listed = control._dispatch_message({
    "jsonrpc": "2.0", "id": 2, "method": "tools/list",
  })
  tools = {
    tool["name"]: tool for tool in listed["result"]["tools"]
  }
  assert tuple(tools) == platform_tools.CONTROL_TOOL_NAMES
  assert tools[platform_tools.GOAL_TOOL_NAME]["inputSchema"]["required"] == [
    "objective",
  ]
  wait_schema = tools[platform_tools.WAIT_TOOL_NAME]["inputSchema"]
  assert wait_schema["required"] == ["description"]
  assert set(wait_schema["properties"]) == {
    "description",
    "condition_owner",
    "command",
    "delay_secs",
    "interval_secs",
    "deadline_secs",
  }
  assert "question card" in (
    wait_schema["properties"]["condition_owner"]["description"]
  )
  assert wait_schema["additionalProperties"] is False
  assert "server restarts" in tools[platform_tools.WAIT_TOOL_NAME]["description"]
  assert "silent exit 1" in tools[platform_tools.WAIT_TOOL_NAME]["description"]
  assert "Never use a wait for an approval" in (
    tools[platform_tools.WAIT_TOOL_NAME]["description"]
  )
  assert "does not inherit turn-only API credentials" in (
    tools[platform_tools.WAIT_TOOL_NAME]["description"]
  )
  cancel_schema = tools[platform_tools.CANCEL_WAIT_TOOL_NAME]["inputSchema"]
  assert cancel_schema["required"] == ["wait_id"]
  assert set(cancel_schema["properties"]) == {"wait_id"}
  assert set(platform_tools.COORDINATION_TOOL_NAMES) <= set(tools)
  send_schema = tools[platform_tools.SEND_MESSAGE_TOOL_NAME]["inputSchema"]
  assert send_schema["required"] == ["body"]
  assert set(send_schema["properties"]["kind"]["enum"]) == {
    "note", "finding", "request", "blocker", "handoff",
  }


def test_delegated_control_server_advertises_only_coordination(monkeypatch):
  monkeypatch.delenv("MOBIUS_RUN_TOKEN", raising=False)
  control = _control_module()

  listed = control._dispatch_message({
    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
  })
  assert tuple(
    tool["name"] for tool in listed["result"]["tools"]
  ) == platform_tools.DELEGATED_CONTROL_TOOL_NAMES
  assert platform_tools.WAIT_TOOL_NAME not in {
    tool["name"] for tool in listed["result"]["tools"]
  }
  assert platform_tools.CANCEL_WAIT_TOOL_NAME not in {
    tool["name"] for tool in listed["result"]["tools"]
  }
  denied = control._call_tool({
    "name": platform_tools.GOAL_TOOL_NAME,
    "arguments": {"objective": "Escape child scope"},
  })
  assert denied["isError"] is True
  assert "unavailable" in denied["content"][0]["text"]

  denied_wait = control._call_tool({
    "name": platform_tools.WAIT_TOOL_NAME,
    "arguments": {"description": "Escape child lifecycle", "delay_secs": 60},
  })
  assert denied_wait["isError"] is True
  assert "unavailable" in denied_wait["content"][0]["text"]
  denied_cancel = control._call_tool({
    "name": platform_tools.CANCEL_WAIT_TOOL_NAME,
    "arguments": {"wait_id": "wait-1"},
  })
  assert denied_cancel["isError"] is True
  assert "unavailable" in denied_cancel["content"][0]["text"]


def test_control_protocol_returns_tool_success_without_framework_wrapping(
  monkeypatch,
):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  control = _control_module()
  monkeypatch.setattr(control, "_promote_goal", lambda objective: {
    "state": "promoted",
    "objective": objective,
    "goal_id": "goal-1",
    "run_id": "run-1",
    "next_action": (
      "If this outcome has multiple verifiable stages or branches, publish "
      "its Goal plan now. The Goal record does not execute a prose checklist."
    ),
  })

  response = control._dispatch_message({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": platform_tools.GOAL_TOOL_NAME,
      "arguments": {"objective": "  Ship and verify  "},
    },
  })

  result = response["result"]
  assert result["isError"] is False
  assert json.loads(result["content"][0]["text"]) == {
    "state": "promoted",
    "objective": "Ship and verify",
    "goal_id": "goal-1",
    "run_id": "run-1",
    "next_action": (
      "If this outcome has multiple verifiable stages or branches, publish "
      "its Goal plan now. The Goal record does not execute a prose checklist."
    ),
  }


def test_control_protocol_declares_wait_through_the_canonical_client(monkeypatch):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  control = _control_module()
  calls = []

  def fake_call(method, path, payload=None):
    calls.append((method, path, payload))
    return {"id": "wait-1", "status": "armed"}

  monkeypatch.setattr(control._WAITS, "_call", fake_call)
  response = control._dispatch_message({
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
      "name": platform_tools.WAIT_TOOL_NAME,
      "arguments": {
        "description": "  CI becomes green  ",
        "condition_owner": "  GitHub Actions  ",
        "command": "gh pr checks 123 --watch=false >/dev/null",
        "interval_secs": 120,
        "deadline_secs": 3600,
      },
    },
  })

  result = response["result"]
  assert result["isError"] is False
  assert json.loads(result["content"][0]["text"]) == {
    "id": "wait-1", "status": "armed",
  }
  assert calls == [("POST", "/api/chat-waits", {
    "description": "CI becomes green",
    "condition_owner": "GitHub Actions",
    "kind": "command",
    "command": "gh pr checks 123 --watch=false >/dev/null",
    "delay_secs": None,
    "interval_secs": 120,
    "deadline_secs": 3600,
  })]

  invalid = control._call_tool({
    "name": platform_tools.WAIT_TOOL_NAME,
    "arguments": {"description": "ambiguous"},
  })
  assert invalid["isError"] is True
  assert "exactly one" in invalid["content"][0]["text"]


def test_top_level_control_cancels_exact_wait_through_the_canonical_client(
  monkeypatch,
):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  control = _control_module()
  calls = []

  def fake_call(method, path, payload=None):
    calls.append((method, path, payload))
    return {"id": "wait-1", "status": "cancelled"}

  monkeypatch.setattr(control._WAITS, "_call", fake_call)
  response = control._dispatch_message({
    "jsonrpc": "2.0",
    "id": 5,
    "method": "tools/call",
    "params": {
      "name": platform_tools.CANCEL_WAIT_TOOL_NAME,
      "arguments": {"wait_id": "  wait-1  "},
    },
  })

  result = response["result"]
  assert result["isError"] is False
  assert json.loads(result["content"][0]["text"]) == {
    "id": "wait-1", "status": "cancelled",
  }
  assert calls == [("POST", "/api/chat-waits/wait-1/cancel", None)]

  invalid = control._call_tool({
    "name": platform_tools.CANCEL_WAIT_TOOL_NAME,
    "arguments": {"wait_id": ""},
  })
  assert invalid["isError"] is True
  assert "non-empty" in invalid["content"][0]["text"]


def test_coordination_tools_validate_and_reuse_the_in_process_cursor(monkeypatch):
  monkeypatch.delenv("MOBIUS_RUN_TOKEN", raising=False)
  control = _control_module()
  calls = []
  inboxes = [
    {"messages": [{"id": "message-1"}], "cursor": "message-1"},
    {"messages": [{"id": "message-2"}], "cursor": "message-2"},
  ]

  def fake_api(method, path, payload=None):
    calls.append((method, path, payload))
    if path.startswith("/api/agent-coordination/room?"):
      return {"scope": {"kind": "delegation", "id": "goal-1"}}
    if method == "POST":
      return {"messages": [{"id": "sent-1"}]}
    return inboxes.pop(0)

  monkeypatch.setattr(control, "_agent_api_call", fake_api)
  assert control._call_list_agent_peers({
    "after": "peer-cursor", "limit": 25,
  })["scope"]["id"] == "goal-1"
  sent = control._call_send_agent_message({
    "recipients": ["peer-1"],
    "kind": "finding",
    "body": "  The fixture requires UTF-8.  ",
    "send_id": "fixture-send-1",
  })
  assert sent == {"messages": [{"id": "sent-1"}]}
  first = control._call_read_agent_messages({})
  second = control._call_read_agent_messages({})
  assert first["cursor"] == "message-1"
  assert second["cursor"] == "message-2"
  assert "after=message-1" in calls[-1][1]
  assert calls[0][1].endswith("peer_limit=25&peer_after=peer-cursor")
  assert calls[1] == (
    "POST",
    "/api/agent-coordination/messages",
    {
      "recipients": ["peer-1"],
      "broadcast": False,
      "kind": "finding",
      "body": "The fixture requires UTF-8.",
      "send_id": "fixture-send-1",
    },
  )

  invalid = control._call_tool({
    "name": platform_tools.SEND_MESSAGE_TOOL_NAME,
    "arguments": {"body": "nowhere"},
  })
  assert invalid["isError"] is True
  assert "exactly one" in invalid["content"][0]["text"]


def test_control_stdio_process_survives_tool_errors_and_keeps_serving():
  script = Path(platform_tools._control_script())
  env = dict(os.environ)
  for key in platform_tools.CONTROL_ENV_VARS:
    env.pop(key, None)
  # Keep this a top-level control process so the Goal tool is advertised;
  # leave its API credentials absent to exercise the intended tool error.
  env["MOBIUS_RUN_TOKEN"] = "stdio-test-run"
  messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
      "protocolVersion": "2025-11-25",
    }},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
      "name": platform_tools.GOAL_TOOL_NAME,
      "arguments": {"objective": "Ship and verify"},
    }},
    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
  ]

  completed = subprocess.run(
    [sys.executable, str(script)],
    input="".join(json.dumps(message) + "\n" for message in messages),
    text=True,
    capture_output=True,
    check=True,
    timeout=10,
    env=env,
  )

  responses = [json.loads(line) for line in completed.stdout.splitlines()]
  assert [response["id"] for response in responses] == [1, 2, 3]
  assert responses[1]["result"]["isError"] is True
  assert "missing environment" in responses[1]["result"]["content"][0]["text"]
  assert responses[2]["result"] == {}
  assert completed.stderr == ""
