"""Shared helper hosts: identity isolation, host lifecycle, and Claude dispatch."""

import asyncio
import os
import stat

import pytest

from app import claude_helper_host as claude_host
from app import helper_hosts


# ----------------------------------------------------------------- identity


def test_turn_identity_never_lives_in_the_shared_host_environment():
  host, turn = helper_hosts.split_env({
    "PATH": "/usr/bin",
    "CODEX_HOME": "/data/cli-auth/codex",
    "AGENT_TOKEN": "secret",
    "CHAT_ID": "chat-1",
    "MOBIUS_RUN_MARKER": "m1",
    "TMPDIR": "/data/agent-scratch/chat-1",
    "VIEWPORT_WIDTH": "1200",
  })
  assert host == {"PATH": "/usr/bin", "CODEX_HOME": "/data/cli-auth/codex"}
  assert set(turn) == {
    "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_MARKER", "TMPDIR", "VIEWPORT_WIDTH",
  }


def test_turn_env_file_is_private_round_trips_and_is_removed(tmp_path):
  values = {"AGENT_TOKEN": "tok with 'quotes' $x", "CHAT_ID": "c1"}
  env_file = helper_hosts.TurnEnvFile(tmp_path, "marker", values)
  assert stat.S_IMODE(os.stat(env_file.path).st_mode) == 0o600
  assert helper_hosts.load_env_file(str(env_file.path)) == values
  env_file.remove()
  assert not env_file.path.exists()


def test_codex_turn_settings_carry_public_values_and_only_a_path_to_secrets(tmp_path):
  from app.platform_tools import CONTROL_SERVER_NAME
  env_file = helper_hosts.TurnEnvFile(tmp_path, "m", {"AGENT_TOKEN": "secret"})
  base = {"mcp_servers": {CONTROL_SERVER_NAME: {
    "command": "python3", "env_vars": ["API_BASE_URL", "AGENT_TOKEN", "CHAT_ID"],
  }}}
  config = helper_hosts.codex_turn_thread_config(
    base,
    turn_env={"AGENT_TOKEN": "secret", "CHAT_ID": "c1", "MOBIUS_RUN_MARKER": "m"},
    env_file=env_file,
  )
  shell = config["shell_environment_policy"]["set"]
  assert shell == {"CHAT_ID": "c1", "MOBIUS_RUN_MARKER": "m", "BASH_ENV": str(env_file.path)}
  control = config["mcp_servers"][CONTROL_SERVER_NAME]
  assert control["env_vars"] == ["API_BASE_URL"]
  assert control["env"] == {
    helper_hosts.CALLER_ENV_FILE_ENV: str(env_file.path), "MOBIUS_RUN_MARKER": "m",
  }
  assert "secret" not in repr(config)
  assert base["mcp_servers"][CONTROL_SERVER_NAME]["env_vars"][1] == "AGENT_TOKEN"


# ----------------------------------------------------------------- lifecycle


class _FakeHost(helper_hosts.Host):
  starts = 0

  def __init__(self, key):
    super().__init__(key)
    self.closed = False

  async def start(self):
    type(self).starts += 1

  async def close(self):
    self._closed = True
    self.closed = True


def _key(parent="p1"):
  return helper_hosts.HostKey(parent, "codex", "write", "/data", "s")


def test_one_host_serves_every_turn_of_a_parent_and_setup(monkeypatch):
  monkeypatch.setattr(helper_hosts, "HOST_IDLE_SECONDS", 60)
  manager = helper_hosts.HostManager()
  _FakeHost.starts = 0

  async def scenario():
    seen = []

    async def turn():
      async with manager.lease(_key(), lambda: _FakeHost(_key())) as host:
        seen.append(host)
        await asyncio.sleep(0.01)

    await asyncio.gather(turn(), turn(), turn())
    async with manager.lease(_key("p2"), lambda: _FakeHost(_key("p2"))) as other:
      seen.append(other)
    await manager.close_all()
    return seen

  seen = asyncio.run(scenario())
  assert seen[0] is seen[1] is seen[2]
  assert seen[3] is not seen[0]
  assert _FakeHost.starts == 2


def test_an_idle_host_closes_and_a_dead_one_is_replaced(monkeypatch):
  monkeypatch.setattr(helper_hosts, "HOST_IDLE_SECONDS", 0.01)
  manager = helper_hosts.HostManager()

  async def scenario():
    async with manager.lease(_key(), lambda: _FakeHost(_key())) as first:
      pass
    await asyncio.sleep(0.05)
    assert first.closed and manager.live_hosts() == []
    async with manager.lease(_key(), lambda: _FakeHost(_key())) as second:
      second._closed = True  # the provider process died
    async with manager.lease(_key(), lambda: _FakeHost(_key())) as third:
      pass
    await manager.close_all()
    return second, third

  second, third = asyncio.run(scenario())
  assert third is not second


def test_host_digest_separates_parents_and_setups():
  assert _key("a").digest != _key("b").digest
  assert helper_hosts.HostKey("a", "codex", "read", "/data", "s").digest != _key("a").digest


# ----------------------------------------------------------------- Claude dispatch


def _claude_host(tmp_path):
  return claude_host.ClaudeHelperHost(
    _key(), options_factory=None, session_file=tmp_path / "host.json",
  )


def _turn(tmp_path, *, read_only=False, dispatch_id="d1"):
  return claude_host.HelperTurn(
    dispatch_id=dispatch_id, kind="spawn",
    spec={"description": dispatch_id, "prompt": "exact task", "subagent_type": "mobius-helper"},
    sink=None, env_file=helper_hosts.TurnEnvFile(tmp_path, dispatch_id, {"CHAT_ID": "c"}),
    read_only=read_only,
  )


def test_dispatcher_launches_only_registered_specs_verbatim(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._specs["d1"] = turn.spec
  host._turn_by_dispatch["d1"] = turn

  allowed = asyncio.run(host.pre_tool_use(
    {"tool_name": "Agent", "tool_input": {"description": "d1", "prompt": "model wrote this"}},
    "toolu_1", None,
  ))
  assert allowed["hookSpecificOutput"]["updatedInput"] == turn.spec
  assert host._turn_by_tool_use["toolu_1"] is turn

  for name, tool_input in (
    ("Agent", {"description": "unknown"}),
    ("Bash", {"command": "rm -rf /"}),
  ):
    denied = asyncio.run(host.pre_tool_use(
      {"tool_name": name, "tool_input": tool_input}, "toolu_2", None,
    ))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
  # SendMessage is deferred: the dispatcher must be able to load it.
  assert asyncio.run(host.pre_tool_use(
    {"tool_name": "ToolSearch", "tool_input": {"query": "select:SendMessage"}}, "t", None,
  )) == {}


def test_helper_calls_carry_their_own_identity_and_respect_their_limits(tmp_path):
  host = _claude_host(tmp_path)
  writer = _turn(tmp_path, dispatch_id="dw")
  reader = _turn(tmp_path, read_only=True, dispatch_id="dr")
  host._turn_by_agent.update({"agent-w": writer, "agent-r": reader})

  def call(agent, name, tool_input):
    return asyncio.run(host.pre_tool_use(
      {"tool_name": name, "tool_input": tool_input, "agent_id": agent}, "t", None,
    ))

  bash = call("agent-w", "Bash", {"command": "echo $CHAT_ID"})
  command = bash["hookSpecificOutput"]["updatedInput"]["command"]
  assert command.endswith("&& echo $CHAT_ID") and str(writer.env_file.path) in command

  control = call("agent-w", "mcp__mobius_control__spawn_agent", {"name": "x"})
  assert control["hookSpecificOutput"]["updatedInput"]["_mobius_caller_env_file"] == str(
    writer.env_file.path,
  )

  for agent, name in (("agent-r", "Write"), ("agent-w", "Agent"), ("agent-w", "Workflow")):
    denied = call(agent, name, {})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", (agent, name)
  assert call("agent-r", "Read", {"file_path": "/data/x"}) == {}


def test_a_resumed_helper_reports_to_its_current_follow_up_turn(tmp_path):
  host = _claude_host(tmp_path)
  first = _turn(tmp_path, dispatch_id="d1")
  host._turn_by_tool_use["toolu_launch"] = first

  class Started:
    task_type = "local_agent"
    task_id = "agent-1"
    tool_use_id = "toolu_launch"

  host._on_task_started(Started())
  assert first.agent_id == "agent-1" and first.started.is_set()
  first.finish("completed")

  follow_up = _turn(tmp_path, dispatch_id="d2")
  follow_up.agent_id = "agent-1"
  host._turn_by_agent["agent-1"] = follow_up
  host._on_task_started(Started())  # resumed under the original launch id
  assert follow_up.started.is_set()
  assert host._turn_for_parent("toolu_launch") is follow_up


def test_host_helper_sessions_round_trip():
  assert claude_host.parse_session("claude-host:sess-1:agent-9:toolu_1") == (
    "sess-1", "agent-9", "toolu_1",
  )
  assert claude_host.parse_session("claude-host:sess-1:agent-9") == ("sess-1", "agent-9", None)
  assert claude_host.parse_session("ordinary-session") == (None, None, None)
  assert claude_host.parse_session(None) == (None, None, None)
  assert claude_host.agent_type_for("high") == "mobius-helper-high"
  assert claude_host.agent_type_for(None) == "mobius-helper"


def test_boot_ends_only_hosts_whose_server_is_gone():
  import subprocess
  gone = subprocess.Popen(["true"])
  gone.wait()
  orphan = subprocess.Popen(
    ["sleep", "60"], start_new_session=True,
    env=dict(os.environ, **{
      helper_hosts.HOST_MARKER_ENV: f"{gone.pid}:1:host-digest",
    }),
  )
  # A live server's host (here: owned by this test process) is never touched,
  # so running this test on a live instance cannot end its real hosts.
  owned = subprocess.Popen(
    ["sleep", "60"], start_new_session=True,
    env=dict(os.environ, **{
      helper_hosts.HOST_MARKER_ENV: helper_hosts.host_marker("live-digest"),
    }),
  )
  bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
  try:
    assert helper_hosts.end_orphaned_hosts() >= 1
    orphan.wait(timeout=2)
    assert owned.poll() is None
    assert bystander.poll() is None
  finally:
    for proc in (orphan, owned, bystander):
      proc.kill()
      proc.wait()


def test_a_new_host_for_a_changed_setup_releases_the_old_idle_one(monkeypatch):
  monkeypatch.setattr(helper_hosts, "HOST_IDLE_SECONDS", 60)
  manager = helper_hosts.HostManager()
  old_key = helper_hosts.HostKey("p1", "codex", "write", "/data", "setup-a")
  new_key = helper_hosts.HostKey("p1", "codex", "write", "/data", "setup-b")
  other_chat = helper_hosts.HostKey("p2", "codex", "write", "/data", "setup-a")

  async def scenario():
    async with manager.lease(old_key, lambda: _FakeHost(old_key)) as old:
      pass
    async with manager.lease(other_chat, lambda: _FakeHost(other_chat)) as keep:
      pass
    async with manager.lease(new_key, lambda: _FakeHost(new_key)):
      pass
    live = manager.live_hosts()
    states = (old.closed, keep.closed)
    await manager.close_all()
    return states, live

  (old_closed, keep_closed), live = asyncio.run(scenario())
  assert old_closed and not keep_closed
  assert set(live) == {new_key, other_chat}


def test_connector_capabilities_do_not_change_the_host_key(monkeypatch):
  import types
  from app import chat as chat_mod

  class _Query:
    def filter(self, *_a):
      return self
    def first(self):
      return ("parent-1",)

  db = types.SimpleNamespace(query=lambda *_a: _Query())
  policy = types.SimpleNamespace(delegation_id="d", scope="read", cwd="/data", model="m")

  def plan(token):
    return types.SimpleNamespace(
      codex_config={"mcp_servers": {"svc": {"url": "http://b", "bearer_token_env_var": "CAP_SVC"}}},
      claude_servers={"svc": {"url": "http://b", "headers": {"Authorization": f"Bearer {token}"}}},
      codex_env={"CAP_SVC": token},
    )

  first = chat_mod._helper_host_key(db, policy, provider_id="codex", connector_plan=plan("tok-1"))
  second = chat_mod._helper_host_key(db, policy, provider_id="codex", connector_plan=plan("tok-2"))
  assert first == second
