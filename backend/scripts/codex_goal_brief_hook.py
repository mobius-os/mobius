"""Give Codex the current Goal brief right after it compacts a live turn.

Codex runs this SessionStart hook (source "compact") before the request that
continues the compacted turn and adds its additionalContext there. The
run-bound backend returns the brief only to the run owning that exact
thread's live turn, at most once per compaction. A top-level turn's process
environment names its run; a shared helper host's environment does not, so
the host resolves the thread to that helper turn's own private env file.
Codex runs it only for threads whose turn has a Goal brief to restore.
Failure adds nothing: the next turn's brief and read_goal cover it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.helper_hosts import THREAD_ENV_LINKS_ENV, load_env_file, thread_env_link
from mobius_control_mcp import _agent_api_call


def _load_turn_identity(thread_id: str) -> None:
  """In a helper host, only the thread's own turn file names the run."""
  links = os.environ.get(THREAD_ENV_LINKS_ENV)
  if links is None:
    return
  for name in ("CHAT_ID", "AGENT_TOKEN"):
    os.environ.pop(name, None)
  link = thread_env_link(links, thread_id)
  if link is not None:
    os.environ.update(load_env_file(str(link)))


def compaction_context(payload: dict) -> str:
  if payload.get("hook_event_name") != "SessionStart" or payload.get("source") != "compact":
    return ""
  thread_id = str(payload.get("session_id") or "")
  _load_turn_identity(thread_id)
  chat_id = os.environ.get("CHAT_ID")
  if not chat_id or not thread_id:
    return ""
  result = _agent_api_call(
    "POST", f"/api/chats/{chat_id}/goal-brief/compaction", {"thread_id": thread_id},
  )
  context = result.get("context")
  return context if isinstance(context, str) else ""


def main() -> None:
  output = {}
  try:
    context = compaction_context(json.load(sys.stdin))
    if context:
      output = {"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": context,
      }}
  except Exception as exc:
    print(f"Goal brief hook failed: {exc}", file=sys.stderr)
  print(json.dumps(output))


if __name__ == "__main__":
  main()
