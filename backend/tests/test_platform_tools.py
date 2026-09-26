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


def test_isolated_owner_control_omits_peer_messaging_tools(monkeypatch):
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


def test_isolated_owner_keeps_durable_work_claims_without_peer_messaging(monkeypatch):
  """Restarting into an otherwise idle instance cannot hide claim ownership."""
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "resumed-owner-run")
  monkeypatch.setenv("MOBIUS_COORDINATION_ENABLED", "0")
  advertised = set(_control_module()._available_tool_names())
  configured = set(platform_tools.codex_turn_mcp_config(
    None, control_enabled=True, coordination_enabled=False,
  )["mcp_servers"]["mobius_control"]["tools"])
  for names in (advertised, configured):
    assert {"claim_agent_work", "finish_agent_work"} <= names
    assert not {"list_agent_peers", "send_agent_message"} & names


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
    "next_action": control._GOALS.PLAN_NEXT_ACTION,
  }


def test_promote_goal_result_names_the_plan_script_not_a_plan_tool():
  """Told only to "publish its Goal plan", agents invented an MCP plan tool."""
  control = _control_module()
  next_action = control._GOALS.PLAN_NEXT_ACTION
  plan_script = Path(__file__).resolve().parents[1] / "scripts" / "goal_plan.py"
  assert f"python3 {plan_script} set --task" in next_action
  assert "there is no plan tool" in next_action


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
  instructions = initialized["result"]["instructions"]
  assert "agents in other Möbius chats" in instructions
  assert "Provider-native subagent tools" in instructions
  assert "temporary subagent tree" in instructions

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
  assert set(platform_tools.PEER_TOOL_NAMES) <= set(tools)
  assert set(platform_tools.WORK_OWNERSHIP_TOOL_NAMES) <= set(tools)
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


def test_isolated_owner_control_does_not_advertise_peer_tools(monkeypatch):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "isolated-owner-run")
  monkeypatch.setenv("MOBIUS_COORDINATION_ENABLED", "0")
  control = _control_module()

  initialized = control._dispatch_message({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18"},
  })
  instructions = initialized["result"]["instructions"]
  assert "Provider-native subagent tools" in instructions
  assert "temporary subagent tree" in instructions
  assert "peer tools" not in instructions
  assert "agents in other Möbius chats" not in instructions


def test_peer_tool_descriptions_cut_coordination_calls():
  """Descriptions steer agents to paths, quiet delivery, and one-call approval."""
  control = _control_module()
  tools = control._TOOL_DEFINITIONS
  send = tools[control.SEND_AGENT_MESSAGE_TOOL]
  assert "by absolute path" in send["description"]
  assert "never paste or chunk" in send["description"]
  assert "unreachable" in send["description"]
  assert "never paste" in send["inputSchema"]["properties"]["body"]["description"]
  approval = tools[control.REQUEST_APPROVAL_TOOL]["description"]
  assert "do not call claim_agent_work first" in approval
  assert "your turn continues" in approval
  assert "needs no owner approval" in tools[control.CLAIM_AGENT_WORK_TOOL]["description"]
  finish = tools[control.FINISH_AGENT_WORK_TOOL]["description"]
  assert "Usually unnecessary" in finish and "--finished WORK_KEY" in finish
  for name in (
    control.SEND_AGENT_MESSAGE_TOOL, control.REQUEST_APPROVAL_TOOL,
    control.CLAIM_AGENT_WORK_TOOL, control.FINISH_AGENT_WORK_TOOL,
    control.LIST_AGENT_PEERS_TOOL,
  ):
    assert len(tools[name]["description"]) <= 1000, name


def test_constitution_routes_each_agent_network_to_its_owner():
  core = (
    Path(__file__).resolve().parents[2] / "skill" / "core.md"
  ).read_text(encoding="utf-8")

  assert "provider-native subagent tools" in core
  assert "`agents.*`, Task, or Agent" in core
  assert "other Möbius chats" in core
  assert core.index("`list_agent_peers`") < core.index("`send_agent_message`")
  assert "ordinary chat-message API" in core


def test_delegated_control_server_advertises_only_peer_and_ownership_tools(monkeypatch):
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


def test_question_tool_requires_only_owner_facing_meaning():
  control = _control_module()
  definition = control._TOOL_DEFINITIONS[control.REQUEST_QUESTION_TOOL]
  item = definition["inputSchema"]["properties"]["questions"]["items"]

  assert item["required"] == ["question"]
  assert item["properties"]["options"]["default"] == []
  assert "supplied when omitted" in definition["description"]


def test_saved_card_tools_instruct_the_agent_to_end_at_the_card():
  """Every exposed saved-card tool carries the same complete instruction."""
  control = _control_module()
  instruction = control.SAVED_CARD_TERMINAL_INSTRUCTION.lower()
  for name in (
    control.REQUEST_APPROVAL_TOOL,
    control.REQUEST_QUESTION_TOOL,
    control.REQUEST_RESTART_TOOL,
  ):
    description = control._TOOL_DEFINITIONS[name]["description"].lower()
    assert description.count(instruction) == 1


def test_control_cli_gate_and_unknown_tool(monkeypatch, capsys):
  """The shell seam reuses the server's authority gating for every primitive.

  Codex model gateways may drop the dynamic MCP namespace, so every control
  primitive must stay reachable through the CLI without a second implementation.
  """
  control = _control_module()
  monkeypatch.delenv("MOBIUS_RUN_TOKEN", raising=False)
  result = control._call_tool({"name": "request_restart", "arguments": {}})
  assert result["isError"] is True
  assert "unavailable" in result["content"][0]["text"]

  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-token")
  monkeypatch.setenv("MOBIUS_COORDINATION_ENABLED", "0")
  # Authority gating owns the check order: an unknown name reads as
  # unavailable rather than leaking which names exist.
  result = control._call_tool({"name": "unknown_tool", "arguments": {}})
  assert result["isError"] is True
  assert "unavailable" in result["content"][0]["text"]


def test_control_cli_usage_errors_and_success(monkeypatch, capsys):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-token")
  assert control._cli_call(["call"]) == 2
  assert control._cli_call(["call", "request_restart", "--args-json", "{"]) == 2
  assert control._cli_call(["call", "unknown_tool", "--args-json", "{}"]) == 1
  monkeypatch.setitem(
    control._TOOL_HANDLERS,
    "request_restart",
    lambda arguments: {"state": "saved", "arguments": arguments},
  )
  assert control._cli_call([
    "call", "request_restart", "--args-json", "{}",
  ]) == 0
  assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
    "state": "saved", "arguments": {},
  }


@pytest.mark.parametrize("arguments", [["cal", "request_restart"], ["--help"]])
def test_control_cli_unknown_subcommand_never_enters_stdio_server(arguments):
  script = (
    Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  )
  result = subprocess.run(
    [sys.executable, str(script), *arguments],
    input="",
    capture_output=True,
    text=True,
    timeout=5,
    check=False,
  )

  assert result.returncode == 2
  assert "usage: mobius_control_mcp.py call" in result.stderr


def _control_with_app_tools(monkeypatch, listed):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-token")
  calls = []

  def fake_api(method, path, payload=None, *, timeout=10):
    calls.append((method, path, payload, timeout))
    if method == "GET" and path == control.APP_TOOLS_PATH:
      return {"tools": listed}
    return {"result": "Logged.", "is_error": False}

  monkeypatch.setattr(control, "_agent_api_call", fake_api)
  return control, calls


def test_control_server_lists_installed_app_tools_beside_its_own(monkeypatch):
  app_tool = {
    "name": "reflection_log_friction", "description": "Log friction.",
    "inputSchema": {"type": "object"},
  }
  shadow = {**app_tool, "name": "request_restart"}
  control, _calls = _control_with_app_tools(monkeypatch, [app_tool, shadow])

  names = [tool["name"] for tool in control._tools_list_result()["tools"]]

  assert names[-1] == "reflection_log_friction"
  # An app can never replace a platform primitive.
  assert names.count("request_restart") == 1


def test_control_server_forwards_app_tool_calls_with_the_providers_meta(monkeypatch):
  control, calls = _control_with_app_tools(monkeypatch, [{
    "name": "reflection_log_friction", "description": "Log friction.",
    "inputSchema": {"type": "object"},
  }])

  result = control._call_tool({
    "name": "reflection_log_friction",
    "arguments": {"friction": "retried a flaky command"},
    "_meta": {"claudecode/toolUseId": "toolu_9"},
  })

  assert result == {
    "content": [{"type": "text", "text": "Logged."}], "isError": False,
  }
  method, path, payload, timeout = calls[-1]
  assert (method, path) == ("POST", control.APP_TOOLS_PATH + "call")
  assert payload == {
    "name": "reflection_log_friction",
    "arguments": {"friction": "retried a flaky command"},
    "meta": {"claudecode/toolUseId": "toolu_9"},
  }
  assert timeout == control.APP_TOOL_CALL_TIMEOUT_SECONDS


def test_unlisted_names_are_never_forwarded_to_apps(monkeypatch):
  control, calls = _control_with_app_tools(monkeypatch, [])
  result = control._call_tool({"name": "reflection_log_friction", "arguments": {}})
  assert result["isError"] is True
  assert "unavailable" in result["content"][0]["text"]
  assert all(method == "GET" for method, *_ in calls)
