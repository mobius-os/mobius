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
  _codex_history_through_call,
  _codex_mcp_isolation_overrides,
  _fork_claude,
  _fork_codex_async,
  _load_codex_sdk,
  _parse_invocation,
)
from fork_chat import _chat_session, coach_chat, main as fork_chat_main  # noqa: E402


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
      return response_model.model_validate({"data": [], "nextCursor": None})

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
  )
  assert all(
    name not in calls["config"]["env"]
    for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN")
  )
  assert calls["mcp_checks"] == [(
    "mcpServerStatus/list",
    {
      "threadId": "forked-codex-thread",
      "detail": "full",
      "limit": 100,
    },
  )]
  assert calls["mcp_inventory"][0] == ["codex", "mcp", "list", "--json"]
  assert calls["mcp_inventory"][1]["cwd"] == "/data"
  assert calls["exclusive_during_fork"] is None
  after = try_acquire_codex_session_sweep(data_dir)
  assert after is not None
  after.release()


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


def test_codex_default_loader_accepts_persisted_completed_subagent_activity():
  # Coaching must cross the same provider-compatibility boundary as live chat.
  # The pinned app-server persists this lifecycle marker even though its
  # generated Python enum omits it until the boundary installs the exact shim.
  pytest.importorskip("openai_codex")
  from openai_codex.generated.v2_all import SubAgentActivityKind

  loaded = _load_codex_sdk()

  assert SubAgentActivityKind("completed").value == "completed"
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


# --- Call moments: fork right after one tool call's result -----------------

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


def _codex_entries():
  return [
    {
      "type": "session_meta",
      "payload": {
        "id": CODEX_THREAD,
        "base_instructions": {"text": "constitution"},
        "model_provider": "openai",
      },
    },
    {"type": "turn_context", "payload": {"model": "gpt-source"}},
    _item("message", role="developer", content="source permissions"),
    _item("message", role="user", content="do the thing"),
    _item("custom_tool_call", id="ctc_A", call_id="call_A", name="exec"),
    _item("custom_tool_call", id="ctc_B", call_id="call_B", name="exec"),
    _item("custom_tool_call_output", id="ctco_B", call_id="call_B"),
    _item("custom_tool_call_output", id="ctco_A", call_id="call_A"),
    _item("message", role="assistant", content="after the call"),
  ]


def test_codex_call_moment_history_ends_at_that_calls_output(tmp_path):
  rollout = _codex_rollout_file(tmp_path, _codex_entries())

  cut = _codex_history_through_call(rollout, "ctc_B")

  assert [item.get("id") for item in cut.items] == [None, "ctc_A", "ctc_B", "ctco_B"]
  assert all(item.get("role") != "developer" for item in cut.items)
  assert (cut.base_instructions, cut.model, cut.model_provider) == (
    "constitution", "gpt-source", "openai",
  )


def test_codex_call_moment_replays_compaction_instead_of_older_items(tmp_path):
  entries = _codex_entries()
  entries.insert(4, {
    "type": "compacted",
    "payload": {"replacement_history": [
      {"type": "message", "role": "developer", "content": "stale"},
      {"type": "compaction", "encrypted_content": "summary"},
    ]},
  })
  rollout = _codex_rollout_file(tmp_path, entries)

  cut = _codex_history_through_call(rollout, "ctc_A")

  assert [item["type"] for item in cut.items] == [
    "compaction", "custom_tool_call", "custom_tool_call",
    "custom_tool_call_output", "custom_tool_call_output",
  ]


@pytest.mark.parametrize(
  ("entries", "call_id", "expected"),
  [
    (_codex_entries(), "ctc_missing", "no recorded call ctc_missing"),
    (_codex_entries()[:6], "ctc_A", "no recorded output for call ctc_A"),
  ],
)
def test_codex_call_moment_without_recorded_call_output_fails(
  tmp_path, entries, call_id, expected
):
  rollout = _codex_rollout_file(tmp_path, entries)

  with pytest.raises(ForkError, match=expected):
    _codex_history_through_call(rollout, call_id)


def test_codex_call_moment_starts_a_thread_seeded_only_through_the_call(
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
    id = "moment-thread"

    async def run(self, prompt, **kwargs):
      calls["run"] = prompt
      return SimpleNamespace(error=None, final_response="coached")

  class FakeClient:
    async def request(self, method, params, *, response_model):
      calls["requests"].append((method, params))
      return response_model.model_validate({"data": []})

  class FakeCodex:
    def __init__(self, config):
      self._client = FakeClient()

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def thread_fork(self, *args, **kwargs):
      raise AssertionError("a call moment must not fork the whole thread")

    async def thread_start(self, **kwargs):
      calls["start"] = kwargs
      return FakeThread()

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
      ),
      mcp_inventory_runner=lambda args, **kw: subprocess.CompletedProcess(
        args, 0, stdout="[]", stderr="",
      ),
    )
  )

  assert calls["config"]["codex_bin"] == "/bin/codex"
  assert calls["start"] == {
    "approval_mode": "deny_all",
    "base_instructions": "constitution",
    "cwd": "/data",
    "model": "gpt-source",
    "model_provider": "openai",
    "sandbox": "read-only",
  }
  methods = [method for method, _ in calls["requests"]]
  assert methods == ["mcpServerStatus/list", "thread/inject_items"]
  injected = calls["requests"][1][1]
  assert injected["threadId"] == "moment-thread"
  assert injected["items"][-1]["id"] == "ctco_A"
  assert "after the call" not in json.dumps(injected["items"])
  assert result == ForkResult(
    provider="codex",
    source_session_id=CODEX_THREAD,
    forked_session_id="moment-thread",
    answer="coached",
    after_call_id="ctc_A",
  )


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
