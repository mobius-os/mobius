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
    "MOBIUS_COORDINATION_ENABLED",
  }
  assert "default_tools_approval_mode" not in codex_server
  assert codex_server["tools"] == {
    name: {"approval_mode": "approve"}
    for name in platform_tools.CONTROL_TOOL_NAMES
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


def test_isolated_owner_control_omits_coordination_tools(monkeypatch):
  expected = platform_tools.OWNER_CONTROL_TOOL_NAMES
  assert platform_tools.expected_control_tool_names(
    top_level=True, coordination_enabled=False,
  ) == expected
  configured = platform_tools.codex_turn_mcp_config(
    None, control_enabled=True, coordination_enabled=False,
  )
  assert set(configured["mcp_servers"]["mobius_control"]["tools"]) == set(
    expected
  )

  control = _control_module()
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  monkeypatch.setenv("MOBIUS_COORDINATION_ENABLED", "0")
  assert control._available_tool_names() == expected


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
  monkeypatch.delenv("MOBIUS_COORDINATION_ENABLED", raising=False)
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
  restart = tools[platform_tools.RESTART_TOOL_NAME]
  assert restart["inputSchema"] == {
    "type": "object", "properties": {}, "additionalProperties": False,
  }
  assert "platform derives" in restart["description"]
  assert "without waking an agent" in restart["description"]
  assert "NOT approval" in restart["description"]
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
  wait_description = tools[platform_tools.WAIT_TOOL_NAME]["description"]
  assert "Prefer a command when readiness is observable" in wait_description
  assert "no safe read-only check is available" in wait_description
  cancel_schema = tools[platform_tools.CANCEL_WAIT_TOOL_NAME]["inputSchema"]
  assert cancel_schema["required"] == ["wait_id"]
  assert set(cancel_schema["properties"]) == {"wait_id"}
  assert set(platform_tools.COORDINATION_TOOL_NAMES) <= set(tools)
  assert "read_agent_messages" not in tools
  send_schema = tools[platform_tools.SEND_MESSAGE_TOOL_NAME]["inputSchema"]
  assert send_schema["required"] == ["body"]
  assert set(send_schema["properties"]["kind"]["enum"]) == {
    "note", "finding", "request", "blocker", "handoff",
  }
  assert set(send_schema["properties"]["delivery"]["enum"]) == {
    "next_turn", "interrupt",
  }
  assert send_schema["properties"]["delivery"]["default"] == "next_turn"
  send_description = tools[platform_tools.SEND_MESSAGE_TOOL_NAME]["description"]
  assert "kind states what the message means" in send_description
  assert "next_turn is the default" in send_description
  assert "Use interrupt only when" in send_description
  assert "Broadcasts are always next_turn" in send_description
  assert "instead of checking for replies" in send_description


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
  assert platform_tools.RESTART_TOOL_NAME not in {
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


def test_request_restart_is_a_no_argument_owner_tool(monkeypatch):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  control = _control_module()
  receipt = {
    "state": "waiting_for_owner",
    "question_id": "restart-card-1",
    "next_action": "End this turn.",
  }
  calls = []
  monkeypatch.setattr(
    control._APPROVALS,
    "request_restart",
    lambda: calls.append("request_restart") or receipt,
  )

  response = control._call_tool({
    "name": platform_tools.RESTART_TOOL_NAME,
    "arguments": {},
  })
  assert response["isError"] is False
  assert json.loads(response["content"][0]["text"]) == receipt
  assert calls == ["request_restart"]

  invalid = control._call_tool({
    "name": platform_tools.RESTART_TOOL_NAME,
    "arguments": {"command": "restart"},
  })
  assert invalid["isError"] is True
  assert "takes no arguments" in invalid["content"][0]["text"]


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
        "condition_owner": "  GitHub checks  ",
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
    "condition_owner": "GitHub checks",
    "kind": "command",
    "command": "gh pr checks 123 --watch=false >/dev/null",
    "delay_secs": None,
    "interval_secs": 120,
    "deadline_secs": 3600,
  })]

  missing_owner = control._call_tool({
    "name": platform_tools.WAIT_TOOL_NAME,
    "arguments": {
      "description": "CI becomes green",
      "command": "false",
      "deadline_secs": 3600,
    },
  })
  assert missing_owner["isError"] is True
  assert "condition_owner" in missing_owner["content"][0]["text"]

  missing_deadline = control._call_tool({
    "name": platform_tools.WAIT_TOOL_NAME,
    "arguments": {
      "description": "CI becomes green",
      "condition_owner": "GitHub checks",
      "command": "false",
    },
  })
  assert missing_deadline["isError"] is True
  assert "deadline_secs" in missing_deadline["content"][0]["text"]

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


def test_coordination_tools_validate_discovery_and_send(monkeypatch):
  monkeypatch.delenv("MOBIUS_RUN_TOKEN", raising=False)
  control = _control_module()
  calls = []
  def fake_api(method, path, payload=None):
    calls.append((method, path, payload))
    if path == "/api/agent-coordination/room":
      return {
        "scope": {"kind": "delegation", "id": "goal-1"},
        "peers": [{"id": "peer-1", "name": "Peer", "online": True}],
      }
    if method == "POST":
      return {
        "messages": [{"id": "sent-1"}],
        "recipient_count": 1,
        "recipient_names": ["Peer"],
      }
    raise AssertionError(f"unexpected coordination request: {method} {path}")

  monkeypatch.setattr(control, "_agent_api_call", fake_api)
  assert control._TOOL_DEFINITIONS[
    platform_tools.LIST_PEERS_TOOL_NAME
  ]["inputSchema"]["properties"] == {}
  assert "read_agent_messages" not in control._TOOL_DEFINITIONS
  listed = control._call_list_agent_peers({})
  assert listed["scope"]["id"] == "goal-1"
  assert listed["peers"] == [{"id": "peer-1", "name": "Peer", "online": True}]
  assert "messages" not in listed
  sent = control._call_send_agent_message({
    "recipients": ["peer-1"],
    "kind": "finding",
    "body": "  The fixture requires UTF-8.  ",
    "send_id": "fixture-send-1",
  })
  assert sent == {
    "messages": [{"id": "sent-1"}],
    "recipient_count": 1,
    "recipient_names": ["Peer"],
  }
  assert calls[0][1] == "/api/agent-coordination/room"
  assert calls[1] == (
    "POST",
    "/api/agent-coordination/messages",
    {
      "recipients": ["peer-1"],
      "broadcast": False,
      "kind": "finding",
      "delivery": "next_turn",
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

  invalid_broadcast = control._call_tool({
    "name": platform_tools.SEND_MESSAGE_TOOL_NAME,
    "arguments": {
      "broadcast": True, "delivery": "interrupt", "body": "too broad",
    },
  })
  assert invalid_broadcast["isError"] is True
  assert "cannot interrupt" in invalid_broadcast["content"][0]["text"]


def test_mcp_send_passes_through_backend_compact_receipt(monkeypatch):
  control = _control_module()
  body = "x" * 4000
  rows = [{
    "id": f"sent-{index}",
    "kind": "handoff",
    "recipient_chat_id": f"peer-{index}",
    "recipient_name": f"Peer {index}",
    "broadcast": False,
    "body": body,
  } for index in range(24)]
  compact = {
    "messages": [rows[0]],
    "recipient_count": 24,
    "recipient_names": [f"Peer {index}" for index in range(24)],
    "woken": [],
  }
  monkeypatch.setattr(control, "_agent_api_call", lambda *_args, **_kwargs: compact)

  receipt = control._call_send_agent_message({
    "recipients": [f"peer-{index}" for index in range(24)],
    "kind": "handoff", "delivery": "interrupt",
    "body": body, "send_id": "one-send",
  })

  assert receipt is compact
  assert receipt["recipient_count"] == 24
  assert receipt["recipient_names"] == [f"Peer {index}" for index in range(24)]
  assert len(receipt["messages"]) == 1
  assert json.dumps(receipt).count(body) == 1


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
      "name": platform_tools.LIST_PEERS_TOOL_NAME,
      "arguments": {},
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


def test_saved_card_option_schema_exposes_explicit_quiet_outcome():
  control = _control_module()
  approval = control._TOOL_DEFINITIONS[control.REQUEST_APPROVAL_TOOL]["inputSchema"]
  question = control._TOOL_DEFINITIONS[control.REQUEST_QUESTION_TOOL]["inputSchema"]
  for option in (
    approval["properties"]["options"]["items"],
    question["properties"]["questions"]["items"]["properties"]["options"]["items"],
  ):
    assert option["properties"]["on_answer"]["enum"] == ["resume", "close"]
    assert "on_answer" not in option["required"]
