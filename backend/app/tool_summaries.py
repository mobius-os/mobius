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
  if not isinstance(inp, dict):
    return str(inp)[:200] if inp else ""
  if tool == "Bash":
    return inp.get("command", "")
  if tool == "shell":
    return inp.get("cmd", "") or inp.get("command", "")
  if tool in ("Read", "Glob"):
    return inp.get("file_path", "") or inp.get("pattern", "")
  if tool in ("Write", "Edit"):
    return inp.get("file_path", "")
  if tool == "apply_patch":
    patch = inp.get("patch", "")
    if isinstance(patch, str) and patch:
      first_line = patch.splitlines()[0] if patch.splitlines() else patch
      return first_line[:200]
    return ""
  if tool == "Grep":
    return inp.get("pattern", "")
  if tool == "WebSearch":
    return inp.get("query", "")
  if tool == "WebFetch":
    url = inp.get("url", "")
    return url[:80] + ("..." if len(url) > 80 else "")
  if tool == "TodoWrite":
    todos = inp.get("todos", [])
    if isinstance(todos, list) and todos:
      first = todos[0]
      if isinstance(first, dict):
        content = first.get("content", "")
        if content:
          return str(content)
      return f"{len(todos)} todo(s)"
    return "0 todo(s)"
  if tool in ("AskUserQuestion", "request_user_input"):
    questions = inp.get("questions", [])
    if isinstance(questions, list) and questions:
      first = questions[0]
      if isinstance(first, dict):
        return (
          first.get("question", "")
          or first.get("text", "")
          or first.get("header", "")
        )
    return ""
  if tool == "update_plan":
    explanation = inp.get("explanation", "")
    if explanation:
      return str(explanation)
    plan = inp.get("plan", [])
    if isinstance(plan, list):
      return f"{len(plan)} step(s)"
    return ""
  if inp:
    return ", ".join(
      f"{k}={str(v)[:40]}" for k, v in inp.items()
    )[:200]
  return ""
