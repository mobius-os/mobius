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

from app.sdk_emit import emit_unknown_enabled, unknown_event


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

  Tool-shaped items (commandExecution, fileChange, mcpToolCall,
  webSearch) translate into tool_start blocks. Reasoning items
  surface as `thinking` so users can see the model is working
  before a tool fires. agentMessage and userMessage start no
  block — agentMessage text streams via the delta channel and
  userMessage is just echo. Anything else returns an
  ``unknown_sdk_event`` (gated by MOBIUS_EMIT_UNKNOWN) so a new
  Codex item type lands in chat.log instead of disappearing.
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
  if itype == "reasoning":
    # Codex sometimes emits the reasoning text in `textDelta`, sometimes
    # in `text`, depending on app-server version. Take whichever has
    # content; an empty reasoning header is a no-op so we don't pollute
    # the wire with empty `thinking` events.
    text = (
      item.get("textDelta")
      or item.get("text")
      or item.get("content")
      or ""
    )
    if not text:
      return []
    return [{"type": "thinking", "content": str(text)}]
  if itype in ("agentMessage", "userMessage"):
    # agentMessage text arrives via item/agentMessage/delta — don't
    # double-emit. userMessage is just an echo of what we sent.
    return []
  return _unknown_events(f"item/started:{itype}", item)


def _unknown_events(kind: str, raw: dict) -> list[dict]:
  """Wraps `unknown_event` in the list shape translate_* callers want."""
  if not emit_unknown_enabled():
    return []
  return [unknown_event(kind, raw)]


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
  if itype == "reasoning":
    # The thinking text already streamed via item/started; the completed
    # message just closes the reasoning span. No new content to surface.
    return []
  if itype == "userMessage":
    # Pure echo of user input.
    return []
  return _unknown_events(f"item/completed:{itype}", item)


def translate_notification(msg: dict) -> list[dict]:
  """Translates one JSON-RPC notification into Möbius events.

  Recognized notifications return their named event shape (text,
  tool_*, session_init, usage, rate_limit, warning, compaction_event,
  model_rerouted, done, error). Anything else returns an
  `unknown_sdk_event` so future Codex protocol additions surface
  on the wire instead of disappearing. ``MOBIUS_EMIT_UNKNOWN=0``
  suppresses the unknown emission (DEBUG-logged either way).
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

  if method in (
    "thread/status/changed",
    "turn/started",
  ):
    # Status flips between active / idle. Idle after turn/completed
    # is the real end-of-turn marker (turn/completed already covers
    # it). turn/started is also redundant with the chat-side state
    # machine. Both are intentionally suppressed, not unknown.
    return []

  if method in ("thread/tokenUsage/updated", "tokenUsage"):
    usage = params.get("usage") or params
    return [{
      "type": "usage",
      "input_tokens": usage.get("input_tokens") or usage.get("inputTokens"),
      "output_tokens": usage.get("output_tokens") or usage.get("outputTokens"),
      "cache_creation_input_tokens": (
        usage.get("cache_creation_input_tokens")
        or usage.get("cachedInputTokens")
      ),
      "cache_read_input_tokens": (
        usage.get("cache_read_input_tokens")
        or usage.get("cachedReadTokens")
      ),
      "raw": dict(usage) if isinstance(usage, dict) else {},
    }]

  if method in (
    "account/rateLimits/updated",
    "rateLimits",
  ):
    info = params.get("rateLimits") or params
    return [{
      "type": "rate_limit",
      "status": info.get("status") if isinstance(info, dict) else None,
      "resets_at": info.get("resets_at") if isinstance(info, dict) else None,
      "raw": dict(info) if isinstance(info, dict) else {},
    }]

  if method in (
    "configWarning",
    "deprecationNotice",
    "guardianWarning",
  ):
    message = (
      params.get("message")
      or params.get("text")
      or params.get("notice")
      or ""
    )
    return [{
      "type": "warning",
      "source": method,
      "message": str(message),
    }]

  if method == "mcpServer/startupStatus/updated":
    status = params.get("status") or ""
    error = params.get("error")
    if not error and str(status).lower() not in ("error", "failed"):
      # Healthy startup transitions are noise; only surface failures.
      return []
    return [{
      "type": "warning",
      "source": method,
      "message": str(error or f"mcp server status: {status}"),
    }]

  if method == "thread/compacted":
    return [{
      "type": "compaction_event",
      "turn": params.get("turn") or params.get("turnNumber"),
    }]

  if method == "model/rerouted":
    return [{
      "type": "model_rerouted",
      "reason": str(params.get("reason") or params.get("message") or ""),
    }]

  return _unknown_events(str(method or "unknown"), msg)
