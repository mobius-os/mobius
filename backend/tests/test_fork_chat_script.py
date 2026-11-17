import asyncio
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "backend" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_session as fork_session_module  # noqa: E402
from fork_session import (  # noqa: E402
  ForkError,
  ForkResult,
  _assert_codex_mcp_isolated,
  _assert_codex_tools_off,
  _codex_mcp_isolation_overrides,
  _codex_turn_of_call,
  _fork_claude,
  _fork_codex_async,
  _load_codex_sdk,
  _parse_invocation,
)
from fork_chat import _chat_session, coach_chat, main as fork_chat_main  # noqa: E402
from app.providers import CODEX_TOOL_FEATURES_OFF  # noqa: E402


def _app_server_page(method, *, features=None):
  """An empty MCP surface, or every tool switch off as a fork reports it."""
  if method != "experimentalFeature/list":
    return {"data": [], "nextCursor": None}
  if features is None:
    features = [
      {"name": name, "enabled": False, "stage": "stable"}
      for name in CODEX_TOOL_FEATURES_OFF
    ]
  return {"data": features, "nextCursor": None}


def test_platform_coaching_helpers_are_directly_executable():
  for name in ("fork-chat.sh", "fork-session.sh", "fork_chat.py", "fork_session.py"):
    assert (SCRIPTS / name).stat().st_mode & stat.S_IXUSR


def test_legacy_three_argument_session_helper_remains_claude(monkeypatch, capsys):
  seen = {}

  def fake_fork(provider, session_id, cwd, prompt):
    seen["args"] = (provider, session_id, cwd, prompt)
    return ForkResult(
      provider=provider,
      source_session_id=session_id,
      forked_session_id="forked-session",
      answer="coached",
    )

  monkeypatch.setattr(fork_session_module, "fork_session", fake_fork)

  assert fork_session_module.main([
    "--json", "source-session", "/data", "coach this",
  ]) == 0
  assert seen["args"] == (
    "claude", "source-session", "/data", "coach this",
  )
  assert json.loads(capsys.readouterr().out)["provider"] == "claude"


def test_explicit_session_helper_provider_is_unchanged():
  args = _parse_invocation([
    "codex", "source-session", "/data", "coach this",
  ])

  assert (args.provider, args.session_id, args.cwd, args.prompt) == (
    "codex", "source-session", "/data", "coach this",
  )


def test_claude_uses_exact_fork_and_reports_distinct_session():
  seen = {}

  def runner(args, **kwargs):
    seen["args"] = args
    seen["kwargs"] = kwargs
    return subprocess.CompletedProcess(
      args,
      0,
      stdout=json.dumps({"session_id": "fork-session", "result": "reflection"}),
      stderr="",
    )

  result = _fork_claude("source-session", "/data", "coach this", runner=runner)

  assert result == ForkResult(
    provider="claude",
    source_session_id="source-session",
    forked_session_id="fork-session",
    answer="reflection",
  )
  assert seen["args"][:5] == [
    "claude", "--resume", "source-session", "--fork-session", "--print"
  ]
  assert seen["kwargs"]["cwd"] == "/data"
  assert seen["args"][-6:] == [
    "--output-format",
    "json",
    "--restricted",
    "--strict-mcp-config",
    "--tools",
    "",
  ]
  assert all(
    name not in seen["kwargs"]["env"]
    for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN")
  )


def test_side_question_fork_answers_on_the_chats_model_and_is_never_saved():
  seen = {}

  def runner(args, **kwargs):
    seen["args"] = args
    return subprocess.CompletedProcess(
      args,
      0,
      stdout=json.dumps({"session_id": "fork-session", "result": "an answer"}),
      stderr="",
    )

  _fork_claude(
    "source-session", "/data", "side question",
    model="claude-haiku-5-5", ephemeral=True, runner=runner,
  )

  args = seen["args"]
  # Without --model the CLI would answer with its own default model.
  assert args[args.index("--model") + 1] == "claude-haiku-5-5"
  assert "--no-session-persistence" in args
  assert args.index("--fork-session") < args.index("--print")
  # Still a tool-less exact fork.
  assert args[-2:] == ["--tools", ""]


def test_coaching_fork_keeps_its_saved_session_and_default_model():
  seen = {}

  def runner(args, **kwargs):
    seen["args"] = args
    return subprocess.CompletedProcess(
      args, 0, stdout=json.dumps({"session_id": "fork", "result": "r"}), stderr="",
    )

  _fork_claude("source", "/data", "coach", runner=runner)
  assert "--model" not in seen["args"]
  assert "--no-session-persistence" not in seen["args"]


def test_cli_passes_side_question_options_only_when_given(monkeypatch, capsys):
  seen = []

  def fake_fork(provider, session_id, cwd, prompt, **options):
    seen.append(options)
    return ForkResult(
      provider=provider, source_session_id=session_id,
      forked_session_id="fork", answer="ok",
    )

  monkeypatch.setattr(fork_session_module, "fork_session", fake_fork)
  assert fork_session_module.main([
    "--json", "--ephemeral", "--model", "gpt-5.5",
    "codex", "thread", "/data", "side question",
  ]) == 0
  assert fork_session_module.main(["--json", "codex", "thread", "/data", "coach"]) == 0
  assert seen == [{"model": "gpt-5.5", "ephemeral": True}, {}]
  capsys.readouterr()


@pytest.mark.parametrize(
  ("payload", "expected"),
  [
    ({"session_id": "source", "result": "answer"}, "instead of a fork"),
    ({"session_id": "fork", "result": ""}, "empty coaching response"),
    ({"session_id": "", "result": "answer"}, "did not return a forked"),
  ],
)
def test_claude_fails_closed_without_a_valid_exact_fork(payload, expected):
  def runner(args, **kwargs):
    return subprocess.CompletedProcess(
      args, 0, stdout=json.dumps(payload), stderr=""
    )

  with pytest.raises(ForkError, match=expected):
    _fork_claude("source", "/data", "coach", runner=runner)


def test_codex_uses_sdk_thread_fork_and_read_only_turn(tmp_path, monkeypatch):
  from app.codex_session_lock import try_acquire_codex_session_sweep

  calls = {}
  data_dir = tmp_path
  monkeypatch.setenv("CODEX_HOME", str(data_dir / "cli-auth" / "codex"))
  for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN"):
    monkeypatch.setenv(name, f"secret-{name}")

  def mcp_inventory_runner(args, **kwargs):
    calls["mcp_inventory"] = (args, kwargs)
    return subprocess.CompletedProcess(
      args,
      0,
      stdout=json.dumps([
        {"name": "mobius_control", "enabled": True},
        {"name": "owner.connector", "enabled": True},
      ]),
      stderr="",
    )

  class FakeConfig:
    def __init__(self, **kwargs):
      calls["config"] = kwargs

  class FakeApprovalMode:
    deny_all = "deny_all"

  class FakeSandbox:
    read_only = "read-only"

  class FakeThread:
    id = "forked-codex-thread"

    async def run(self, prompt, **kwargs):
      calls["run"] = (prompt, kwargs)
      return SimpleNamespace(error=None, final_response="codex reflection")

  class FakeClient:
    async def request(self, method, params, *, response_model):
      calls.setdefault("mcp_checks", []).append((method, params))
      return response_model.model_validate(_app_server_page(method))

  class FakeCodex:
    def __init__(self, config):
      calls["codex_config"] = config
      self._client = FakeClient()

    async def __aenter__(self):
      calls["exclusive_during_fork"] = try_acquire_codex_session_sweep(data_dir)
      return self

    async def __aexit__(self, *args):
      return None

    async def thread_fork(self, source_session_id, **kwargs):
      calls["fork"] = (source_session_id, kwargs)
      return FakeThread()

  result = asyncio.run(
    _fork_codex_async(
      "source-codex-thread",
      "/data",
      "coach this",
      sdk_loader=lambda: (
        FakeCodex,
        FakeConfig,
        FakeApprovalMode,
        FakeSandbox,
        None,
      ),
      mcp_inventory_runner=mcp_inventory_runner,
    )
  )

  assert result == ForkResult(
    provider="codex",
    source_session_id="source-codex-thread",
    forked_session_id="forked-codex-thread",
    answer="codex reflection",
  )
  assert calls["fork"] == (
    "source-codex-thread",
    {"approval_mode": "deny_all", "cwd": "/data", "sandbox": "read-only"},
  )
  assert calls["run"] == (
    "coach this",
    {"approval_mode": "deny_all", "cwd": "/data", "sandbox": "read-only"},
  )
  assert calls["config"]["config_overrides"] == (
    "features.apps=false",
    "features.enable_mcp_apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.skill_mcp_dependency_install=false",
    'mcp_servers={"mobius_control"={enabled=false},'
    '"owner.connector"={enabled=false}}',
    # A forked session must not regain Codex's native sub-agents either.
    "agents.enabled=false",
    "features.multi_agent=false",
    "features.multi_agent_v2.enabled=false",
    # Built-in tools off, like a Claude fork's --tools "": no shell, file or
    # image reads, browser, apps, or web, and no code runner.
    "features.shell_tool=false",
    "features.apps=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.browser_use_full_cdp_access=false",
    "features.computer_use=false",
    "features.image_generation=false",
    "features.goals=false",
    "features.view_image=false",
    "features.sleep_tool=false",
    "features.code_mode_host=false",
    "features.tool_suggest=false",
    'web_search="disabled"',
    "skills.include_instructions=false",
  )
  assert all(
    name not in calls["config"]["env"]
    for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN")
  )
  assert calls["mcp_checks"] == [
    (
      "mcpServerStatus/list",
      {
        "threadId": "forked-codex-thread",
        "detail": "full",
        "limit": 100,
      },
    ),
    ("experimentalFeature/list", {"threadId": "forked-codex-thread"}),
  ]
  assert calls["mcp_inventory"][0] == ["codex", "mcp", "list", "--json"]
  assert calls["mcp_inventory"][1]["cwd"] == "/data"
  assert calls["exclusive_during_fork"] is None
  after = try_acquire_codex_session_sweep(data_dir)
  assert after is not None
  after.release()


def test_codex_side_question_fork_is_ephemeral_and_uses_the_chats_model(
  tmp_path, monkeypatch,
):
  # The fork derives its lock directory from CODEX_HOME; without this pin the
  # test would reach for the instance's real /data, which CI does not have.
  monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cli-auth" / "codex"))
  calls = {}

  class FakeConfig:
    def __init__(self, **kwargs):
      calls["config"] = kwargs

  class FakeApprovalMode:
    deny_all = "deny_all"

  class FakeSandbox:
    read_only = "read-only"

  class FakeThread:
    id = "forked-codex-thread"

    async def run(self, prompt, **kwargs):
      calls["run"] = kwargs
      return SimpleNamespace(error=None, final_response="side answer")

  class FakeClient:
    async def request(self, method, params, *, response_model):
      return response_model.model_validate(_app_server_page(method))

  class FakeCodex:
    def __init__(self, config):
      self._client = FakeClient()

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def thread_fork(self, source_session_id, **kwargs):
      calls["fork"] = kwargs
      return FakeThread()

  def mcp_inventory_runner(args, **kwargs):
    return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

  result = asyncio.run(_fork_codex_async(
    "source-codex-thread", "/data", "side question",
    model="gpt-5.5", ephemeral=True,
    sdk_loader=lambda: (FakeCodex, FakeConfig, FakeApprovalMode, FakeSandbox, None),
    mcp_inventory_runner=mcp_inventory_runner,
  ))

  assert result.answer == "side answer"
  assert calls["fork"]["ephemeral"] is True
  # Codex refuses an ephemeral fork that would return its turns.
  assert calls["fork"]["include_turns"] is False
  assert calls["fork"]["sandbox"] == "read-only"
  assert calls["run"]["model"] == "gpt-5.5"


def test_codex_mcp_inventory_is_encoded_as_one_exact_disable_map():
  def runner(args, **kwargs):
    return subprocess.CompletedProcess(
      args,
      0,
      stdout=json.dumps([
        {"name": 'quote"server', "enabled": True},
        {"name": "dot.server", "enabled": True},
        {"name": "dot.server", "enabled": False},
      ]),
      stderr="",
    )

  overrides = _codex_mcp_isolation_overrides(
    "/workspace",
    {"CODEX_HOME": "/tmp/codex-home"},
    runner=runner,
  )

  assert overrides[-1] == (
    'mcp_servers={"dot.server"={enabled=false},'
    '"quote\\\"server"={enabled=false}}'
  )


@pytest.mark.parametrize(
  ("returncode", "stdout", "expected"),
  [
    (1, "[]", "inventory failed"),
    (0, "not-json", "inventory was malformed"),
    (0, "{}", "unexpected shape"),
    (0, '[{"name": ""}]', "invalid server name"),
  ],
)
def test_codex_mcp_inventory_fails_closed(returncode, stdout, expected):
  def runner(args, **kwargs):
    return subprocess.CompletedProcess(
      args, returncode, stdout=stdout, stderr="private detail"
    )

  with pytest.raises(ForkError, match=expected):
    _codex_mcp_isolation_overrides(
      "/workspace",
      {"CODEX_HOME": "/tmp/codex-home"},
      runner=runner,
    )


def test_codex_fails_before_coaching_when_effective_mcp_tools_survive():
  class FakeClient:
    async def request(self, method, params, *, response_model):
      assert method == "mcpServerStatus/list"
      return response_model.model_validate({
        "data": [{
          "name": "managed-server",
          "tools": {"preapproved_write": {}},
          "resources": [],
          "resourceTemplates": [],
        }],
      })

  fake_codex = SimpleNamespace(_client=FakeClient())

  with pytest.raises(ForkError, match="exposed an MCP capability"):
    asyncio.run(_assert_codex_mcp_isolated(fake_codex, "forked-thread"))


def _features_client(pages):
  class FakeClient:
    async def request(self, method, params, *, response_model):
      assert method == "experimentalFeature/list"
      return response_model.model_validate(pages[params.get("cursor")])

  return SimpleNamespace(_client=FakeClient())


@pytest.mark.parametrize(
  "view_image",
  [
    None,  # a switch this Codex does not know
    {"name": "view_image", "enabled": True, "stage": "stable"},
    {"name": "view_image", "enabled": False, "stage": "removed"},
  ],
)
def test_codex_fork_refuses_a_tool_switch_that_did_not_take(view_image):
  features = [
    {"name": name, "enabled": False, "stage": "stable"}
    for name in CODEX_TOOL_FEATURES_OFF if name != "view_image"
  ] + ([view_image] if view_image else [])
  codex = _features_client({
    None: _app_server_page("experimentalFeature/list", features=features),
  })

  with pytest.raises(ForkError, match="could not switch off its view_image tool"):
    asyncio.run(_assert_codex_tools_off(codex, "forked-thread"))


def test_codex_fork_reads_every_page_of_tool_switches():
  def page(names, cursor):
    return {
      "data": [{"name": name, "enabled": False, "stage": "stable"} for name in names],
      "nextCursor": cursor,
    }

  first, rest = CODEX_TOOL_FEATURES_OFF[:1], CODEX_TOOL_FEATURES_OFF[1:]
  codex = _features_client({None: page(first, "page-2"), "page-2": page(rest, None)})

  asyncio.run(_assert_codex_tools_off(codex, "forked-thread"))


def test_codex_default_loader_accepts_persisted_completed_subagent_activity():
  # Coaching uses the same SDK import contract as live chat, and the native
  # enum accepts the completion marker in persisted activity.
  pytest.importorskip("openai_codex")
  from openai_codex.generated.v2_all import SubAgentActivityKind

  assert SubAgentActivityKind("completed").value == "completed"
  loaded = _load_codex_sdk()
  assert all(loaded)


def _seed_db(
  path: Path,
  *,
  provider="codex",
  session_id="source-session",
  deleted_at=None,
  messages="transcript must never be used",
):
  with sqlite3.connect(path) as con:
    con.execute(
      "create table chats (id text primary key, provider text, session_id text, "
      "messages text, deleted_at text)"
    )
    con.execute(
      "insert into chats values (?, ?, ?, ?, ?)",
      ("chat-1", provider, session_id, messages, deleted_at),
    )


def test_chat_coaching_delegates_only_to_its_exact_provider_session(tmp_path):
  db_dir = tmp_path / "db"
  db_dir.mkdir()
  _seed_db(db_dir / "ultimate.db")
  seen = {}

  def driver(provider, session_id, cwd, prompt, *, after_call_id):
    seen["args"] = (provider, session_id, cwd, prompt, after_call_id)
    return ForkResult(
      provider=provider,
      source_session_id=session_id,
      forked_session_id="forked-session",
      answer="answer",
    )

  payload = coach_chat("chat-1", "coach", data_dir=tmp_path, driver=driver)

  assert seen["args"] == (
    "codex", "source-session", str(tmp_path), "coach", None,
  )
  assert payload == {
    "chat_id": "chat-1",
    "provider": "codex",
    "source_session_id": "source-session",
    "forked_session_id": "forked-session",
    "answer": "answer",
    "method": "session_fork",
    "exact_session_fork": True,
    "after_call_id": None,
  }


def test_chat_can_recover_same_provider_exact_session_link(tmp_path):
  db = tmp_path / "ultimate.db"
  _seed_db(db, session_id="")
  with sqlite3.connect(db) as con:
    con.execute(
      "create table chat_session_links (provider text, session_id text, "
      "chat_id text, last_seen_at text)"
    )
    con.execute(
      "insert into chat_session_links values (?, ?, ?, ?)",
      ("codex", "older", "chat-1", "2026-08-30"),
    )
    con.execute(
      "insert into chat_session_links values (?, ?, ?, ?)",
      ("codex", "newest", "chat-1", "2026-08-31"),
    )

  assert _chat_session(db, "chat-1") == ("codex", "newest")


def test_chat_without_exact_session_fails_instead_of_reseeding(tmp_path):
  db = tmp_path / "ultimate.db"
  _seed_db(db, session_id="", messages='[{"role":"user","content":"seed me"}]')

  with pytest.raises(ForkError, match="no exact provider session"):
    _chat_session(db, "chat-1")


def test_deleted_chat_cannot_be_coached(tmp_path):
  db = tmp_path / "ultimate.db"
  _seed_db(db, deleted_at="2026-08-31")

  with pytest.raises(ForkError, match="deleted chats cannot be forked"):
    _chat_session(db, "chat-1")


# --- Call moments: fork the provider session at one tool call ---------------

CLAUDE_SESSION = "11111111-2222-3333-4444-555555555555"
CODEX_THREAD = "01a0c949-5dea-7260-820a-a2e43a6dbb93"


def _write_jsonl(path: Path, entries):
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(
    "".join(json.dumps(entry) + "\n" for entry in entries) + '{"partial',
    encoding="utf-8",
  )


def _claude_transcript(config_dir: Path):
  def tool_use(uuid, call_id):
    return {
      "type": "assistant",
      "uuid": uuid,
      "message": {"content": [{"type": "tool_use", "id": call_id}]},
    }

  def tool_result(uuid, call_id):
    return {
      "type": "user",
      "uuid": uuid,
      "sourceToolAssistantUUID": f"a-{call_id}",
      "message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]},
    }

  _write_jsonl(
    config_dir / "projects" / "-data" / f"{CLAUDE_SESSION}.jsonl",
    [
      {"type": "user", "uuid": "u-prompt", "message": {"content": "hi"}},
      tool_use("a-toolu_A", "toolu_A"),
      tool_use("a-toolu_B", "toolu_B"),
      tool_result("u-result-A", "toolu_A"),
      tool_result("u-result-B", "toolu_B"),
      {"type": "assistant", "uuid": "a-later", "message": {"content": "later"}},
    ],
  )


def test_claude_call_moment_resumes_at_that_calls_tool_result_entry(
  tmp_path, monkeypatch
):
  _claude_transcript(tmp_path)
  monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
  seen = {}

  def runner(args, **kwargs):
    seen["args"] = args
    return subprocess.CompletedProcess(
      args,
      0,
      stdout=json.dumps({"session_id": "fork", "result": "at the moment"}),
      stderr="",
    )

  result = _fork_claude(
    CLAUDE_SESSION, "/data", "coach", after_call_id="toolu_A", runner=runner,
  )

  assert seen["args"] == [
    "claude", "--resume", CLAUDE_SESSION, "--fork-session",
    "--resume-session-at=u-result-A",
    "--print", "coach", "--output-format", "json",
    "--restricted", "--strict-mcp-config", "--tools", "",
  ]
  assert result.after_call_id == "toolu_A"
  assert result.forked_session_id == "fork"


def test_claude_call_moment_without_recorded_result_fails_before_forking(
  tmp_path, monkeypatch
):
  _claude_transcript(tmp_path)
  monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

  def runner(args, **kwargs):
    raise AssertionError("must not fork the whole session instead")

  with pytest.raises(ForkError, match="no recorded result for call toolu_Z"):
    _fork_claude(
      CLAUDE_SESSION, "/data", "coach", after_call_id="toolu_Z", runner=runner,
    )
  with pytest.raises(ForkError, match="not a UUID"):
    _fork_claude("../*", "/data", "coach", after_call_id="toolu_A", runner=runner)


def _codex_rollout_file(codex_home: Path, entries) -> Path:
  path = (
    codex_home / "sessions" / "2026" / "09" / "25"
    / f"rollout-2026-09-25T10-00-00-{CODEX_THREAD}.jsonl"
  )
  _write_jsonl(path, entries)
  return path


def _item(kind, **fields):
  return {"type": "response_item", "payload": {"type": kind, **fields}}


def _turn_started(turn_id):
  return {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}


def _codex_entries():
  # The shape Codex 0.157 writes: each turn opens with task_started, and the
  # turn's items (developer context included) follow it.
  return [
    {"type": "session_meta", "payload": {"id": CODEX_THREAD}},
    _turn_started("turn-1"),
    _item("message", id="msg_1", role="user", content="do the thing"),
    {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
    _item("custom_tool_call", id="ctc_A", call_id="call_A", name="exec"),
    _item("custom_tool_call_output", call_id="call_A"),
    _item("message", id="msg_2", role="assistant", content="later in turn 1"),
    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
    _turn_started("turn-2"),
    _item("custom_tool_call", id="ctc_B", call_id="call_B", name="exec"),
    _item("custom_tool_call_output", call_id="call_B"),
  ]


@pytest.mark.parametrize(("call_id", "turn_id"), [("ctc_A", "turn-1"), ("ctc_B", "turn-2")])
def test_codex_call_moment_is_the_turn_that_made_the_call(tmp_path, call_id, turn_id):
  rollout = _codex_rollout_file(tmp_path, _codex_entries())

  assert _codex_turn_of_call(rollout, call_id) == turn_id


@pytest.mark.parametrize(
  ("entries", "call_id", "expected"),
  [
    (_codex_entries(), "ctc_missing", "no recorded call ctc_missing"),
    (
      [_item("custom_tool_call", id="ctc_A", call_id="call_A")],
      "ctc_A",
      "records call ctc_A outside a turn",
    ),
  ],
)
def test_codex_call_moment_without_a_recorded_turn_fails(
  tmp_path, entries, call_id, expected
):
  rollout = _codex_rollout_file(tmp_path, entries)

  with pytest.raises(ForkError, match=expected):
    _codex_turn_of_call(rollout, call_id)


def test_codex_call_moment_forks_the_session_through_the_calls_turn(
  tmp_path, monkeypatch
):
  codex_home = tmp_path / "cli-auth" / "codex"
  _codex_rollout_file(codex_home, _codex_entries())
  monkeypatch.setenv("CODEX_HOME", str(codex_home))
  monkeypatch.setattr(fork_session_module.shutil, "which", lambda name: f"/bin/{name}")
  calls = {"requests": []}

  class FakeConfig:
    def __init__(self, **kwargs):
      calls["config"] = kwargs

  class FakeThread:
    def __init__(self, codex, thread_id):
      self.id = thread_id

    async def run(self, prompt, **kwargs):
      calls["run"] = (prompt, kwargs)
      return SimpleNamespace(error=None, final_response="coached")

  class FakeClient:
    async def thread_fork(self, thread_id, params):
      calls["fork"] = (thread_id, params)
      return SimpleNamespace(thread=SimpleNamespace(id="moment-thread"))

    async def request(self, method, params, *, response_model):
      calls["requests"].append((method, params))
      return response_model.model_validate(_app_server_page(method))

  class FakeCodex:
    def __init__(self, config):
      self._client = FakeClient()

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def thread_fork(self, *args, **kwargs):
      raise AssertionError("a call moment must not fork the whole session")

  result = asyncio.run(
    _fork_codex_async(
      CODEX_THREAD,
      "/data",
      "coach",
      after_call_id="ctc_A",
      sdk_loader=lambda: (
        FakeCodex,
        FakeConfig,
        SimpleNamespace(deny_all="deny_all"),
        SimpleNamespace(read_only="read-only"),
        FakeThread,
      ),
      mcp_inventory_runner=lambda args, **kw: subprocess.CompletedProcess(
        args, 0, stdout="[]", stderr="",
      ),
    )
  )

  assert calls["config"]["codex_bin"] == "/bin/codex"
  assert calls["fork"] == (CODEX_THREAD, {
    "lastTurnId": "turn-1",
    "cwd": "/data",
    "approvalPolicy": "never",
    "sandbox": "read-only",
  })
  assert [method for method, _ in calls["requests"]] == [
    "mcpServerStatus/list", "experimentalFeature/list",
  ]
  assert calls["requests"][0][1]["threadId"] == "moment-thread"
  assert calls["run"] == (
    "coach",
    {"approval_mode": "deny_all", "cwd": "/data", "sandbox": "read-only"},
  )
  assert result == ForkResult(
    provider="codex",
    source_session_id=CODEX_THREAD,
    forked_session_id="moment-thread",
    answer="coached",
    after_call_id="ctc_A",
  )
  assert (result.method, result.exact_session_fork) == ("session_fork", True)


def _seed_run(db: Path, *, run_id="run-1", chat_id="chat-1", provider="codex",
              provider_session_id="run-session"):
  with sqlite3.connect(db) as con:
    con.execute(
      "create table if not exists chat_runs (id text primary key, chat_id text, "
      "provider text, provider_session_id text)"
    )
    con.execute(
      "insert into chat_runs values (?, ?, ?, ?)",
      (run_id, chat_id, provider, provider_session_id),
    )


def _moment(**overrides):
  return {
    "chat_id": "chat-1",
    "run_id": "run-1",
    "provider": "codex",
    "call_id": "ctc_A",
    **overrides,
  }


def _moment_db(tmp_path):
  db_dir = tmp_path / "db"
  db_dir.mkdir()
  db = db_dir / "ultimate.db"
  _seed_db(db, session_id="chat-current-session")
  return db


def test_chat_call_moment_forks_the_runs_session_after_that_call(tmp_path):
  _seed_run(_moment_db(tmp_path))
  seen = {}

  def driver(provider, session_id, cwd, prompt, *, after_call_id):
    seen["args"] = (provider, session_id, cwd, prompt, after_call_id)
    return ForkResult(
      provider=provider,
      source_session_id=session_id,
      forked_session_id="fork",
      answer="answer",
      after_call_id=after_call_id,
    )

  payload = coach_chat(
    "chat-1", "coach", moment=_moment(), data_dir=tmp_path, driver=driver,
  )

  assert seen["args"] == ("codex", "run-session", str(tmp_path), "coach", "ctc_A")
  assert payload["after_call_id"] == "ctc_A"


@pytest.mark.parametrize(
  ("run", "moment", "expected"),
  [
    ({}, _moment(chat_id="chat-2"), "different chat"),
    ({"chat_id": "chat-2"}, _moment(), "run not found in this chat"),
    ({}, _moment(run_id="run-missing"), "run not found in this chat"),
    ({"provider": "claude"}, _moment(), "does not match its run"),
    ({"provider": None}, _moment(), "does not match its run"),
    ({"provider_session_id": None}, _moment(), "no exact provider session"),
    ({}, _moment(call_id=""), "needs string chat_id"),
    ({}, ["not", "an", "object"], "needs string chat_id"),
  ],
)
def test_chat_call_moment_mismatch_fails_without_whole_session_fallback(
  tmp_path, run, moment, expected
):
  _seed_run(_moment_db(tmp_path), **run)

  def driver(*args, **kwargs):
    raise AssertionError("a bad moment must never reach any fork")

  with pytest.raises(ForkError, match=expected):
    coach_chat("chat-1", "coach", moment=moment, data_dir=tmp_path, driver=driver)


def test_chat_call_moment_cli_passes_the_parsed_moment(
  tmp_path, monkeypatch, capsys
):
  _seed_run(_moment_db(tmp_path), provider_session_id=None)
  monkeypatch.setenv("DATA_DIR", str(tmp_path))

  assert fork_chat_main([
    "--after-call", json.dumps(_moment()), "chat-1", "coach",
  ]) == 1
  assert "no exact provider session" in capsys.readouterr().err
