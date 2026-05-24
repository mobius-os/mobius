"""Shared tool-input summarizer used by SSE event builders.

Both `providers.py` (subprocess path) and `claude_sdk_runner.py` (SDK
path) emit `tool_input` events whose `input` field is a short human-
readable summary of the tool call's arguments. Keeping the summary
logic in one place means a future tool addition or format tweak only
needs to land here — both runners pick it up automatically.
"""

from typing import Any


def summarize_tool_input(tool: str, inp: dict[str, Any]) -> str:
  """Returns a short human-readable summary of a tool's input."""
  if tool == "Bash":
    return inp.get("command", "")
  if tool in ("Read", "Glob"):
    return inp.get("file_path", "") or inp.get("pattern", "")
  if tool in ("Write", "Edit"):
    return inp.get("file_path", "")
  if tool == "Grep":
    return inp.get("pattern", "")
  return str(inp)[:200] if inp else ""
