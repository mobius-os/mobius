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


def test_goal_copy_guidance_separates_owner_text_from_verification_evidence():
  control = _control_module()
  objective = control._TOOL_DEFINITIONS['promote_goal']['inputSchema']['properties']['objective']
  complete = control._TOOL_DEFINITIONS['update_goal']['inputSchema']['properties']['complete']
  assert 'plain-language outcome shown to the owner' in objective['description']
  assert 'verification criteria in tasks' in objective['description']
  assert 'Set true after verifying the whole outcome' in complete['description']
  assert 'evidence in task results or the chat checkpoint' in complete['description']
  assert 'No separate success summary' in complete['description']
  assert complete['type'] == 'boolean'
  assert complete['enum'] == [True]
  assert 'maxLength' not in complete


@pytest.mark.parametrize("top_level,coordination", [(True, True), (True, False), (False, True)])
def test_helpers_are_builtin_without_subagents_app(monkeypatch, top_level, coordination):
  monkeypatch.delenv("MOBIUS_SUBAGENT_HELPER", raising=False)
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run" if top_level else "")
  monkeypatch.setenv("MOBIUS_COORDINATION_ENABLED", "1" if coordination else "0")
  control = _control_module()
  monkeypatch.setattr(control, "_app_tool_listings", lambda: [])
  names = platform_tools.expected_control_tool_names(
    top_level=top_level, coordination_enabled=coordination,
  )
  configured = platform_tools.codex_turn_mcp_config(
    None, control_enabled=True, top_level=top_level,
    coordination_enabled=coordination,
  )["mcp_servers"]["mobius_control"]["tools"]
  listed = control._dispatch_message({
    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
  })["result"]["tools"]
  assert tuple(configured) == names == tuple(tool["name"] for tool in listed)
  assert set(platform_tools.HELPER_TOOL_NAMES) <= set(names)
  assert "Delegate to helper agents with spawn_agent" in control._initialize_result({})["instructions"]
  assert "checkpoint_chat" in names and "claim_agent_work" in names
  assert ("list_agent_peers" in names) is (not top_level or coordination)


def test_spawn_defaults_to_actual_calling_turn_and_no_app_owner(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("CHAT_ID", "parent")
  monkeypatch.setenv("MOBIUS_AGENT_PROVIDER", "codex")
  monkeypatch.setenv("MOBIUS_AGENT_MODEL", "gpt-current")
  monkeypatch.setenv("MOBIUS_AGENT_EFFORT", "high")
  calls = []
  def api(method, path, body=None):
    if path.endswith("/capabilities"):
      return {"connections": {"codex": {"configured": True}},
              "models": {"codex": [{"id": "gpt-current", "name": "Current"}]},
              "defaults": {"codex": "other"}}
    calls.append((method, path, body))
    return {"id": "child", "task_key": "review"}
  monkeypatch.setattr(control, "_agent_api_call", api)
  result = control._call_tool({"name": "spawn_agent", "arguments": {
    "name": "review", "task": "Review",
  }})
  assert result["isError"] is False
  assert calls[0][2] == {
    "app_id": None, "parent_chat_id": "parent", "task_key": "review",
    "prompt": "Review", "provider": "codex", "model": "gpt-current",
    "effort": "high", "scope": "write", "notify_parent_on_complete": True,
  }


def test_explicit_app_preference_overrides_turn_without_routing_provider(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_AGENT_PROVIDER", "codex")
  monkeypatch.setenv("MOBIUS_AGENT_MODEL", "current")
  monkeypatch.setenv("MOBIUS_AGENT_EFFORT", "medium")
  monkeypatch.setattr(control, "_agent_api_call", lambda *args: {
    "connections": {"codex": {"configured": True}},
    "config": {"providers": {"codex": {"enabled": True,
               "default_model": "configured", "default_effort": "high"}}},
    "models": {"codex": [{"id": "configured", "name": "Configured"}]},
  })
  assert control._helper_selection({}) == ("codex", "configured", "high")
  assert control._helper_selection({"model": "Configured", "effort": "low"}) == (
    "codex", "configured", "low")


def test_paused_preference_refuses_implicit_calling_provider_but_explicit_override_works(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_AGENT_EFFORT", "medium")
  monkeypatch.setenv("MOBIUS_AGENT_PROVIDER", "codex")
  monkeypatch.setenv("MOBIUS_AGENT_MODEL", "current")
  monkeypatch.setattr(control, "_agent_api_call", lambda *args: {
    "connections": {"codex": {"configured": True},
                    "claude": {"configured": True}},
    "config": {"providers": {"codex": {"enabled": False}}},
    "models": {"codex": [{"id": "current"}],
               "claude": [{"id": "sonnet"}]},
    "defaults": {"claude": "sonnet"},
  })
  with pytest.raises(RuntimeError, match="paused"):
    control._helper_selection({})
  assert control._helper_selection({"provider": "codex"}) == (
    "codex", "current", "medium")
  assert control._helper_selection({"provider": "claude"}) == (
    "claude", "sonnet", None)


def test_shared_discovery_does_not_import_backend_dependencies():
  source = platform_tools._control_script()
  result = subprocess.run([
    sys.executable, "-S", "-c",
    "import runpy, sys; runpy.run_path(sys.argv[1], run_name='control_probe'); "
    "assert 'app' not in sys.modules; assert 'sqlalchemy' not in sys.modules",
    source,
  ], capture_output=True, text=True, check=False)
  assert result.returncode == 0, result.stderr


def test_spawn_forwards_an_explicit_goal_task_without_guessing(
  monkeypatch,
):
  monkeypatch.setenv("MOBIUS_AGENT_PROVIDER", "codex")
  monkeypatch.setenv("MOBIUS_AGENT_MODEL", "current")
  monkeypatch.setenv("CHAT_ID", "parent")
  control = _control_module()
  sent = []
  def api(method, path, body=None):
    if path.endswith("/capabilities"):
      return {"connections": {"codex": {"configured": True}},
              "models": {"codex": [{"id": "current"}]}}
    sent.append((method, path, body))
    return {"id": "child", "task_key": "review"}
  monkeypatch.setattr(control, "_agent_api_call", api)
  arguments = {
    "name": "review", "task": "Review",
  }
  control._call_spawn_agent({**arguments, "plan_task": " verify "})
  assert sent[-1][2]["plan_task"] == "verify"
  control._call_spawn_agent(arguments)
  assert "plan_task" not in sent[-1][2]
  for invalid in ("", "  ", 7, None, "x" * 129):
    with pytest.raises(ValueError, match="plan_task"):
      control._call_spawn_agent({**arguments, "plan_task": invalid})
  schema = control._TOOL_DEFINITIONS["spawn_agent"]["inputSchema"]
  assert schema["properties"]["plan_task"]["maxLength"] == 128


def test_claude_pointer_does_not_replace_missing_helpers_with_a_provider_cli():
  text = (Path(__file__).resolve().parents[1] / "scripts/seed-skills/claude.md").read_text()
  assert "current tool list" in text
  assert "continue locally and sequentially" in text
  assert "launch a provider CLI as a substitute" in text
  assert "claude -p" not in text


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
    # Non-secret calling-turn selection and delegation identity.
    "MOBIUS_AGENT_PROVIDER", "MOBIUS_AGENT_MODEL", "MOBIUS_AGENT_EFFORT",
    "MOBIUS_DELEGATION_ID",
    # Non-secret capture context for the screenshot tool.
    "VIEWPORT_WIDTH", "VIEWPORT_HEIGHT", "VIEWPORT_PIXEL_RATIO",
    "AGENT_BROWSER_SESSION",
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


def test_promote_goal_result_names_the_real_plan_tool():
  """Told only to "publish its Goal plan", agents once invented plan tools."""
  control = _control_module()
  next_action = control._GOALS.PLAN_NEXT_ACTION
  assert "update_goal" in next_action
  assert control.UPDATE_GOAL_TOOL in control.OWNER_TOOLS
  assert control.UPDATE_GOAL_TOOL not in control.DELEGATED_TOOLS


def test_promote_goal_with_tasks_publishes_the_plan_in_the_same_call(monkeypatch):
  control = _control_module()
  monkeypatch.setattr(control._GOALS, "promote_goal", lambda objective: {
    "state": "promoted", "objective": objective,
    "root_run_id": "goal-1", "run_id": "run-1",
  })
  sent = []
  monkeypatch.setenv("CHAT_ID", "chat-1")
  monkeypatch.setattr(control, "_agent_api_call", lambda method, path, body: (
    sent.append((method, path, body)) or {
      "goal": {"id": "goal-1", "status": "open", "revision": 1},
      "plan": {"tasks": [], "summary": {"completed": 0, "total": 2, "ready": ["a"]}},
    }
  ))

  text = control._call_promote_goal({
    "objective": "Ship", "tasks": [{"id": "a", "title": "A"}],
  })

  assert sent == [("POST", "/api/chats/chat-1/goal/update", {
    "tasks": [{"id": "a", "title": "A"}],
  })]
  assert text.startswith("Goal promoted. Goal open, revision 1: 0/2 tasks complete.")
  assert "Ready: a." in text


def test_update_goal_reports_compactly_and_rejects_unknown_arguments(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("CHAT_ID", "chat-1")
  monkeypatch.setattr(control, "_agent_api_call", lambda method, path, body: {
    "goal": {"id": "g", "status": "open", "revision": 4, "objective": "Ship"},
    "plan": {
      "tasks": [{"id": "a", "title": "A", "status": "completed", "result": "ok"}],
      "summary": {"completed": 1, "total": 1, "running": [], "ready": []},
    },
  })

  write = control._call_update_goal({"tasks": [{"id": "a", "status": "completed"}]})
  read = control._call_update_goal({})

  assert write == "Goal open, revision 4: 1/1 tasks complete."
  assert read.splitlines()[0] == "Objective: Ship"
  assert "- a [completed]: A — ok" in read
  with pytest.raises(ValueError, match="does not take: owner"):
    control._call_update_goal({"owner": "x"})


def test_goal_report_supplies_completion_keys_without_bloating_progress_updates():
  control = _control_module()
  payload = {
    "goal": {"status": "open", "revision": 1, "held_work_keys": ["test:verified"]},
    "plan": {"tasks": [], "summary": {"can_complete": False, "completion_blockers": ["verify"]}},
  }
  compact = control._goal_report(payload, full=False)
  assert "test:verified" not in compact
  full = control._goal_report(payload, full=True)
  assert "Completion blocked by: verify" in full
  assert "test:verified" in full
  payload["plan"]["summary"]["can_complete"] = True
  ready = control._goal_report(payload, full=False)
  assert "Ready to complete after verification" in ready
  assert "test:verified" in ready
  payload["goal"]["status"] = "completed"
  settled = control._goal_report(payload, full=True)
  assert "Ready to complete" not in settled
  assert "test:verified" not in settled


def test_a_settled_goal_does_not_offer_its_old_next_action(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("CHAT_ID", "chat-1")
  goal = {"id": "g", "revision": 9, "objective": "Ship", "next_action": "Run the probe"}
  for status, shown in (("open", True), ("completed", False)):
    monkeypatch.setattr(control, "_agent_api_call", lambda *a, **k: {
      "goal": {**goal, "status": status}, "plan": None,
    })
    assert ("Next action: Run the probe" in control._call_update_goal({})) is shown


def test_defer_tool_records_a_quiet_hold_not_ready_work_or_a_terminal_outcome(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("CHAT_ID", "chat-1")
  sent = []
  def record(method, path, body):
    sent.append(body)
    return {"goal": {"status": "stopped", "revision": 5,
      "hold": {"cause": "deferred", "reason": "Tests deferred by owner"}},
      "plan": {"tasks": [], "summary": {"ready": ["later"], "running": []}}}
  monkeypatch.setattr(control, "_agent_api_call", record)
  receipt = control._call_update_goal({"defer": "Tests deferred by owner"})
  assert sent == [{"defer": "Tests deferred by owner"}]
  assert "On hold: Tests deferred by owner" in receipt and "End normally" in receipt
  assert "Ready:" not in receipt and "Outcome:" not in receipt
  assert "defer" in control._TOOL_DEFINITIONS[control.UPDATE_GOAL_TOOL]["inputSchema"]["properties"]


def test_platform_control_tools_are_marked_always_loaded(monkeypatch):
  """Claude Code defers MCP tools behind a search round trip by default, so the
  control tools every owner turn is told to use carry the always-load meta."""
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  control = _control_module()

  tools = control._tools_list_result()["tools"]

  assert tools and all(
    tool["_meta"]["anthropic/alwaysLoad"] is True for tool in tools
  )
  # The meta is added to the listing, not baked into the shared definition.
  assert "_meta" not in control._TOOL_DEFINITIONS[control.PROMOTE_GOAL_TOOL]
  assert all(set(tool["_meta"]) == {"anthropic/alwaysLoad"} for tool in tools)


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
  # Möbius-owned helpers replace the providers' built-in helper tools.
  assert "spawn_agent" in instructions
  assert "arrive in this chat automatically" in instructions

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
  # Expose the owning route's existing limits before an agent spends a call
  # discovering them in a 422 (condition_owner was previously unbounded here).
  for name, length in (("description", 500), ("condition_owner", 160), ("command", 4000)):
    assert wait_schema["properties"][name]["maxLength"] == length
  for name, minimum, maximum in (("delay_secs", 60, 604800),
                                  ("interval_secs", 60, 86400),
                                  ("deadline_secs", 1, 604800)):
    assert wait_schema["properties"][name]["minimum"] == minimum
    assert wait_schema["properties"][name]["maximum"] == maximum
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
  assert "spawn_agent" in instructions
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
  assert "Usually unnecessary" in finish and "finished_claims" in finish
  spawn = tools[control.SPAWN_AGENT_TOOL]["description"]
  assert "never poll" in spawn and "does not see this" in spawn
  for name in (
    control.SEND_AGENT_MESSAGE_TOOL, control.REQUEST_APPROVAL_TOOL,
    control.CLAIM_AGENT_WORK_TOOL, control.FINISH_AGENT_WORK_TOOL,
    control.LIST_AGENT_PEERS_TOOL, *control.HELPER_TOOLS,
  ):
    assert len(tools[name]["description"]) <= 1000, name


def test_constitution_routes_each_agent_network_to_its_owner():
  core = (
    Path(__file__).resolve().parents[2] / "skill" / "core.md"
  ).read_text(encoding="utf-8")

  # Delegation is platform-owned even with no installed apps. Native provider
  # helpers and peer messaging remain distinct authority paths.
  assert "built-in `spawn_agent`" in core
  assert "no app installation is required" in core
  assert "`delegation` skill" in core
  assert "do not substitute a provider CLI" in core
  assert "built-in helper tools" in " ".join(core.split()) and "switched off" in core
  assert "other Möbius chats" in core
  assert core.index("`list_agent_peers`") < core.index("`send_agent_message`")
  assert "ordinary chat-message API" in core


def test_bookkeeping_batch_guidance_preserves_durability_and_card_isolation():
  core = (
    Path(__file__).resolve().parents[2] / "skill" / "core.md"
  ).read_text(encoding="utf-8")
  assert "batch independent informational" in core
  assert "already-needed tool work in the same model step" in core
  assert "Await every\n  result and handle failures" in core
  assert "never delay a required save just to form a batch" in core
  assert "Owner-input cards remain separate and last" in core
  assert "measure saved model calls and input/cache tokens" in core


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
  # Independent requests may finish out of order; ids, not line position,
  # associate each response with its request. Errors must not lose the ping.
  assert sorted(response["id"] for response in responses) == [1, 2, 3]
  by_id = {response["id"]: response for response in responses}
  assert by_id[2]["result"]["isError"] is True
  assert "missing environment" in by_id[2]["result"]["content"][0]["text"]
  assert by_id[3]["result"] == {}
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
    control.REQUEST_SECRET_TOOL,
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


def test_app_tool_timeouts_are_ordered_service_then_control_then_provider():
  """The control script's HTTP wait sits between the service's own timeout and
  the provider-facing tool timeout, so the app's own timeout error is what the
  agent sees rather than a control or provider cutoff."""
  from app import app_tools

  control = _control_module()

  assert (
    app_tools.TOOL_TIMEOUT_SECONDS
    < control.APP_TOOL_CALL_TIMEOUT_SECONDS
    < platform_tools.CONTROL_TOOL_TIMEOUT_SECONDS
  )


def _recording_api(monkeypatch, control, reply=None):
  sent = []
  def call(method, path, payload=None, *, timeout=10):
    sent.append((method, path, payload))
    return reply if reply is not None else {}
  monkeypatch.setenv("CHAT_ID", "chat-1")
  monkeypatch.setattr(control, "_agent_api_call", call)
  monkeypatch.setattr(control, "_agent_api_json", call)
  return sent


def test_notify_owner_defaults_the_tap_to_this_chat_inside_the_installed_app(monkeypatch):
  control = _control_module()
  sent = _recording_api(monkeypatch, control)

  text = control._call_notify_owner({"title": "Ready", "body": "Your app is built."})

  assert sent == [("POST", "/api/notifications/send", {
    "source_id": "chat-1", "target": "/shell/?chat=chat-1",
    "title": "Ready", "body": "Your app is built.",
  })]
  assert "/shell/?chat=chat-1" in text
  control._call_notify_owner({"title": "t", "body": "b", "target": "/shell/?app=7"})
  assert sent[-1][2]["target"] == "/shell/?app=7"
  with pytest.raises(ValueError, match="needs: body"):
    control._call_notify_owner({"title": "t"})


def test_open_item_places_beside_this_chat_in_the_background_by_default(monkeypatch):
  control = _control_module()
  sent = _recording_api(monkeypatch, control)

  control._call_open_item({"kind": "app", "id": 42})

  assert sent == [("POST", "/api/notify", {
    "type": "open_item", "itemKind": "app", "itemId": "42",
    "sourceKind": "chat", "sourceId": "chat-1",
    "placement": "beside-source", "activation": "background",
  })]


def test_request_secret_saves_a_sealed_card_and_never_offers_reveal(monkeypatch):
  control = _control_module()
  saved = []
  monkeypatch.setattr(control._SECURE_INPUT, "_request_saved", lambda spec, command, action, cwd=None: (
    saved.append((spec, command, action, cwd)) or {"state": "waiting_for_owner"}
  ))

  control._call_request_secret({
    "title": "Connect service",
    "fields": [{"name": "api_key", "type": "password", "label": "API key"}],
    "command": ["python3", "/data/apps/x/store.py"],
  })
  control._call_request_secret({"preset": "owner_credentials"})

  spec, command, action, cwd = saved[0]
  assert spec["mode"] == "sealed" and action == "run" and cwd == "/data"
  assert command == ["python3", "/data/apps/x/store.py"]
  assert saved[1][2] == "owner-credentials"
  assert saved[1][0] is control._SECURE_INPUT.OWNER_CREDENTIALS_SPEC
  schema = control._TOOL_DEFINITIONS[control.REQUEST_SECRET_TOOL]["inputSchema"]
  assert "mode" not in schema["properties"]
  with pytest.raises(ValueError, match="does not take: mode"):
    control._call_request_secret({"mode": "reveal", "title": "x"})
  with pytest.raises(ValueError, match="argv list"):
    control._call_request_secret({
      "title": "x", "fields": [{"name": "a", "type": "text", "label": "A"}],
      "command": "python3 leak.py",
    })


def test_list_apps_narrows_by_one_exact_filter(monkeypatch):
  control = _control_module()
  _recording_api(monkeypatch, control, reply=[
    {"id": 1, "name": "Notes", "slug": "notes", "source_dir": "/data/apps/notes"},
    {"id": 2, "name": "Notes", "slug": "notes-2", "source_dir": "/data/apps/notes-2"},
  ])

  assert control._call_list_apps({"slug": "notes-2", "with_source_dir": True}) == [
    {"id": 2, "name": "Notes", "slug": "notes-2", "source_dir": "/data/apps/notes-2"},
  ]
  assert [app["id"] for app in control._call_list_apps({"name": "Notes"})] == [1, 2]
  with pytest.raises(ValueError, match="at most one"):
    control._call_list_apps({"slug": "a", "name": "b"})


def test_apply_app_publishes_the_directory_and_returns_where_to_open_it(
  monkeypatch, tmp_path,
):
  control = _control_module()
  sent = _recording_api(monkeypatch, control, reply={
    "mode": "updated", "warnings": [],
    "app": {"id": 9, "name": "Notes", "slug": "notes", "source_dir": str(tmp_path)},
  })

  receipt = control._call_apply_app({"source_dir": str(tmp_path)})

  assert sent == [("POST", "/api/apps/apply", {
    "source_dir": str(tmp_path.resolve()), "chat_id": "chat-1",
  })]
  assert receipt["app_id"] == 9 and receipt["open_path"] == "/shell/?app=9"
  with pytest.raises(ValueError, match="existing absolute directory"):
    control._call_apply_app({"source_dir": "relative/path"})


def test_helpers_may_build_apps_but_not_reach_the_owner(monkeypatch):
  control = _control_module()
  for name in control.APP_TOOLS:
    assert name in control.DELEGATED_TOOLS
  for name in (
    control.NOTIFY_OWNER_TOOL, control.OPEN_ITEM_TOOL, control.REQUEST_SECRET_TOOL,
  ):
    assert name in control.OWNER_TOOLS and name not in control.DELEGATED_TOOLS


def test_screenshot_returns_the_image_and_the_owner_embed_line(monkeypatch, tmp_path):
  control = _control_module()
  shot = tmp_path / "shot.png"
  shot.write_bytes(b"\x89PNG fake")
  calls = []

  class Done:
    returncode = 0
    stdout = (
      f"{shot}\nPASTE into your reply (the partner cannot see the PNG otherwise): "
      "![screenshot](/api/chats/c/media/shot.png)\n"
    )
    stderr = ""

  monkeypatch.setattr(control.subprocess, "run", lambda command, **kw: calls.append(command) or Done())

  result = control._call_tool({"name": "screenshot", "arguments": {
    "route": "/app/4", "content_only": True,
  }})

  assert calls[0][-2:] == ["--content-only", "/app/4"]
  image, note = result["content"]
  assert image["type"] == "image" and image["mimeType"] == "image/png"
  assert "![screenshot](/api/chats/c/media/shot.png)" in note["text"]
  refused = control._call_tool({"name": "screenshot", "arguments": {"route": "https://x"}})
  assert refused["isError"] is True
  direct = control._call_tool({"name": "screenshot", "arguments": {"app_id": 42}})
  assert direct["isError"] is False
  assert calls[-1][-1] == "/shell/?app=42"
  before = len(calls)
  for arguments in ({}, {"route": "/", "app_id": 42}, {"app_id": "memory"},
                    {"app_id": True}, {"app_id": 0}):
    invalid = control._call_tool({"name": "screenshot", "arguments": arguments})
    assert invalid["isError"] is True
  assert len(calls) == before


def test_screenshot_in_a_read_only_sandbox_says_why_it_cannot_capture(monkeypatch):
  control = _control_module()

  class Denied:
    returncode = 1
    stdout = ""
    stderr = "mkdir: cannot create directory '/data/chats/x/media': Permission denied"

  monkeypatch.setattr(control.subprocess, "run", lambda command, **kw: Denied())
  result = control._call_tool({"name": "screenshot", "arguments": {"route": "/"}})

  assert result["isError"] is True
  assert "needs write access" in result["content"][0]["text"]


@pytest.mark.parametrize(("body", "reason"), [
  ('{"detail":{"code":"invalid_plan","message":"note for a must be at most 1000 characters","task_id":"a"}}',
   "note for a must be at most 1000 characters"),
  ('{"detail":"A recipient is not an addressable Möbius peer."}',
   "A recipient is not an addressable Möbius peer."),
  ('{"detail":[{"loc":["body","tasks",0,"id"],"msg":"Field required"}]}',
   "tasks 0 id: Field required"),
  ("<html>Bad Gateway</html>", "<html>Bad Gateway</html>"),
])
def test_refusals_read_as_their_reason_not_the_wire_envelope(body, reason):
  control = _control_module()
  assert control._refusal_message(body) == reason
  assert "{" not in control._refusal_message(body) or body.startswith("<")


def test_a_settled_goal_does_not_offer_its_old_next_action(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("CHAT_ID", "chat-1")
  goal = {"id": "g", "revision": 9, "objective": "Ship", "next_action": "Run the probe"}
  for status, shown in (("open", True), ("completed", False)):
    monkeypatch.setattr(control, "_agent_api_call", lambda *a, **k: {
      "goal": {**goal, "status": status}, "plan": None,
    })
    assert ("Next action: Run the probe" in control._call_update_goal({})) is shown


def test_restart_guidance_registers_each_chat_without_duplicating_the_executor():
  control = _control_module()
  description = control._TOOL_DEFINITIONS[control.REQUEST_RESTART_TOOL]["description"]
  assert "Create this chat's own card even when another chat has one" in description
  assert "One later ready restart resumes every still-registered chat" in description
  assert "one restart per worker" in description
  root = Path(__file__).resolve().parents[2]
  core = (root / "skill/core.md").read_text()
  maintenance = (root / "backend/scripts/seed-skills/platform-maintenance.md").read_text()
  assert "Each chat whose work needs a restart publishes its own Restart card" in core
  assert "not a second restart executor" in core
  assert "Do not replace your\n  card with a peer handoff" in core
  assert "Every chat that still owes activation and verification calls" in maintenance
  assert "even if another chat already has a Restart card" in maintenance
  assert "not duplicate restart\n   executors" in maintenance


def test_legacy_preference_and_alias_remain_explicit_overrides(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_AGENT_PROVIDER", "codex")
  monkeypatch.setenv("MOBIUS_AGENT_MODEL", "caller")
  monkeypatch.setenv("MOBIUS_AGENT_EFFORT", "medium")
  capability = {
    "connections": {"codex": {"configured": True}},
    "config": {"enabled": True, "default": "configured"},
    "models": {"codex": [{"id": "configured", "name": "Configured"}]},
    "aliases": {"codex": {"configured": ["reviewer"]}},
  }
  monkeypatch.setattr(control, "_agent_api_call", lambda *a: capability)
  assert control._helper_selection({}) == ("codex", "configured", "medium")
  assert control._helper_selection({"model": "reviewer"}) == ("codex", "configured", "medium")
  capability["config"]["enabled"] = False
  with pytest.raises(RuntimeError, match="paused"):
    control._helper_selection({})
  assert control._helper_selection({"provider": "codex"}) == ("codex", "configured", "medium")


def test_builtin_delegation_guidance_is_available_without_an_app():
  root = Path(__file__).resolve().parents[1] / "scripts/seed-skills"
  text = (root / "delegation.md").read_text()
  assert "installing Subagents is optional" in text
  assert "calling turn" in text
  assert "Never poll" in text
  assert "provider CLI" in text
  assert "no `access` selector" in text
  assert "State read-only limits in the task" in text
  assert "read-only children" not in text
  assert "complete `delegation`" in (root / "claude.md").read_text()


def test_completion_tool_sends_a_flag_and_returns_only_a_receipt(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("CHAT_ID", "chat-1")
  sent = []
  def record(method, path, body):
    sent.append(body)
    return {"goal": {"status": "completed", "revision": 2, "result": None},
            "plan": {"tasks": [], "summary": {"completed": 1, "total": 1}}}
  monkeypatch.setattr(control, "_agent_api_call", record)
  receipt = control._call_update_goal({"complete": True})
  assert sent == [{"complete": True}]
  assert receipt == "Goal completed, revision 2: 1/1 tasks complete."


def test_legacy_success_text_is_readable_but_not_repeated_in_write_receipts():
  control = _control_module()
  payload = {"goal": {"status": "completed", "revision": 2,
                      "result": "Historical result"}, "plan": None}
  assert "Historical result" not in control._goal_report(payload, full=False)
  assert "Historical result" in control._goal_report(payload, full=True)
  payload["goal"]["status"] = "cannot_complete"
  assert "Historical result" in control._goal_report(payload, full=False)
