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

from fork_session import (  # noqa: E402
  ForkError,
  ForkResult,
  _fork_claude,
  _fork_codex_async,
  _load_codex_sdk,
)
from fork_chat import _chat_session, coach_chat  # noqa: E402


def test_platform_coaching_helpers_are_directly_executable():
  for name in ("fork-chat.sh", "fork-session.sh", "fork_chat.py", "fork_session.py"):
    assert (SCRIPTS / name).stat().st_mode & stat.S_IXUSR


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
  assert "--output-format" in seen["args"]
  assert "json" in seen["args"]


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


def test_codex_uses_sdk_thread_fork_and_read_only_turn():
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
      calls["run"] = (prompt, kwargs)
      return SimpleNamespace(error=None, final_response="codex reflection")

  class FakeCodex:
    def __init__(self, config):
      calls["codex_config"] = config

    async def __aenter__(self):
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

  def driver(provider, session_id, cwd, prompt):
    seen["args"] = (provider, session_id, cwd, prompt)
    return ForkResult(
      provider=provider,
      source_session_id=session_id,
      forked_session_id="forked-session",
      answer="answer",
    )

  payload = coach_chat("chat-1", "coach", data_dir=tmp_path, driver=driver)

  assert seen["args"] == ("codex", "source-session", str(tmp_path), "coach")
  assert payload == {
    "chat_id": "chat-1",
    "provider": "codex",
    "source_session_id": "source-session",
    "forked_session_id": "forked-session",
    "answer": "answer",
    "method": "session_fork",
    "exact_session_fork": True,
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
