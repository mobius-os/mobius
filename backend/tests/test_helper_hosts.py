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


def test_core_helpers_are_available_and_caller_defaults_are_turn_scoped():
  from app import platform_tools
  host, turn = helper_hosts.split_env({
    "MOBIUS_AGENT_PROVIDER": "codex", "MOBIUS_AGENT_MODEL": "chosen-model",
    "MOBIUS_AGENT_EFFORT": "high", "AGENT_TOKEN": "secret",
  })
  assert host == {}
  assert turn["MOBIUS_AGENT_MODEL"] == "chosen-model"
  assert turn["MOBIUS_AGENT_PROVIDER"] == "codex"
  assert turn["MOBIUS_AGENT_EFFORT"] == "high"
  assert "spawn_agent" in platform_tools.expected_control_tool_names(top_level=False)


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
  return helper_hosts.HostKey(parent, "codex", "/data", "s")


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


def test_host_digest_separates_parents_and_setups_without_changing_write_identity():
  assert _key("a").digest != _key("b").digest
  assert helper_hosts.HostKey("a", "codex", "/data", "other-setup").digest != _key("a").digest
  import hashlib, json
  old_write_key = ["a", "codex", "write", "/data", "s"]
  assert _key("a").digest == hashlib.sha256(json.dumps(old_write_key).encode()).hexdigest()[:24]


# ----------------------------------------------------------------- Claude dispatch


def _claude_host(tmp_path):
  return claude_host.ClaudeHelperHost(
    _key(), options_factory=None, session_file=tmp_path / "host.json",
  )


def _turn(tmp_path, *, dispatch_id="d1"):
  return claude_host.HelperTurn(
    dispatch_id=dispatch_id, kind="spawn",
    spec={"description": dispatch_id, "prompt": "exact task", "subagent_type": "mobius-helper"},
    sink=None, env_file=helper_hosts.TurnEnvFile(tmp_path, dispatch_id, {"CHAT_ID": "c"}),
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


def test_helper_calls_carry_their_own_identity_and_block_native_fanout(tmp_path):
  host = _claude_host(tmp_path)
  writer = _turn(tmp_path, dispatch_id="dw")
  host._turn_by_agent.update({"agent-w": writer})

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

  for agent, name in (("agent-w", "Agent"), ("agent-w", "Workflow")):
    denied = call(agent, name, {})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", (agent, name)
  assert call("agent-w", "Write", {"file_path": "/data/x"}) == {}


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


def test_a_dispatcher_that_answers_without_the_call_is_asked_again(tmp_path):
  """Seen live after a restart: the dispatcher replied "OK" without calling,
  so the resumed helper waited out 90 s and was rebuilt from its history. The
  host asks again at once, and each dispatch is used once, so a late original
  call and the repeated one can never launch two helpers."""
  host = _claude_host(tmp_path)
  queries: list[str] = []
  decisions: list[str] = []

  class _ForgetfulDispatcher:
    async def query(self, line):
      queries.append(line)
      if len(queries) == 1:
        host._dispatcher_replied()  # "OK", and no call
        return
      dispatch_id = line.split()[1]
      for tool_use_id in ("toolu_late", "toolu_again"):
        decision = await host.pre_tool_use(
          {"tool_name": "Agent", "tool_input": {"description": dispatch_id}},
          tool_use_id, None,
        )
        decisions.append(decision["hookSpecificOutput"]["permissionDecision"])
      host._on_task_started(_Started("agent-1", "toolu_late"))
      host._dispatcher_replied()
      asyncio.get_running_loop().call_soon(turn.finish, "completed")

  host._client = _ForgetfulDispatcher()
  turn = _turn(tmp_path)
  asyncio.run(host.run_turn(turn))

  assert queries == ["SPAWN d1", "SPAWN d1"]
  assert decisions == ["allow", "deny"]
  assert (turn.status, turn.dispatch_error, turn.agent_id) == (
    "completed", None, "agent-1",
  )


def test_a_late_end_of_the_previous_run_never_ends_a_follow_up(tmp_path):
  """Each run reports its end more than once. A follow-up is mapped to the
  agent before the agent resumes, so the late report once completed it
  instantly with no answer (seen with the live CLI)."""
  host = _claude_host(tmp_path)
  first = _turn(tmp_path, dispatch_id="d1")
  host._turn_by_tool_use["toolu_launch"] = first
  host._on_task_started(_Started("agent-1", "toolu_launch"))
  host._on_task_end("agent-1", "completed", None, None)
  assert first.status == "completed"

  follow_up = _turn(tmp_path, dispatch_id="d2")
  follow_up.agent_id = "agent-1"
  host._turn_by_agent["agent-1"] = follow_up  # as run_turn maps a MESSAGE
  host._on_task_end("agent-1", "completed", None, None)  # the late report
  assert not follow_up.done.is_set()

  host._on_task_started(_Started("agent-1", "toolu_launch"))
  host._on_task_end("agent-1", "completed", "answer", None)
  assert (follow_up.status, follow_up.summary) == ("completed", "answer")


def test_a_helper_lost_with_its_host_is_reseeded_not_messaged(tmp_path):
  lost = _turn(tmp_path, dispatch_id="d1")
  lost.agent_id, lost.launch_tool_use_id = "agent-1", "toolu_1"
  kept = _turn(tmp_path, dispatch_id="d2")
  kept.agent_id, kept.launch_tool_use_id = "agent-2", "toolu_2"
  kept.finish("completed")
  host = _claude_host(tmp_path)
  host._turn_by_dispatch = {"d1": lost, "d2": kept}
  host._fail_open_turns()  # its process died under the open turn
  assert claude_host.parse_session(
    claude_host.resume_reference("sess-1", lost),
  ) == ("sess-1", None, None)
  assert claude_host.parse_session(
    claude_host.resume_reference("sess-1", kept),
  ) == ("sess-1", "agent-2", "toolu_2")


class _Started:
  task_type = "local_agent"

  def __init__(self, task_id, tool_use_id):
    self.task_id, self.tool_use_id = task_id, tool_use_id


class _DispatcherClient:
  """Stands in for the host's Claude Code process: it launches or messages
  exactly the registered spec, the helper's agent reports that it started,
  and ``settle`` then ends the turn the way the scenario needs."""

  def __init__(self, host, settle):
    self.host, self.settle = host, settle
    self.dispatched: list[tuple[str, dict]] = []

  async def query(self, line):
    verb, dispatch_id = line.split()
    turn = self.host._turn_by_dispatch[dispatch_id]
    tool = "Agent" if verb == "SPAWN" else "SendMessage"
    key = "description" if verb == "SPAWN" else "summary"
    decision = await self.host.pre_tool_use(
      {"tool_name": tool, "tool_input": {key: dispatch_id}},
      f"toolu_{verb.lower()}", None,
    )
    self.dispatched.append(
      (verb, decision["hookSpecificOutput"]["updatedInput"]),
    )
    # A resumed helper reports under the tool call that first launched it.
    self.host._on_task_started(_Started(turn.agent_id or "agent-1", "toolu_spawn"))
    asyncio.get_running_loop().call_soon(self.settle, turn, verb)


def _host_turns(tmp_path, monkeypatch, settle):
  """Run helper turns through the real host turn and dispatch bookkeeping."""
  import contextlib
  from app import process_groups

  host = claude_host.ClaudeHelperHost(
    _key(), options_factory=None, session_file=tmp_path / "host.json",
  )
  host.session_id = "host-session"
  host._client = client = _DispatcherClient(host, settle)
  saved: list[str] = []

  class _Manager:
    @contextlib.asynccontextmanager
    async def lease(self, _key, _factory):
      yield host

  async def record_reference(_chat_id, reference):
    saved.append(reference)

  monkeypatch.setattr(helper_hosts, "MANAGER", _Manager())
  monkeypatch.setattr(claude_host, "record_reference", record_reference)
  monkeypatch.setattr(process_groups, "terminate_run_processes", lambda *a, **k: 0)

  def turn(user_message, session_id=None):
    return asyncio.run(claude_host.run_claude_host_turn(
      user_message=user_message, session_id=session_id,
      base_env={"CHAT_ID": "child", "TMPDIR": str(tmp_path)},
      chat_id="child", skill_text="helper", bc=None, agent_settings=None,
      skills_enabled=False, run_policy=None, connector_plan=None,
      helper_host_key=_key(), data_dir=str(tmp_path),
    ))

  return turn, client, saved


def test_a_helper_interrupted_mid_task_resumes_its_own_agent_with_its_task(
  tmp_path, monkeypatch,
):
  """Regression: a planned restart stopped two running helpers, and each came
  back as a brand-new helper whose whole prompt was the generic "resume the
  interrupted work" continuation, so it audited the restart instead of its
  task. The interrupted turn never settled cleanly, so its agent was never
  recorded and the continuation had nothing to resume.

  The helper's chat must name its agent from the moment the agent exists;
  the continuation then reaches that agent, whose transcript holds the task.
  """
  def restart_stops_the_first_turn(turn, verb):
    turn.finish("stopped" if verb == "SPAWN" else "completed")

  turn, client, saved = _host_turns(tmp_path, monkeypatch, restart_stops_the_first_turn)

  interrupted = turn("ORIGINAL TASK: audit the Reflection runs")
  # The stopped turn reports an error, so its own result never becomes the
  # chat's session; the pointer saved at agent start is what survives.
  assert interrupted["error"]
  assert claude_host.parse_session(saved[-1]) == (
    "host-session", "agent-1", "toolu_spawn",
  )

  continuation = "Resume the interrupted owner work after the planned server restart."
  turn(continuation, saved[-1])

  (first_verb, first), (verb, resumed) = client.dispatched
  assert first_verb == "SPAWN" and first["prompt"].startswith("ORIGINAL TASK")
  assert verb == "MESSAGE"
  assert resumed["to"] == "agent-1" and resumed["message"] == continuation


def test_a_lost_host_leaves_the_helper_pointing_at_a_reseed(tmp_path, monkeypatch):
  """A host that dies under a turn fails it, and a failed turn's result never
  reaches the chat; the chat must still stop naming the dead agent."""
  def host_dies(_turn, _verb):
    client.host._fail_open_turns()

  turn, client, saved = _host_turns(tmp_path, monkeypatch, host_dies)

  result = turn("task")

  assert result["error"]
  assert claude_host.parse_session(saved[0])[1] == "agent-1"
  assert claude_host.parse_session(saved[-1]) == ("host-session", None, None)


def test_a_restart_suspends_a_helper_by_ending_its_host_not_stopping_its_agent(
  tmp_path,
):
  """Claude Code never resumes an agent it was told to stop, but resumes one
  whose host process ended, with its task (checked against the live CLI). A
  planned restart must therefore end the host; an owner's Stop still stops
  the agent for good."""
  async def scenario():
    host = claude_host.ClaudeHelperHost(
      _key(), options_factory=None, session_file=tmp_path / "host.json",
    )
    client = _DispatcherClient(host, settle=lambda _turn, _verb: None)
    stopped_agents: list[str] = []

    async def stop_task(agent_id):
      stopped_agents.append(agent_id)

    async def disconnect():
      return None

    client.stop_task, client.disconnect = stop_task, disconnect
    host._client = client
    turn = _turn(tmp_path)
    handle = claude_host.ActiveClaudeHelperTurn("child", host, turn, "marker")

    async def run():
      try:
        await host.run_turn(turn)
      finally:
        handle.mark_finished()

    running = asyncio.create_task(run())
    await asyncio.wait_for(turn.started.wait(), 1)
    suspended = await handle.suspend(timeout=1)
    await running
    host._client = client  # an owner's Stop, on a live host
    await host.stop(turn)
    return suspended, host, turn, stopped_agents

  suspended, host, turn, stopped_agents = asyncio.run(scenario())

  assert suspended and turn.done.is_set() and not host.alive
  # Only the owner's Stop told Claude Code to stop the agent.
  assert stopped_agents == ["agent-1"]


def test_a_usage_limit_inside_a_host_parks_like_the_private_runner(
  tmp_path, monkeypatch,
):
  """Four helpers that hit the session limit failed as "The helper failed."
  and never resumed: in a host the limit arrives only as the helper's own
  API-error message. It must reach the turn's result as a limit."""
  from claude_agent_sdk.types import AssistantMessage, TextBlock
  from app import chat as chat_mod

  limit_text = "You've hit your session limit · resets 12:50am (UTC)"
  host = claude_host.ClaudeHelperHost(
    _key(), options_factory=None, session_file=tmp_path / "host.json",
  )
  routed = _turn(tmp_path)
  host._turn_by_tool_use["toolu_spawn"] = routed

  class _Stream:
    async def receive_messages(self):
      yield AssistantMessage(
        content=[TextBlock(text=limit_text)], model="claude",
        parent_tool_use_id="toolu_spawn", error="rate_limit",
      )

  host._client = _Stream()
  asyncio.run(host._read())
  assert routed.api_error == ("rate_limit", limit_text)

  def limit_ends_the_turn(turn, _verb):
    turn.api_error = routed.api_error
    turn.finish("failed")

  turn, _client, _saved = _host_turns(tmp_path, monkeypatch, limit_ends_the_turn)
  result = turn("task")

  assert result["error"] == limit_text
  assert chat_mod._is_limit_terminal(result)


def test_a_dispatch_that_never_starts_says_what_the_dispatcher_did(
  tmp_path, monkeypatch, caplog,
):
  """Two helpers failed with "could not start" and left nothing to diagnose;
  the timeout records the dispatcher's own last reply, and the host's logs
  reach the persistent chat log."""
  import logging
  from app import startup

  monkeypatch.setattr(claude_host, "DISPATCH_START_TIMEOUT", 0.05)
  host = claude_host.ClaudeHelperHost(
    _key(), options_factory=None, session_file=tmp_path / "host.json",
  )

  class _SilentDispatcher:
    async def query(self, _line):
      host.dispatcher_last = "server_error: API Error: 529 Overloaded"

  host._client = _SilentDispatcher()
  turn = _turn(tmp_path)
  with caplog.at_level(logging.WARNING, logger="app.claude_helper_host"):
    asyncio.run(host.run_turn(turn))

  assert turn.dispatch_error == "timeout"
  assert "529 Overloaded" in caplog.text and "tool_call_seen=False" in caplog.text

  startup._route_diagnostics_to_chat_log(None)
  from app.chat_logging import get_chat_log_handler
  for name in ("app.helper_hosts", "app.claude_helper_host"):
    assert get_chat_log_handler() in logging.getLogger(name).handlers


def test_host_helper_sessions_round_trip():
  assert claude_host.parse_session("claude-host:sess-1:agent-9:toolu_1") == (
    "sess-1", "agent-9", "toolu_1",
  )
  assert claude_host.parse_session("claude-host:sess-1:agent-9") == ("sess-1", "agent-9", None)
  assert claude_host.parse_session("ordinary-session") == (None, None, None)
  assert claude_host.parse_session(None) == (None, None, None)
  assert claude_host.agent_type_for("high") == "mobius-helper-high"
  assert claude_host.agent_type_for(None) == "mobius-helper"


def test_boot_ends_only_hosts_whose_server_is_gone(monkeypatch):
  import subprocess
  gone = subprocess.Popen(["true"])
  gone.wait()

  def host(marker):
    return subprocess.Popen(
      ["sleep", "60"], start_new_session=True,
      env=dict(os.environ, **{helper_hosts.HOST_MARKER_ENV: marker}),
    )

  orphan = host(f"{gone.pid}:1:host-digest")
  unowned = host("host-digest")  # marker from before owners were recorded
  owned = host(helper_hosts.host_marker("live-digest"))
  bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
  procs = (orphan, unowned, owned, bystander)
  # Scan only this test's processes: the real scan would end the live hosts
  # of whatever instance runs the suite.
  real_listdir = os.listdir
  monkeypatch.setattr(
    helper_hosts.os, "listdir",
    lambda path: [str(p.pid) for p in procs] if path == "/proc" else real_listdir(path),
  )
  try:
    assert helper_hosts.end_orphaned_hosts() == 2
    orphan.wait(timeout=2)
    unowned.wait(timeout=2)
    assert owned.poll() is None
    assert bystander.poll() is None
  finally:
    for proc in procs:
      proc.kill()
      proc.wait()


def test_a_new_host_for_a_changed_setup_releases_the_old_idle_one(monkeypatch):
  monkeypatch.setattr(helper_hosts, "HOST_IDLE_SECONDS", 60)
  manager = helper_hosts.HostManager()
  old_key = helper_hosts.HostKey("p1", "codex", "/data", "setup-a")
  new_key = helper_hosts.HostKey("p1", "codex", "/data", "setup-b")
  other_chat = helper_hosts.HostKey("p2", "codex", "/data", "setup-a")

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
  policy = types.SimpleNamespace(delegation_id="d", cwd="/data", model="m")

  def plan(token):
    return types.SimpleNamespace(
      codex_config={"mcp_servers": {"svc": {"url": "http://b", "bearer_token_env_var": "CAP_SVC"}}},
      claude_servers={"svc": {"url": "http://b", "headers": {"Authorization": f"Bearer {token}"}}},
      codex_env={"CAP_SVC": token},
    )

  first = chat_mod._helper_host_key(db, policy, provider_id="codex", connector_plan=plan("tok-1"))
  second = chat_mod._helper_host_key(db, policy, provider_id="codex", connector_plan=plan("tok-2"))
  assert first == second


@pytest.mark.parametrize("supports_effort", [True, False])
def test_every_hosted_helper_gets_the_claude_register_and_text_stream(tmp_path, supports_effort):
  """Hosted helpers get the same Claude register as a top-level turn."""
  from contextlib import ExitStack
  from types import SimpleNamespace

  factory = claude_host._host_options(
    key=SimpleNamespace(cwd=str(tmp_path)), host_env={},
    skill_text="CONSTITUTION", connector_plan=None, skills_enabled=False,
    model=None, supports_effort=supports_effort,
  )
  host = SimpleNamespace(stderr_tail=[], pre_tool_use=None, post_tool_use=None, subagent_stop=None)
  for resume in (None, "host-session"):
    with ExitStack() as stack:
      options = factory(host, resume, stack)
    assert options.include_partial_messages and options.forward_subagent_text
    assert set(options.agents) == {
      "mobius-helper", *(claude_host.agent_type_for(e) for e in claude_host.EFFORTS),
    }
    if not supports_effort:
      assert all(agent.effort is None for agent in options.agents.values())
    for agent in options.agents.values():
      assert agent.prompt.startswith("CONSTITUTION")
      assert "# Interruptions in Möbius" in agent.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("model,expected_agent", [
  ("claude-haiku-4-5-20251001", "mobius-helper"),
  ("claude-live-no-effort", "mobius-helper"),
  ("claude-opus-4-8", "mobius-helper-high"),
])
@pytest.mark.parametrize("saved_settings", [False, True])
@pytest.mark.parametrize("session_id", [None, "claude-host:host-session:agent-1:tool-1"])
async def test_hosted_helper_omits_effort_when_its_model_rejects_it(
  tmp_path, monkeypatch, model, expected_agent, saved_settings, session_id,
):
  from contextlib import asynccontextmanager
  from types import SimpleNamespace
  import time
  from app import providers, process_groups

  monkeypatch.setattr(providers, "_model_registry_cache", {
    "claude": (time.monotonic(), [
      {"id": "claude-live-no-effort", "effort_levels": []},
      {"id": "claude-opus-4-8"},
    ]),
  })
  dispatched = []

  class Host:
    session_id = "host-session"

    async def run_turn(self, turn, on_started):
      dispatched.append(turn.spec)
      turn.finish("completed")

  @asynccontextmanager
  async def lease(key, factory):
    yield Host()

  monkeypatch.setattr(helper_hosts.MANAGER, "lease", lease)
  monkeypatch.setattr(process_groups, "terminate_run_processes", lambda *_a, **_kw: None)
  policy = SimpleNamespace(model=model, effort="high", scope="write")
  result = await claude_host.run_claude_host_turn(
    user_message="inspect the source", session_id=session_id,
    base_env={"TMPDIR": str(tmp_path)}, chat_id="effort-test",
    skill_text="", bc=None,
    agent_settings={"model": model, "effort": "high"} if saved_settings else None,
    skills_enabled=False, run_policy=policy, connector_plan=None,
    helper_host_key=helper_hosts.HostKey("parent", "claude", str(tmp_path), "setup"),
    data_dir=str(tmp_path),
  )

  assert result["error"] is None
  assert len(dispatched) == 1
  if session_id:
    # A continuing agent keeps its SDK launch options: never replay its task.
    assert dispatched[0]["to"] == "agent-1"
    assert "subagent_type" not in dispatched[0]
  else:
    assert dispatched[0]["subagent_type"] == expected_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_support", [False, True])
async def test_reused_claude_host_keeps_dispatch_names_across_capability_changes(
  tmp_path, monkeypatch, initial_support,
):
  from contextlib import ExitStack, asynccontextmanager
  from types import SimpleNamespace
  from app import providers, process_groups

  supported = initial_support

  async def supports_effort(data_dir, model):
    return supported

  monkeypatch.setattr(providers, "model_supports_effort", supports_effort)
  monkeypatch.setattr(process_groups, "terminate_run_processes", lambda *_a, **_kw: None)
  options = None
  seen = []

  class Host:
    session_id = "host-session"

    async def run_turn(self, turn, on_started):
      agent = options.agents[turn.spec["subagent_type"]]
      seen.append(agent.effort)
      turn.finish("completed")

  @asynccontextmanager
  async def lease(key, factory):
    nonlocal options
    if options is None:
      host = factory()
      with ExitStack() as stack:
        options = host._options_factory(host, None, stack)
    yield Host()

  monkeypatch.setattr(helper_hosts.MANAGER, "lease", lease)
  for support in (initial_support, not initial_support):
    supported = support
    result = await claude_host.run_claude_host_turn(
      user_message="inspect", session_id=None, base_env={"TMPDIR": str(tmp_path)},
      chat_id="capability-test", skill_text="", bc=None, agent_settings=None,
      skills_enabled=False,
      run_policy=SimpleNamespace(model="claude-live", effort="high", scope="write"),
      connector_plan=None,
      helper_host_key=helper_hosts.HostKey("parent", "claude", str(tmp_path), "setup"),
      data_dir=str(tmp_path),
    )
    assert result["error"] is None

  assert seen == (["high", None] if initial_support else [None, None])


def test_codex_host_death_observation_survives_sdk_and_counter_changes(monkeypatch):
  from types import SimpleNamespace

  count = 4
  monkeypatch.setattr(helper_hosts, "cgroup_oom_kill_count", lambda: count)
  sync = SimpleNamespace(_proc=SimpleNamespace(poll=lambda: -9))
  host = helper_hosts.CodexHelperHost(_key(), sdk={}, config=None)
  host.client = SimpleNamespace(_client=SimpleNamespace(_sync=sync))
  assert not host.alive
  evidence = host.exit_evidence
  sync._proc = None
  count = 5
  assert not host.alive
  assert host.exit_evidence is evidence
  assert evidence.was_oom_killed(3)
  assert not evidence.was_oom_killed(4)
