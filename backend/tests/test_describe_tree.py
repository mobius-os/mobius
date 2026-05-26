"""Tests for the describe-tree script's description-extraction logic.

These are unit tests against the per-extension extractors. The
script is itself standalone — invoked via subprocess by the agent
or developer — but the extractors are pure functions worth pinning
so a future regression (e.g. a regex that stops matching one of
the supported file types) fails loudly.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the script as a module. It's a script-style file (not under
# a package), so import via spec_from_file_location.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "describe-tree.py"
_spec = importlib.util.spec_from_file_location("describe_tree", _SCRIPT)
describe_tree = importlib.util.module_from_spec(_spec)
sys.modules["describe_tree"] = describe_tree
_spec.loader.exec_module(describe_tree)


# ---------------------------------------------------------------------
# _first_sentence — the cross-language normalizer
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
  ("Hello world.", "Hello world."),
  ("Hello world.\n\nMore details.", "Hello world."),
  ("Hello world\n\nMore details.", "Hello world"),
  ("  Hello   world.  ", "Hello world."),
  ("Multi\nline\nbut one paragraph", "Multi line but one paragraph"),
  ("", ""),
  # No period — return whole first paragraph trimmed.
  ("No terminator here", "No terminator here"),
])
def test_first_sentence(text, expected):
  assert describe_tree._first_sentence(text) == expected


# ---------------------------------------------------------------------
# _extract_python — module docstring
# ---------------------------------------------------------------------

def test_extract_python_triple_double_quoted():
  src = '"""Hello world.\n\nMore detail."""\n\ndef foo(): pass\n'
  assert describe_tree._extract_python(src) == "Hello world."


def test_extract_python_triple_single_quoted():
  src = "'''Hello world.'''\n"
  assert describe_tree._extract_python(src) == "Hello world."


def test_extract_python_skips_shebang_and_comments():
  src = (
    "#!/usr/bin/env python3\n"
    "# coding: utf-8\n"
    "\n"
    '"""The real docstring."""\n'
  )
  assert describe_tree._extract_python(src) == "The real docstring."


def test_extract_python_no_docstring_returns_empty():
  src = "import os\n\ndef foo():\n  pass\n"
  assert describe_tree._extract_python(src) == ""


# ---------------------------------------------------------------------
# _extract_js_ts — leading /* */ or // comments
# ---------------------------------------------------------------------

def test_extract_js_block_comment():
  src = "/* Hello world. */\n\nfunction foo() {}\n"
  assert describe_tree._extract_js_ts(src) == "Hello world."


def test_extract_js_multiline_block_comment_strips_leading_stars():
  src = "/*\n * Hello world.\n * Extra detail.\n */\nexport default 1;\n"
  assert describe_tree._extract_js_ts(src) == "Hello world."


def test_extract_js_line_comments():
  src = "// Hello world.\n// More detail.\nconst x = 1;\n"
  assert describe_tree._extract_js_ts(src) == "Hello world."


def test_extract_js_no_comment_returns_empty():
  src = "import React from 'react'\nexport default function() {}\n"
  assert describe_tree._extract_js_ts(src) == ""


# ---------------------------------------------------------------------
# _extract_shell — leading # lines after shebang
# ---------------------------------------------------------------------

def test_extract_shell_with_shebang():
  src = (
    "#!/bin/bash\n"
    "# Build script for the frontend.\n"
    "# Runs vite build.\n"
    "set -e\n"
  )
  assert describe_tree._extract_shell(src) == "Build script for the frontend."


def test_extract_shell_without_shebang():
  src = "# Trivial script.\necho hi\n"
  assert describe_tree._extract_shell(src) == "Trivial script."


def test_extract_shell_stops_at_first_non_comment():
  src = "# Header.\necho 'no'\n# Later comment\n"
  assert describe_tree._extract_shell(src) == "Header."


# ---------------------------------------------------------------------
# _extract_css + _extract_md
# ---------------------------------------------------------------------

def test_extract_css_block_comment():
  src = "/* Header styles. */\n.h { color: red; }\n"
  assert describe_tree._extract_css(src) == "Header styles."


def test_extract_md_heading():
  src = "# My Feature\n\nDescription here.\n"
  assert describe_tree._extract_md(src) == "My Feature"


def test_extract_md_skips_frontmatter():
  src = (
    "---\n"
    "id: '042'\n"
    "title: My Feature\n"
    "---\n"
    "\n"
    "# Real heading\n"
    "\n"
    "Description.\n"
  )
  assert describe_tree._extract_md(src) == "Real heading"


# ---------------------------------------------------------------------
# walk() integration: directory tree with mixed file types
# ---------------------------------------------------------------------

def test_walk_skips_unknown_extensions(tmp_path):
  (tmp_path / "a.py").write_text('"""A description."""\n')
  (tmp_path / "b.bin").write_text("\x00\x01\x02")
  (tmp_path / "c.png").write_bytes(b"PNG\x89")
  rows = describe_tree.walk(tmp_path, depth=1, quiet=True)
  paths = [p for p, _ in rows]
  assert "a.py" in paths
  assert "b.bin" not in paths
  assert "c.png" not in paths


def test_walk_quiet_skips_files_without_docstring(tmp_path):
  (tmp_path / "documented.py").write_text('"""Has a doc."""\n')
  (tmp_path / "undocumented.py").write_text("import os\n")
  rows = describe_tree.walk(tmp_path, depth=1, quiet=True)
  paths = [p for p, _ in rows]
  assert "documented.py" in paths
  assert "undocumented.py" not in paths


def test_walk_non_quiet_includes_undocumented(tmp_path):
  (tmp_path / "undocumented.py").write_text("import os\n")
  rows = describe_tree.walk(tmp_path, depth=1, quiet=False)
  assert ("undocumented.py", "(no description)") in rows


def test_walk_respects_depth(tmp_path):
  (tmp_path / "a.py").write_text('"""Level 0."""\n')
  sub = tmp_path / "sub"
  sub.mkdir()
  (sub / "b.py").write_text('"""Level 1."""\n')
  subsub = sub / "deep"
  subsub.mkdir()
  (subsub / "c.py").write_text('"""Level 2."""\n')
  rows = describe_tree.walk(tmp_path, depth=1, quiet=True)
  paths = [p for p, _ in rows]
  assert "a.py" in paths
  assert "sub/b.py" in paths
  # Level-2 file beyond depth=1 must NOT appear.
  assert "sub/deep/c.py" not in paths


def test_walk_prunes_skip_dirs(tmp_path):
  (tmp_path / "real.py").write_text('"""Real."""\n')
  nm = tmp_path / "node_modules"
  nm.mkdir()
  (nm / "junk.js").write_text("// stuff\n")
  rows = describe_tree.walk(tmp_path, depth=4, quiet=True)
  paths = [p for p, _ in rows]
  assert "real.py" in paths
  assert all("node_modules" not in p for p in paths)


def test_walk_handles_missing_directory(tmp_path, capsys):
  rows = describe_tree.walk(tmp_path / "does-not-exist", depth=1, quiet=True)
  assert rows == []
  err = capsys.readouterr().err
  assert "not found" in err
