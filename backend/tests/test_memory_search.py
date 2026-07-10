"""Tests for the memory-search subagent runner (backend/scripts/memory_search.py).

The headline test is the boot smoke-test: the runner imports `app.memory` /
`app.memory_trace` for read-tracking, but the chat agent and the reflection runner
invoke it from cwd `/data` with no `PYTHONPATH`. Its `sys.path.insert` must make
those imports resolve regardless of cwd — otherwise `_path_to_node_id` raises
`ModuleNotFoundError` and the WHOLE search synthesis is silently lost (the latent
bug a live reflection pass caught 2026-06-24). Plus coverage for the depth/breadth
hint parsing (a flag with no value used to silently become the query text).
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memory_search.py"


def _load():
  spec = importlib.util.spec_from_file_location("memory_search", SCRIPT)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


# --- the boot smoke-test (guards the cwd-import bug class) ----------------


def test_imports_app_from_a_foreign_cwd(tmp_path):
  # Exec the runner's module body (which runs its sys.path.insert) from a cwd
  # that does NOT have backend/ on the path and with PYTHONPATH cleared, then do
  # the exact imports the read-tracking path does. They must resolve via the
  # script's own sys.path fix — if it regresses, this raises ModuleNotFoundError.
  prog = (
    "import importlib.util\n"
    f"spec = importlib.util.spec_from_file_location('ms', r'{SCRIPT}')\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"
    "from app.memory import _loaded_path_to_id\n"
    "from app.memory_trace import record_note_read\n"
    "print('IMPORT_OK')\n"
  )
  env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
  r = subprocess.run(
    [sys.executable, "-c", prog],
    cwd=str(tmp_path),
    env=env,
    capture_output=True,
    text=True,
  )
  assert "ModuleNotFoundError" not in r.stderr, r.stderr
  assert "IMPORT_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


# --- depth/breadth hint parsing ------------------------------------------


def test_parse_args_positional_and_hints():
  ms = _load()
  pos, depth, breadth = ms._parse_args(["the request", "chat-9", "--max-depth", "3"])
  assert pos == ["the request", "chat-9"]
  assert (depth, breadth) == (3, None)


def test_parse_args_rejects_flag_without_value():
  ms = _load()
  with pytest.raises(ValueError):
    ms._parse_args(["q", "--max-depth"])


def test_parse_args_rejects_non_int():
  ms = _load()
  with pytest.raises(ValueError):
    ms._parse_args(["q", "--max-breadth", "lots"])


def test_hints_clause_empty_without_hints_and_soft_with_them():
  ms = _load()
  assert ms._hints_clause(None, None) == ""
  clause = ms._hints_clause(2, 5)
  assert "SOFT" in clause
  assert "2 hop" in clause and "5 map" in clause


def test_run_usage_error_on_no_query(monkeypatch):
  ms = _load()
  monkeypatch.setattr(ms.sys, "argv", ["memory_search.py"])
  assert ms.run() == 2  # no positional request → usage error, never a crash


# --- slug/source parsing (no graph.json needed) --------------------------


def _identity_path_to_id(ms):
  """A stand-in for _path_to_node_id keyed purely on a path's SHAPE, so the
  slug/source parsers can be tested without a real graph.json. Distinct id
  prefixes (note:/moc:/chat:) let a test assert WHICH candidate matched; only
  the standard directory shapes resolve, everything else is None (as a missing
  node would be)."""

  def stub(file_path: str) -> str | None:
    rel = os.path.relpath(file_path, ms.MEMORY_DIR)
    if rel.startswith("chats/") and rel.endswith("/index.md"):
      return "chat:" + rel[len("chats/") : -len("/index.md")]
    if rel.startswith("notes/") and rel.endswith(".md"):
      return "note:" + rel[len("notes/") : -len(".md")]
    if rel.startswith("mocs/") and rel.endswith(".md"):
      return "moc:" + rel[len("mocs/") : -len(".md")]
    return None

  return stub


def test_slug_to_node_id_shapes(monkeypatch):
  ms = _load()
  monkeypatch.setattr(ms, "_path_to_node_id", _identity_path_to_id(ms))
  # bare slug tries notes first, then mocs, then chats
  assert ms._slug_to_node_id("foo") == "note:foo"
  assert ms._slug_to_node_id("notes/foo") == "note:foo"
  assert ms._slug_to_node_id("mocs/foo") == "moc:foo"
  # the finding-2 bug: all three per-chat spellings resolve to the SAME node,
  # where "chats/<id>/index" used to build "chats/<id>/index/index.md" and miss
  a = ms._slug_to_node_id("chats/abc123")
  b = ms._slug_to_node_id("chats/abc123/index")
  c = ms._slug_to_node_id("chats/abc123/index.md")
  assert a == b == c == "chat:abc123"


def test_sources_to_node_ids_mixed_separators_and_dedupe(monkeypatch):
  ms = _load()
  monkeypatch.setattr(ms, "_path_to_node_id", _identity_path_to_id(ms))
  # a SOURCES line mixing comma + semicolon separators, a duplicate slug, and a
  # per-chat citation in its ".md" spelling — parsed, mapped, and deduped in order
  text = (
    "- Coffee twice daily (notes/coffee).\n"
    "- Homelab named moss (notes/moss).\n"
    "SOURCES: notes/coffee, notes/coffee; mocs/food , chats/xy/index.md\n"
  )
  ids = ms._sources_to_node_ids(text)
  assert ids == ["note:coffee", "moc:food", "chat:xy"]


# --- provider selection / fallback --------------------------------------


def test_resolve_search_agents_uses_background_primary_and_fallback(
  monkeypatch, tmp_path
):
  ms = _load()
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(
    """
    {
      "background_agents": {
        "primary": {"provider": "codex", "model": "gpt-5.5", "effort": "medium"},
        "fallback": {"provider": "claude", "model": "claude-sonnet-4-6"}
      }
    }
    """,
    encoding="utf-8",
  )
  monkeypatch.setattr(ms, "DATA_DIR", tmp_path)

  assert ms._resolve_search_agents() == [
    {"provider": "codex", "model": "gpt-5.5", "effort": "medium"},
    {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
  ]


def test_resolve_search_agents_uses_ordered_provider_list(monkeypatch, tmp_path):
  ms = _load()
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(
    """
    {
      "background_agents": {
        "providers": [
          {"provider": "claude", "model": "claude-sonnet-4-6", "enabled": false},
          {"provider": "codex", "model": "gpt-5.5", "effort": "high", "enabled": true}
        ],
        "primary": {"provider": "claude", "model": "claude-opus-4-8"}
      }
    }
    """,
    encoding="utf-8",
  )
  monkeypatch.setattr(ms, "DATA_DIR", tmp_path)

  assert ms._resolve_search_agents() == [
    {"provider": "codex", "model": "gpt-5.5", "effort": "high"},
  ]


def test_clean_choice_drops_known_cross_provider_model():
  ms = _load()
  assert ms._clean_choice(
    {"provider": "codex", "model": "claude-sonnet-4-6", "effort": "medium"}
  ) == {"provider": "codex", "model": None, "effort": "medium"}


def test_run_falls_back_to_codex_when_claude_is_usage_limited(
  monkeypatch, capsys
):
  ms = _load()
  monkeypatch.setattr(ms, "_path_to_node_id", _identity_path_to_id(ms))
  monkeypatch.setattr(
    ms,
    "_resolve_search_agents",
    lambda: [
      {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
      {"provider": "codex", "model": "gpt-5.5", "effort": "high"},
    ],
  )
  monkeypatch.setattr(ms.sys, "argv", ["memory_search.py", "the request"])
  captured = {"providers": [], "codex_cmd": None, "codex_env": None}

  class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
      self.stdout = stdout
      self.stderr = stderr
      self.returncode = returncode

  def _fake_run(cmd, **kwargs):
    if cmd[0] == ms.CLAUDE_CLI_PATH:
      captured["providers"].append("claude")
      return _Proc(stderr="Usage limit reached", returncode=1)
    if cmd[0] == ms.CODEX_CLI_PATH:
      captured["providers"].append("codex")
      captured["codex_cmd"] = cmd
      captured["codex_env"] = kwargs.get("env", {})
      output_path = Path(cmd[cmd.index("-o") + 1])
      output_path.write_text(
        "- Relevant thing (notes/foo).\nSOURCES: notes/foo\n",
        encoding="utf-8",
      )
      return _Proc(stdout='{"type":"result"}\n', returncode=0)
    raise AssertionError(f"unexpected command: {cmd}")

  monkeypatch.setattr(ms.subprocess, "run", _fake_run)

  assert ms.run() == 0
  out = capsys.readouterr()
  assert "Relevant thing" in out.out
  assert "provider=codex" in out.err
  assert "trying fallback" in out.err
  assert captured["providers"] == ["claude", "codex"]

  cmd = captured["codex_cmd"]
  assert cmd[:2] == [ms.CODEX_CLI_PATH, "exec"]
  for arg in (
    "--skip-git-repo-check",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--json",
  ):
    assert arg in cmd
  assert cmd[cmd.index("-s") + 1] == "read-only"
  assert cmd[cmd.index("-a") + 1] == "never"
  assert "model_reasoning_effort=\"high\"" in cmd
  assert captured["codex_env"]["CODEX_HOME"] == str(ms.CODEX_HOME)


def test_run_falls_back_when_claude_exits_zero_with_an_error_result(
  monkeypatch, capsys
):
  # The Claude CLI mislabels a 401 as subtype="success" and can exit 0 while
  # setting is_error=True on the result event, with the auth-error string as
  # the "result" text. Without honoring is_error, run() would accept that blob
  # as the synthesis and inject the error text into the agent's context as
  # "memories". The fallback must fire on this class and the error text must
  # never reach stdout.
  ms = _load()
  monkeypatch.setattr(ms, "_path_to_node_id", _identity_path_to_id(ms))
  monkeypatch.setattr(
    ms,
    "_resolve_search_agents",
    lambda: [
      {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
      {"provider": "codex", "model": "gpt-5.5", "effort": None},
    ],
  )
  monkeypatch.setattr(ms.sys, "argv", ["memory_search.py", "the request"])
  providers = []

  error_blob = "Invalid authentication credentials. Please run /login (401)."

  class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
      self.stdout = stdout
      self.stderr = stderr
      self.returncode = returncode

  def _fake_run(cmd, **kwargs):
    if cmd[0] == ms.CLAUDE_CLI_PATH:
      providers.append("claude")
      # exit 0, but the result event flags the error and carries the 401 text.
      return _Proc(
        stdout=json.dumps(
          {"type": "result", "is_error": True, "result": error_blob}
        )
        + "\n",
        returncode=0,
      )
    if cmd[0] == ms.CODEX_CLI_PATH:
      providers.append("codex")
      Path(cmd[cmd.index("-o") + 1]).write_text(
        "- Relevant thing (notes/foo).\nSOURCES: notes/foo\n",
        encoding="utf-8",
      )
      return _Proc(stdout='{"type":"result"}\n', returncode=0)
    raise AssertionError(f"unexpected command: {cmd}")

  monkeypatch.setattr(ms.subprocess, "run", _fake_run)

  assert ms.run() == 0
  out = capsys.readouterr()
  assert providers == ["claude", "codex"]
  # the fallback synthesis is served; the 401 blob is never injected as memory.
  assert "Relevant thing" in out.out
  assert error_blob not in out.out
  assert "provider=codex" in out.err


def test_is_provider_failure_honors_is_error_and_markers():
  ms = _load()
  # exit 0 + is_error is a failure (the mislabeled-401 case).
  assert ms._is_provider_failure("clean synthesis", 0, True) is True
  # a clean run with no auth/usage marker in the scanned text is a success.
  assert ms._is_provider_failure("clean synthesis", 0, False) is False
  # an auth marker in the scanned text (stderr) still routes to the fallback.
  assert ms._is_provider_failure("401 not logged in", 0, False) is True


def test_run_accepts_clean_synthesis_that_mentions_a_limit(monkeypatch, capsys):
  # The false-positive guard for the is_error fix: a clean run (exit 0,
  # is_error False) whose SYNTHESIS happens to mention "rate limit" must be
  # served as-is, not misread as a provider failure — run() scans only stderr
  # on a clean zero exit, never the synthesis text.
  ms = _load()
  monkeypatch.setattr(ms, "_path_to_node_id", _identity_path_to_id(ms))
  monkeypatch.setattr(
    ms,
    "_resolve_search_agents",
    lambda: [
      {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
      {"provider": "codex", "model": "gpt-5.5", "effort": None},
    ],
  )
  monkeypatch.setattr(ms.sys, "argv", ["memory_search.py", "the request"])
  providers = []
  synthesis = "- We once hit a rate limit on provider X (notes/foo)."

  class _Proc:
    stderr = ""
    returncode = 0

    def __init__(self, stdout):
      self.stdout = stdout

  def _fake_run(cmd, **kwargs):
    if cmd[0] == ms.CLAUDE_CLI_PATH:
      providers.append("claude")
      return _Proc(
        json.dumps({"type": "result", "is_error": False, "result": synthesis})
        + "\n"
      )
    raise AssertionError("fallback must not run for a clean synthesis")

  monkeypatch.setattr(ms.subprocess, "run", _fake_run)
  assert ms.run() == 0
  out = capsys.readouterr()
  assert providers == ["claude"]
  assert "rate limit" in out.out


# --- the constructed CLI command scopes Bash (the HIGH finding) ----------


def test_run_cmd_scopes_bash_and_denies_write_tools(monkeypatch):
  ms = _load()
  captured = {}

  class _FakeProc:
    stdout = '{"type":"result","result":"No relevant memories."}\n'
    stderr = ""
    returncode = 0

  def _fake_run(cmd, **kwargs):
    captured["cmd"] = cmd
    return _FakeProc()

  monkeypatch.setattr(ms.subprocess, "run", _fake_run)
  monkeypatch.setattr(ms.sys, "argv", ["memory_search.py", "the request"])
  assert ms.run() == 0
  cmd = captured["cmd"]
  # Bash is scoped to read-only verbs, each rule its own argv element ...
  assert "Bash(cat:*)" in cmd
  assert "Bash(rg:*)" in cmd
  assert "Bash(ls:*)" in cmd
  # ... and the unrestricted bare "Bash" grant is gone (the write-capable hole)
  assert "Bash" not in cmd
  # write-shaped tools are denied explicitly, so denial doesn't rely on absence
  assert "--disallowedTools" in cmd
  for tool in ("Write", "Edit", "NotebookEdit"):
    assert tool in cmd
