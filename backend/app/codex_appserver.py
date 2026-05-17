"""Translator for the Codex `app-server` JSON-RPC protocol.

The Codex app-server speaks JSON-RPC 2.0 over stdio (one message per
line).  Streaming text arrives as `item/agentMessage/delta` notifications;
tool work arrives as `item/started` + `item/completed` pairs.  The other
Codex entry point (`codex exec --json`) does NOT emit deltas — it only
sends the final `agent_message` once the turn is done.  This module
exists so the agent can render text as it streams instead of appearing
in one big block at the end.

The translator is a pure function: it takes a parsed JSON-RPC
notification dict and returns a list of Möbius-shaped event dicts (the
same shape ClaudeProvider emits via parse_line).  The actual I/O
(spawning app-server, sending initialize/thread/start/turn/start,
reading stdout) lives in `scripts/codex_appserver_runner.py` so this
module stays trivially unit-testable.
"""

from __future__ import annotations

import json
from typing import Any


def _extract_bash_command(raw: str) -> str:
  """Strips Codex's /bin/bash -lc wrapper from a command string."""
  prefix = "/bin/bash -lc '"
  if raw.startswith(prefix) and raw.endswith("'"):
    return raw[len(prefix):-1]
  return raw


def _summarize_args(value: Any) -> str:
  """Best-effort string rendering for MCP tool args / outputs."""
  if value is None or value == "":
    return ""
  if isinstance(value, (dict, list)):
    try:
      return json.dumps(value, indent=2)
    except (TypeError, ValueError):
      return str(value)
  return str(value)


# App-server emits camelCase item types. Legacy `codex exec --json`
# (and possibly future protocol versions) emit snake_case. Normalize
# so the dispatch tables only need one branch per logical type.
_ITEM_TYPE_ALIASES = {
  "command_execution": "commandExecution",
  "file_change": "fileChange",
  "mcp_tool_call": "mcpToolCall",
  "web_search": "webSearch",
  "agent_message": "agentMessage",
  "user_message": "userMessage",
}


def _canonical_item_type(itype: str | None) -> str | None:
  """Return the canonical (camelCase) type, mapping legacy aliases."""
  if not itype:
    return None
  return _ITEM_TYPE_ALIASES.get(itype, itype)


def translate_item_started(item: dict) -> list[dict]:
  """Returns Möbius events for an item/started notification.

  Returns [] for items that don't map to a Möbius tool block
  (reasoning, agentMessage, userMessage — the latter is just echo).
  """
  itype = _canonical_item_type(item.get("type"))
  if itype == "commandExecution":
    cmd = item.get("command", "")
    return [{
      "type": "tool_start",
      "tool": "Bash",
      "input": _extract_bash_command(cmd),
    }]
  if itype == "fileChange":
    changes = item.get("changes", [])
    path = changes[0].get("path", "") if changes else ""
    return [{"type": "tool_start", "tool": "Edit", "input": path}]
  if itype == "mcpToolCall":
    server = item.get("server", "")
    tool = item.get("tool", "")
    return [{
      "type": "tool_start",
      "tool": f"{server}:{tool}" if server else (tool or "mcp"),
      "input": _summarize_args(
        item.get("arguments") or item.get("input")
      ),
    }]
  if itype == "webSearch":
    return [{
      "type": "tool_start",
      "tool": "WebSearch",
      "input": item.get("query", ""),
    }]
  # reasoning / agentMessage / userMessage — no tool block.
  return []


def translate_item_completed(item: dict) -> list[dict]:
  """Returns Möbius events for an item/completed notification.

  agentMessage items are SUPPRESSED at this layer because the
  agentMessage/delta notifications already streamed the text — emitting
  it again from item/completed would double the content.  The runner
  is responsible for not re-emitting deltas it already sent.
  """
  itype = _canonical_item_type(item.get("type"))
  if itype == "agentMessage":
    # Deltas already streamed the text; don't duplicate.
    return []
  if itype == "commandExecution":
    output = (item.get("aggregated_output") or "").strip()
    events: list[dict] = []
    if output:
      events.append({"type": "tool_output", "content": output})
    events.append({"type": "tool_end"})
    return events
  if itype == "fileChange":
    changes = item.get("changes", [])
    lines = [
      f"{c.get('kind', '?')} {c.get('path', '')}".strip()
      for c in changes
    ]
    summary = "\n".join(line for line in lines if line)
    events: list[dict] = []
    if summary:
      events.append({"type": "tool_output", "content": summary})
    events.append({"type": "tool_end"})
    return events
  if itype == "mcpToolCall":
    args = item.get("arguments") or item.get("input")
    result = item.get("result") or item.get("output")
    events: list[dict] = []
    args_str = _summarize_args(args)
    if args_str:
      events.append({"type": "tool_input", "input": args_str})
    result_str = _summarize_args(result)
    if result_str:
      events.append({"type": "tool_output", "content": result_str})
    events.append({"type": "tool_end"})
    return events
  if itype == "webSearch":
    query = item.get("query", "")
    if not query:
      action = item.get("action") or {}
      queries = action.get("queries") or []
      if queries:
        query = "\n".join(str(q) for q in queries)
    events: list[dict] = []
    if query:
      events.append({"type": "tool_input", "input": query})
    events.append({"type": "tool_end"})
    return events
  # reasoning / userMessage — no Möbius event.
  return []


def translate_notification(msg: dict) -> list[dict]:
  """Translates one JSON-RPC notification into Möbius events.

  Returns [] for notifications that don't map to a user-visible event
  (status updates, rate-limit updates, mcp startup status, etc.).
  The runner calls this for every notification it reads from stdout.
  """
  method = msg.get("method")
  params = msg.get("params", {}) or {}

  if method == "thread/started":
    tid = (params.get("thread") or {}).get("id")
    if tid:
      return [{"type": "session_init", "session_id": tid}]
    return []

  if method == "item/agentMessage/delta":
    delta = params.get("delta", "")
    if delta:
      return [{"type": "text", "content": delta}]
    return []

  if method == "item/started":
    item = params.get("item") or {}
    return translate_item_started(item)

  if method == "item/completed":
    item = params.get("item") or {}
    return translate_item_completed(item)

  if method == "turn/completed":
    # Pull cost / token info if present so the chat can record it.
    turn = params.get("turn") or {}
    usage = turn.get("usage") or params.get("usage") or {}
    return [{
      "type": "done",
      "cost_usd": 0,  # app-server doesn't compute cost; leave 0
      "_usage": usage,  # underscore key — internal hint, ignored
    }]

  if method == "error":
    msg_text = params.get("message") or params.get("error") or "Codex error"
    return [{"type": "error", "message": str(msg_text)}]

  if method == "thread/status/changed":
    # Status flips between active / idle.  Idle after turn/completed
    # is the real end-of-turn marker, but turn/completed already gives
    # us that — so we ignore status changes.
    return []

  # Everything else (tokenUsage, rateLimits, configWarning, mcpServer
  # startup, etc.) is informational — drop it.
  return []
