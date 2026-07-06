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
