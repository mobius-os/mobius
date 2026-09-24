#!/usr/bin/env python3
"""Native SessionStart context for Claude, Codex and Codex-backed model apps.

Provider command hook, not a model-selected tool. Uses inherited run-bound
credentials; never includes secrets in hook settings or command arguments.
"""
import json
import sys

from mobius_control_mcp import _agent_api_call


def hook_output() -> dict:
  try:
    result = _agent_api_call("GET", "/api/chat/continuity/context", timeout=5)
    context = result["context"]
    if not isinstance(context, str) or not context:
      raise ValueError("missing continuity context")
  except Exception:
    # An unavailable store is NOT an empty chat. Keep this visible to both
    # provider hook diagnostics and the model; no hidden semantic repair.
    print("Möbius could not load saved chat continuity.", file=sys.stderr)
    context = (
      "Saved chat continuity is UNAVAILABLE, not empty. Do not assume there "
      "are no saved notes. Retrieve saved history before relying on or "
      "replacing the handoff; report the failure if retrieval remains unavailable."
    )
  return {"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": context,
  }}


if __name__ == "__main__":
  json.load(sys.stdin)
  print(json.dumps(hook_output(), ensure_ascii=False))
