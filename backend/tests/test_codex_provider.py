"""Tests for the surviving `codex_appserver` helper.

The Codex chat path runs through the Agent SDK (`codex_sdk_runner`);
the legacy `codex app-server` subprocess path and its JSON-RPC
translator were removed. The one helper the SDK runner still reuses
from `codex_appserver` is `_extract_bash_command`, covered here.
"""

from app.codex_appserver import _extract_bash_command


def test_extract_bash_command_strips_wrapper():
  assert _extract_bash_command("/bin/bash -lc 'ls /tmp'") == "ls /tmp"


def test_extract_bash_command_passes_through_unwrapped():
  assert _extract_bash_command("ls /tmp") == "ls /tmp"


def test_extract_bash_command_requires_both_delimiters():
  # A prefix without the trailing quote is not the wrapper shape.
  raw = "/bin/bash -lc 'ls /tmp"
  assert _extract_bash_command(raw) == raw
