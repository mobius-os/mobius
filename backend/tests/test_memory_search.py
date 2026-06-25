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
