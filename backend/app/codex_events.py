"""Codex app-server notifications translated into stable Möbius events.

The runner owns SDK acquisition, process/control lifecycle, and retry decisions.
This module owns typed-notification interpretation and public/private event
shapes so protocol churn can be reviewed and tested without the turn machinery.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from app.codex_appserver import _extract_bash_command
from app.json_safety import json_safe
from app.tool_edit_preview import codex_edit_preview
from app.tool_sources import normalize_tool_sources

log = logging.getLogger("moebius.chat")

def _thinking_event(content: str, segment_id: str | None = None) -> dict:
  """Build a reasoning delta, preserving its provider semantic segment.

  Deltas within one segment are token fragments and concatenate verbatim.
  Distinct summary/content indices are separate thoughts and need a paragraph
  boundary. Keeping that identity on the wire lets both live and durable
  reducers make the distinction without guessing from Markdown text.
  """
  event = {
    "type": "thinking",
    "content": content,
    "ts": int(time.time() * 1000),
  }
  if segment_id:
    event["segment_id"] = segment_id
  return event


def _codex_thinking_segment_id(payload: Any) -> str | None:
  item_id = getattr(payload, "item_id", None)
  if not item_id:
    return None
  summary_index = getattr(payload, "summary_index", None)
  if summary_index is not None:
    return f"codex:{item_id}:summary:{summary_index}"
  content_index = getattr(payload, "content_index", None)
  if content_index is not None:
    return f"codex:{item_id}:content:{content_index}"
  return f"codex:{item_id}"


def _extract_rate_limit_reset(snapshot) -> tuple[int | None, bool]:
  """Pull a park-worthy reset epoch + reached flag from a RateLimitSnapshot.

  The Codex analog of what the Claude runner reads off RateLimitEvent. Picks the
  most-constrained window's ``resets_at`` (the binding limit is the one closest
  to full, so its reset is the one worth waiting for) and reports whether any
  window/credit pool actually hit its cap. ``rate_limit_reached_type`` has no
  "ok" member, so a non-None value reliably means a real limit hit — a
  structured signal trustworthy without string-matching the error text.

  Returns ``(resets_at_epoch_or_None, reached_bool)``. Defensive against partial
  or older SDK payloads: any missing field degrades to skip / (None, False).
  """
  if snapshot is None:
    return None, False
  reached = getattr(snapshot, "rate_limit_reached_type", None) is not None
  best_reset: int | None = None
  best_used = -1.0
  for window in (
    getattr(snapshot, "primary", None),
    getattr(snapshot, "secondary", None),
  ):
    if window is None:
      continue
    resets_at = getattr(window, "resets_at", None)
    if resets_at is None:
      continue
    try:
      used = float(getattr(window, "used_percent", 0) or 0)
    except (TypeError, ValueError):
      used = 0.0
    if used > best_used:
      best_used = used
      best_reset = resets_at
  return best_reset, reached


def _model_dump(value: Any) -> Any:
  """Turns provider SDK objects into plain JSON-safe values."""
  return json_safe(value)


def _format_json(value: Any) -> str:
  """Returns a stable user-facing string for tool inputs and outputs."""
  dumped = _model_dump(value)
  if dumped is None or dumped == "":
    return ""
  if isinstance(dumped, str):
    return dumped
  try:
    return json.dumps(dumped, ensure_ascii=True, indent=2)
  except (TypeError, ValueError):
    return str(dumped)


def _reasoning_summary_setting(sdk: dict[str, Any]) -> Any | None:
  """Ask Codex for the richest public reasoning summary it supports."""
  summary_cls = sdk.get("ReasoningSummary")
  if summary_cls is None:
    return None
  try:
    return summary_cls("auto")
  except Exception:
    try:
      return summary_cls(root="auto")
    except Exception:
      log.warning("Codex: could not construct ReasoningSummary", exc_info=True)
      return None


def _web_search_sources(item: Any) -> list[dict[str, str]]:
  """Extract source URLs exposed by a Codex web-search item, if any.

  The pinned SDK's public item model carries the URL on ``openPage`` and
  ``findInPage`` actions, but not on a plain search action. Keep the optional
  result-field scan for forward compatibility with SDKs that expose the app
  server's result metadata directly.
  """
  collected: list[dict[str, str]] = []
  seen: set[str] = set()

  def add(raw: Any) -> None:
    for source in normalize_tool_sources(raw):
      url = source.get("url")
      if not url or url in seen:
        continue
      collected.append(source)
      seen.add(url)

  for attr in ("results", "sources", "content", "output"):
    add(getattr(item, attr, None))

  action = getattr(item, "action", None)
  action_root = getattr(action, "root", action)
  add(action_root)
  return collected


def _stamp_tool_use_id(event: dict[str, Any], item: Any) -> None:
  """Stamp the ThreadItem's stable id onto a tool event as `tool_use_id`
  (contract rule 6), so a large tool output can be reduced on the wire and
  fetched lazily by id.

  Verified stable: every Codex ThreadItem carries an `id`, the SAME id rides
  the `ItemStarted` (tool_start) and `ItemCompleted` (tool_output/tool_end)
  notifications for one tool call, and the streaming output-delta / file-change
  notifications reference it as `itemId` — so it is stable emit->read and unique
  within the chat, which is all the stash key needs. A test fake without an
  `id` (or a null id) is left unstamped, so the event shape is unchanged."""
  tid = getattr(item, "id", None)
  if tid:
    event["tool_use_id"] = tid


def _stamp_notification_item_id(event: dict[str, Any], payload: Any) -> None:
  """Stamp `tool_use_id` from a notification's `item_id` (the streaming
  output-delta / file-change-patch notifications reference their ThreadItem by
  `itemId`, the same id the completed item carries). getattr-guarded so SDK
  shape drift or a test fake without the field degrades to an untagged event
  (which the sink leaves inline) rather than raising."""
  item_id = getattr(payload, "item_id", None) or getattr(payload, "itemId", None)
  if item_id:
    event["tool_use_id"] = item_id


# A collab tool input is a short human label. VERIFIED on codex 0.144.5: a
# delegation turn streams the collab tool ONLY as the `wait` op, which carries
# no prompt, so the label is the partner-language "Working in the background"
# rather than a wire op string. A future SDK that surfaces the spawn op WITH a
# prompt would render "<op>: <prompt>". The summary joins the sub-agents'
# last-known messages (dead at runtime — see _collab_summary); both are bounded
# so an oversized prompt or a chatty fleet cannot bloat the wire event.
_COLLAB_DESCRIPTION_MAX = 120
_COLLAB_SUMMARY_MAX = 500


def _collab_op(item: Any) -> str:
  """The collab tool operation as its wire string (spawnAgent, sendInput, …).

  ``item.tool`` is a ``CollabAgentTool`` enum on a real SDK item; a test fake may
  pass the raw string. Read ``.value`` when present and fall back to ``str`` so
  the caller never has to import the enum just to branch on the operation.
  """
  tool = getattr(item, "tool", None)
  value = getattr(tool, "value", None)
  if value:
    return value
  return str(tool) if tool is not None else "collab"


def _collab_description(item: Any) -> str:
  """Build the partner-language input for an ordinary collab tool activity.

  VERIFIED on codex 0.144.5: the only collab item that streams on the parent
  turn is the `wait` op, which carries no prompt. With no prompt there is
  nothing task-specific to name, so return a generic owner-facing label rather
  than leaking the wire op string ("wait:") into the activity. When a prompt IS
  present (a future SDK that surfaces the spawn op, or a test fake) keep the
  "<op>: <prompt>" form so the chip names the delegated work. Bounded either way.
  """
  prompt = (getattr(item, "prompt", None) or "").strip()
  if not prompt:
    return "Working in the background"
  op = _collab_op(item)
  return f"{op}: {prompt}"[:_COLLAB_DESCRIPTION_MAX]


def _collab_summary(item: Any) -> str | None:
  """Join the sub-agents' last-known status messages into one summary line.

  DEAD AT RUNTIME on codex 0.144.5: the only collab item that reaches the parent
  stream is the `wait` op, whose ``agents_states`` is always EMPTY, so this
  returns None every time in production. Kept defensively for a future SDK that
  populates ``agents_states`` on the parent stream. Today the named child and
  its result are surfaced HISTORICALLY by the Workflows app parser (via the
  child rollout's parent_thread_id), NOT by this summary — so nobody should read
  the live Codex chip as carrying the child's answer.

  ``agents_states`` maps a child thread id to a ``CollabAgentState`` whose
  ``message`` is the agent's latest note (often None while running). Skip the
  empties and return None when nothing is available, so a still-silent fleet
  produces no summary rather than an empty string.
  """
  states = getattr(item, "agents_states", None) or {}
  messages = []
  for state in states.values():
    message = (getattr(state, "message", None) or "").strip()
    if message:
      messages.append(message)
  if not messages:
    return None
  return "; ".join(messages)[:_COLLAB_SUMMARY_MAX]


def _subagent_lifecycle_event(
  item: Any, sdk: dict[str, Any], *, provider_session_id: str | None,
  occurred_at: Any = None, provider_activation_id: str | None = None,
) -> dict[str, Any] | None:
  """Normalize a Codex subAgentActivity marker without inventing completion.

  The native marker currently exposes started/interacted/interrupted.  Started
  opens a helper lane; interrupted closes it as stopped. Interacted is progress,
  not a lifecycle boundary, and remains intentionally silent.
  """
  cls = sdk.get("SubAgentActivityThreadItem")
  if cls is None or not isinstance(item, cls):
    return None
  kind = getattr(getattr(item, "kind", None), "value", None)
  kind = kind or str(getattr(item, "kind", ""))
  if kind == "started":
    event_type, state = "agent_started", "running"
  elif kind == "interrupted":
    event_type, state = "agent_terminal", "stopped"
  else:
    return None
  return {
    "type": "agent_lifecycle",
    "provider": "codex",
    "provider_session_id": provider_session_id,
    "provider_agent_id": getattr(item, "agent_thread_id", None),
    "provider_activation_id": provider_activation_id,
    "parent_kind": "unknown",
    "event_type": event_type,
    "state": state,
    # agent_path is identity/role metadata, not an outcome summary. Keeping
    # non-terminal Codex summaries empty gives the durable table a structural
    # guarantee that delegated prompt/preview prose cannot enter through a
    # start fact.
    "agent_type": getattr(item, "agent_path", None),
    "occurred_at": occurred_at,
    "source": "runner",
    "source_event_id": getattr(item, "id", None),
  }


def _thread_started_lifecycle_event(
  payload: Any, *, root_thread_id: str | None,
  parent_provider_activation_id: str | None = None,
) -> dict[str, Any] | None:
  """Build the exact spawn/parent fact carried by ThreadStartedNotification."""
  thread = getattr(payload, "thread", None)
  thread_id = getattr(thread, "id", None)
  if not thread_id:
    return None
  parent_id = getattr(thread, "parent_thread_id", None)
  # A top-level thread started notification is not a spawned helper.
  if not parent_id or parent_id == thread_id:
    return None
  role = getattr(thread, "agent_role", None)
  nickname = getattr(thread, "agent_nickname", None)
  return {
    "type": "agent_lifecycle",
    "provider": "codex",
    "provider_session_id": root_thread_id,
    "provider_agent_id": thread_id,
    "provider_activation_id": f"thread-started:{thread_id}",
    "parent_provider_agent_id": parent_id,
    "parent_provider_activation_id": (
      parent_provider_activation_id or f"thread-started:{parent_id}"
    ),
    "parent_kind": "main" if parent_id == root_thread_id else "agent",
    "event_type": "agent_spawned",
    "state": "running",
    # ``thread.preview`` derives from the delegated prompt. Role/nickname is
    # sufficient lifecycle metadata; never copy the preview into persistence.
    "agent_type": role or nickname,
    "occurred_at": getattr(thread, "created_at", None),
    "source": "runner",
    "source_event_id": f"thread-started:{thread_id}",
  }


def _record_private_lifecycle(bc: Any, event: dict[str, Any] | None) -> None:
  if event is None:
    return
  recorder = getattr(bc, "record_lifecycle", None)
  if callable(recorder):
    recorder(event)


def _public_task_event(
  lifecycle: dict[str, Any] | None,
  *,
  tool_use_id: str,
) -> dict[str, Any] | None:
  """Translate one provider-neutral lifecycle fact into the shared chip wire.

  Durable Workflows attribution keeps the richer ``agent_lifecycle`` record.
  The transcript needs only the same task_start/task_done contract Claude
  already uses. An activation id, rather than a child thread id, is the public
  task identity because Codex can resume one helper multiple times in a turn.
  """
  if not lifecycle:
    return None
  task_id = (
    lifecycle.get("provider_activation_id")
    or lifecycle.get("provider_agent_id")
  )
  if not task_id:
    return None
  event_type = lifecycle.get("event_type")
  if event_type in ("agent_spawned", "agent_started"):
    agent_type = str(lifecycle.get("agent_type") or "").strip()
    return {
      "type": "task_start",
      "task_id": str(task_id),
      "description": (
        agent_type[:_COLLAB_DESCRIPTION_MAX] or "Background helper"
      ),
      "task_type": agent_type or None,
      "tool_use_id": tool_use_id,
    }
  if event_type == "agent_terminal":
    summary = lifecycle.get("summary")
    return {
      "type": "task_done",
      "task_id": str(task_id),
      "status": lifecycle.get("state") or "done",
      "summary": (
        str(summary)[:_COLLAB_SUMMARY_MAX] if summary is not None else None
      ),
      "tool_use_id": tool_use_id,
    }
  return None


def _collab_reactivation_events(
  item: Any, sdk: dict[str, Any], *, root_thread_id: str | None,
  occurred_at: Any, active: dict[str, str], known: set[str],
  activation_by_call_child: dict[tuple[str, str], str],
  last_activation_by_child: dict[str, str],
) -> list[dict[str, Any]]:
  cls = sdk.get("CollabAgentToolCallThreadItem")
  if cls is None or not isinstance(item, cls):
    return []
  operation = _collab_op(item)
  receivers = [str(value) for value in (
    getattr(item, "receiver_thread_ids", None) or []) if value]
  sender = getattr(item, "sender_thread_id", None)
  call_id = str(getattr(item, "id", None) or operation)
  if operation == "spawnAgent":
    known.update(receivers)
    for child_id in receivers:
      activation = active.setdefault(child_id, f"thread-started:{child_id}")
      activation_by_call_child[(call_id, child_id)] = activation
      last_activation_by_child[child_id] = activation
    return []  # ThreadStarted carries the exact spawn + ancestry fact.
  if operation not in ("sendInput", "resumeAgent"):
    return []
  events = []
  for child_id in receivers:
    known.add(child_id)
    activation = active.get(child_id)
    if activation is not None:
      # Input sent to an already-running child is progress in the current
      # activation, not proof of a new lane. Keep the call association so its
      # exact completion can still enrich that activation later.
      activation_by_call_child[(call_id, child_id)] = activation
      continue
    activation = f"{call_id}:{child_id}"
    active[child_id] = activation
    activation_by_call_child[(call_id, child_id)] = activation
    last_activation_by_child[child_id] = activation
    parent_kind = "main" if sender and sender == root_thread_id else (
      "agent" if sender else "unknown")
    events.append({
      "type": "agent_lifecycle", "provider": "codex",
      "provider_session_id": root_thread_id,
      "provider_agent_id": child_id,
      "provider_activation_id": activation,
      "parent_provider_agent_id": sender,
      "parent_provider_activation_id": active.get(str(sender)) if sender else None,
      "parent_kind": parent_kind,
      "event_type": "agent_started", "state": "running",
      # ``item.prompt`` is the delegated prompt body, not a lifecycle summary.
      # It must not enter the durable observability table; the terminal agent
      # state may later contribute a provider-authored result summary.
      "occurred_at": occurred_at,
      "source": "runner", "source_event_id": f"{call_id}:{child_id}:started",
    })
  return events


def _collab_completion_events(
  item: Any, sdk: dict[str, Any], *, root_thread_id: str | None,
  occurred_at: Any, active: dict[str, str], known: set[str],
  activation_by_call_child: dict[tuple[str, str], str],
  last_activation_by_child: dict[str, str],
) -> list[dict[str, Any]]:
  cls = sdk.get("CollabAgentToolCallThreadItem")
  if cls is None or not isinstance(item, cls):
    return []
  terminal = {
    "completed": "done", "errored": "failed", "interrupted": "stopped",
    "shutdown": "stopped",
  }
  events = []
  call_id = str(getattr(item, "id", None) or _collab_op(item))
  for child_id, agent_state in (getattr(item, "agents_states", None) or {}).items():
    child_id = str(child_id)
    raw_status = getattr(agent_state, "status", None)
    status = getattr(raw_status, "value", None) or str(raw_status or "")
    state = terminal.get(status)
    activation = activation_by_call_child.get((call_id, child_id))
    if activation is None:
      activation = active.get(child_id) or last_activation_by_child.get(child_id)
    if state is None or activation is None:
      continue
    known.add(child_id)
    events.append({
      "type": "agent_lifecycle", "provider": "codex",
      "provider_session_id": root_thread_id,
      "provider_agent_id": child_id,
      "provider_activation_id": activation,
      "parent_kind": "unknown", "event_type": "agent_terminal",
      "state": state, "summary": getattr(agent_state, "message", None),
      "occurred_at": occurred_at, "source": "runner",
      "source_event_id": f"{call_id}:{child_id}:{status}",
    })
    last_activation_by_child[child_id] = activation
    if active.get(child_id) == activation:
      active.pop(child_id, None)
  return events


def _thread_status_lifecycle_event(
  payload: Any, *, root_thread_id: str | None, active: dict[str, str],
  known: set[str], activation_counts: dict[str, int],
  last_activation_by_child: dict[str, str],
) -> dict[str, Any] | None:
  thread_id = str(getattr(payload, "thread_id", None) or "")
  if not thread_id or thread_id == root_thread_id or thread_id not in known:
    return None
  status_union = getattr(payload, "status", None)
  status_root = getattr(status_union, "root", status_union)
  status_type = str(getattr(status_root, "type", ""))
  if status_type == "active":
    if thread_id in active:
      return None
    activation_counts[thread_id] = activation_counts.get(thread_id, 0) + 1
    activation = f"status:{thread_id}:{activation_counts[thread_id]}"
    active[thread_id] = activation
    last_activation_by_child[thread_id] = activation
    event_type, state = "agent_started", "running"
  elif status_type in ("idle", "systemError"):
    activation = active.pop(thread_id, None)
    if activation is None:
      return None
    last_activation_by_child[thread_id] = activation
    event_type = "agent_terminal"
    state = "done" if status_type == "idle" else "failed"
  else:
    return None
  return {
    "type": "agent_lifecycle", "provider": "codex",
    "provider_session_id": root_thread_id,
    "provider_agent_id": thread_id,
    "provider_activation_id": activation,
    "parent_kind": "unknown", "event_type": event_type, "state": state,
    "source": "runner",
    "source_event_id": f"thread-status:{thread_id}:{activation}:{status_type}",
  }


async def _record_collab_child_links(
  item: Any, sdk: dict[str, Any], *, chat_id: str,
) -> None:
  """Attribute a spawned sub-agent's thread to this chat.

  DEAD AT RUNTIME on codex 0.144.5: the only collab item that reaches the parent
  stream is the `wait` op (never a `spawnAgent`), and its ``receiver_thread_ids``
  is always EMPTY, so the spawn gate below never fires and no link is recorded
  here in production. Kept defensively for a future SDK that surfaces the spawn
  op with populated ``receiver_thread_ids`` on the parent stream. Today the named
  child rollout is attributed to this chat by the Workflows app parser via the
  child's parent_thread_id, NOT by this recorder — so the live Codex chip is not
  a named child link.

  A spawn's ``receiver_thread_ids`` are the freshly-spawned child thread ids;
  recording each in the append-only session->chat map keeps the child's own
  rollout resolvable back to this chat even though it streams on its own thread.
  Gated on the spawn operation (that is when a NEW child id first appears; the
  other ops reference children already recorded at their spawn). Idempotent, and
  never raises: observability must not break the notification loop. The write
  goes through ``record_session_link_async`` (own session, worker thread) so it
  neither blocks the stream loop nor touches the runner's shared ``db``.
  """
  try:
    collab_cls = sdk.get("CollabAgentToolCallThreadItem")
    if collab_cls is None or not isinstance(item, collab_cls):
      return
    if _collab_op(item) != "spawnAgent":
      return
    from app.session_links import record_session_link_async
    for child_id in getattr(item, "receiver_thread_ids", None) or []:
      await record_session_link_async("codex", child_id, chat_id)
  except Exception:
    log.debug("codex collab child-link recording failed", exc_info=True)


def _tool_start_event(item: Any, sdk: dict[str, Any]) -> dict[str, Any] | None:
  """Builds one Möbius `tool_start` event from a typed item."""
  image_view_cls = sdk.get("ImageViewThreadItem")
  if image_view_cls is not None and isinstance(item, image_view_cls):
    return {
      "type": "tool_start",
      "tool": "ViewImage",
      "input": _format_json(getattr(item, "path", "")),
    }
  # Standalone dispatch keeps collab items as ordinary Task activity. The live
  # loop now groups all such items under one per-turn host and enriches it with
  # ThreadStarted/ThreadStatus task events, avoiding duplicate Task rows while
  # retaining this safe fallback for isolated callers and older SDKs.
  collab_cls = sdk.get("CollabAgentToolCallThreadItem")
  if collab_cls is not None and isinstance(item, collab_cls):
    return {
      "type": "tool_start",
      "tool": "Task",
      "input": _collab_description(item),
    }
  sub_activity_cls = sdk.get("SubAgentActivityThreadItem")
  if sub_activity_cls is not None and isinstance(item, sub_activity_cls):
    # subAgentActivity is Codex's sub-agent LIFECYCLE marker (agentPath,
    # agentThreadId, kind) in the parent thread's item stream/history. The
    # invariant: the sub-agent's actual tool work is surfaced elsewhere — live,
    # the parent streams the delegation as the CollabAgentToolCallThreadItem
    # `Task` events above; on resume, the parent's replayed history is never
    # re-rendered (the runner uses only thread.id + thread.turn()). So the
    # marker itself carries nothing Möbius opens as its own tool block. This is
    # a DELIBERATE no-op, classified explicitly rather than left to fall through
    # silently — surfacing sub-agent lifecycle as its own UI is a future UX
    # decision, not an accident of omission (see test_codex_sdk_contract).
    log.debug("codex subAgentActivity marker (no-op): kind=%s",
              getattr(item, "kind", None))
    return None
  if isinstance(item, sdk["CommandExecutionThreadItem"]):
    return {
      "type": "tool_start",
      "tool": "Bash",
      "input": _extract_bash_command(item.command),
    }
  if isinstance(item, sdk["FileChangeThreadItem"]):
    first = item.changes[0] if item.changes else None
    path = _model_dump(first).get("path", "") if first is not None else ""
    edit_preview = _file_change_edit_preview(item.changes)
    return {
      "type": "tool_start",
      "tool": "Edit",
      "input": path,
      **({"edit_preview": edit_preview} if edit_preview else {}),
    }
  if isinstance(item, sdk["McpToolCallThreadItem"]):
    tool_name = f"{item.server}:{item.tool}" if item.server else item.tool
    return {
      "type": "tool_start",
      "tool": tool_name or "mcp",
      "input": _format_json(item.arguments),
    }
  if isinstance(item, sdk["DynamicToolCallThreadItem"]):
    tool_name = item.tool
    if item.namespace:
      tool_name = f"{item.namespace}:{tool_name}"
    return {
      "type": "tool_start",
      "tool": tool_name or "tool",
      "input": _format_json(item.arguments),
    }
  if isinstance(item, sdk["WebSearchThreadItem"]):
    return {
      "type": "tool_start",
      "tool": "WebSearch",
      "input": item.query,
    }
  return None


def _tool_completed_events(item: Any, sdk: dict[str, Any]) -> list[dict[str, Any]]:
  """Builds Möbius tool-end events from a completed typed item."""
  image_view_cls = sdk.get("ImageViewThreadItem")
  if image_view_cls is not None and isinstance(item, image_view_cls):
    return [{"type": "tool_end"}]
  # Standalone dispatch closes the ordinary Task activity above. The live loop
  # owns its one per-turn host and bypasses this branch, publishing normalized
  # task_done events from lifecycle notifications before closing that host.
  collab_cls = sdk.get("CollabAgentToolCallThreadItem")
  if collab_cls is not None and isinstance(item, collab_cls):
    events: list[dict[str, Any]] = []
    summary = _collab_summary(item)
    if summary:
      events.append({"type": "tool_output", "content": summary})
    events.append({"type": "tool_end"})
    return events

  sub_activity_cls = sdk.get("SubAgentActivityThreadItem")
  if sub_activity_cls is not None and isinstance(item, sub_activity_cls):
    # Completion counterpart of the _tool_start_event no-op: the sub-agent
    # lifecycle marker opens no Möbius tool block, so it closes none. The live
    # delegation's open/close rides CollabAgentToolCallThreadItem (`Task`); this
    # marker is classified explicitly to keep the invariant visible rather than
    # silently returning [] by fall-through.
    return []

  if isinstance(item, sdk["CommandExecutionThreadItem"]):
    output = (item.aggregated_output or "").strip()
    exit_code = getattr(item, "exit_code", None)
    events: list[dict[str, Any]] = [{
      "type": "tool_output",
      "content": output,
      "output_complete": True,
      **({"output_exit_code": exit_code} if isinstance(exit_code, int) else {}),
    }]
    events.append({"type": "tool_end"})
    return events

  if isinstance(item, sdk["FileChangeThreadItem"]):
    events: list[dict[str, Any]] = []
    summary = _file_change_patch_summary(item.changes)
    if summary:
      events.append({"type": "tool_output", "content": summary})
    events.append({"type": "tool_end"})
    return events

  if isinstance(item, sdk["McpToolCallThreadItem"]):
    events: list[dict[str, Any]] = []
    result = _format_json(item.result)
    if result:
      events.append({"type": "tool_output", "content": result})
    events.append({"type": "tool_end"})
    return events

  if isinstance(item, sdk["DynamicToolCallThreadItem"]):
    events: list[dict[str, Any]] = []
    result = _format_json(item.content_items)
    if result:
      events.append({"type": "tool_output", "content": result})
    events.append({"type": "tool_end"})
    return events

  if isinstance(item, sdk["WebSearchThreadItem"]):
    events: list[dict[str, Any]] = []
    # The real query (or the opened page's URL) only lands on completion —
    # ItemStarted carried an empty one, so the row showed a bare "WebSearch".
    # Backfill it now; the caller stamps tool_use_id so it targets this exact
    # search rather than the first input-less block.
    query = getattr(item, "query", "")
    if query:
      events.append({"type": "tool_input", "input": query})
    sources = _web_search_sources(item)
    if sources:
      events.append({"type": "tool_sources", "sources": sources})
    events.append({"type": "tool_end"})
    return events

  if isinstance(item, sdk["AgentMessageThreadItem"]):
    # Materialize the authoritative full text of the completed assistant
    # message. Durable prose otherwise rides ONLY on the streamed
    # AgentMessageDeltaNotification deltas (published as "text" above); if those
    # were absent/dropped/coalesced (observed on oversized responses, e.g. a
    # "very long numbered" request that persisted NOTHING) the reply vanishes
    # silently. text_final REPLACES the accumulated text block, so it is
    # idempotent when the deltas already delivered identical prose (events.py
    # returns False), converts the lingering text_boundary into text when no
    # delta arrived, and recovers a truncated prefix. Guarded on non-empty text
    # so a genuinely-empty message stays silent.
    text = item.text or ""
    if text.strip():
      event = {"type": "text_final", "content": text}
      item_id = getattr(item, "id", None)
      if item_id:
        event["text_item_id"] = item_id
      return [event]
    return []

  return []


def _enum_wire_value(value: Any) -> str | None:
  """Returns the wire value for a generated enum, tolerating test doubles."""
  if value is None:
    return None
  raw = getattr(value, "value", value)
  return raw if isinstance(raw, str) else str(raw)


_CHATGPT_MODEL_UNAVAILABLE_RE = re.compile(
  r"[\"'](?P<model>[^\"']+)[\"'] model is not supported when using Codex "
  r"with a ChatGPT account",
  re.IGNORECASE,
)


def _codex_user_error(error_text: str | None) -> str | None:
  """Turns a known account/model rejection into a useful next action.

  The live Codex catalog can advertise a model that the signed-in account's
  plan or staged rollout cannot actually run. The upstream 400 is technically
  precise but reads like a broken connection and offers no recovery path.
  Preserve every unknown provider error verbatim; only this exact, observed
  contract gets product wording.
  """
  if not error_text:
    return error_text
  match = _CHATGPT_MODEL_UNAVAILABLE_RE.search(error_text)
  if match is None:
    return error_text
  model_id = match.group("model")
  return (
    f"{model_id} isn’t available for this ChatGPT account. Codex is "
    "connected, but this account’s current plan or model rollout does not "
    "include it. Choose another Codex model from this chat’s Model menu, "
    "then try again."
  )


def _agent_message_phase(item: Any, sdk: dict[str, Any]) -> str | None:
  """Returns a completed agent message's native SDK phase."""
  if not isinstance(item, sdk["AgentMessageThreadItem"]):
    return None
  return _enum_wire_value(getattr(item, "phase", None))


def _turn_items(turn: Any) -> list[Any]:
  """Unwraps the typed items included in a TurnCompleted payload."""
  items = getattr(turn, "items", None) or []
  return [
    item.root if hasattr(item, "root") else item
    for item in items
  ]


def _codex_terminal_error(
  completed_turn: Any | None,
  sdk: dict[str, Any],
  *,
  interrupt_requested: bool,
  completed_message_phases: list[str | None],
) -> tuple[str | None, str | None, str | None]:
  """Validates the SDK's native turn-status and message-phase contract.

  `turn/completed` is the terminal notification envelope, not proof that the
  model completed the task. The nested TurnStatus carries that fact. Likewise,
  an AgentMessageThreadItem marked `commentary` is an in-progress preamble,
  while `final_answer` is the SDK's explicit completion signal.

  Older SDK payloads can omit both status and message phase; that fully-legacy
  shape retains historical success. Otherwise a completed turn needs an actual
  agent message, and the LAST such message must be final: an earlier final
  followed by commentary is not terminal completion. phase=None remains the
  SDK helper's legacy final-response marker when a message is present.
  """
  if completed_turn is None:
    return (
      "Codex turn stream ended without a turn/completed notification.",
      None,
      None,
    )

  terminal_status = _enum_wire_value(getattr(completed_turn, "status", None))
  failed_status = _enum_wire_value(sdk["TurnStatus"].failed)
  interrupted_status = _enum_wire_value(sdk["TurnStatus"].interrupted)
  completed_status = _enum_wire_value(sdk["TurnStatus"].completed)
  error = getattr(completed_turn, "error", None)
  error_message = getattr(error, "message", None) if error is not None else None

  if terminal_status == failed_status:
    return (
      str(error_message or "Codex turn failed without an error message."),
      terminal_status,
      None,
    )
  if terminal_status == interrupted_status:
    if not interrupt_requested:
      return (
        "Codex turn was interrupted unexpectedly.",
        terminal_status,
        None,
      )
    return None, terminal_status, None
  if terminal_status not in (None, completed_status):
    return (
      f"Codex turn ended with unexpected status {terminal_status!r}.",
      terminal_status,
      None,
    )
  if error is not None:
    return (
      str(error_message or "Codex turn completed with an unknown error."),
      terminal_status,
      None,
    )

  # ItemCompleted normally carries phases first. When TurnCompleted includes
  # agent items, that ordered snapshot is authoritative rather than additive:
  # deduping/merging two streams can reorder messages and turn an earlier final
  # into a false terminal success.
  turn_phases = [
    _agent_message_phase(item, sdk)
    for item in _turn_items(completed_turn)
    if isinstance(item, sdk["AgentMessageThreadItem"])
  ]
  phases = turn_phases if turn_phases else list(completed_message_phases)
  commentary_phase = _enum_wire_value(sdk["MessagePhase"].commentary)
  final_answer_phase = _enum_wire_value(sdk["MessagePhase"].final_answer)
  if not phases:
    if terminal_status is None:
      return None, terminal_status, None
    return (
      "Codex turn completed without an agent final answer.",
      terminal_status,
      None,
    )

  final_message_phase = phases[-1]
  if final_message_phase == commentary_phase:
    return (
      "Codex turn completed after commentary without a final answer.",
      terminal_status,
      final_message_phase,
    )
  if final_message_phase not in (None, final_answer_phase):
    return (
      f"Codex turn completed with unexpected final message phase "
      f"{final_message_phase!r}.",
      terminal_status,
      final_message_phase,
    )
  return None, terminal_status, final_message_phase


def _skill_names_in_command(command: str, data_dir: str) -> list[str]:
  """Extracts Möbius skill names a shell command reads.

  Codex has no Read tool and no `can_use_tool` hook — its closest
  interception point is the command-execution item stream, where a
  skill load can target either Möbius's authoritative shared tree or
  Codex's project-local `.codex/skills` tree. Provider-neutral semantics count
  only the entry document: a flat shared skill or a directory's SKILL.md.
  Bundled scripts and references are use *after* loading, not another load.
  Returns deduped names in first-mention order.
  """
  if not command:
    return []
  from app.skills import GENERATED_INDEX_STEMS

  shared_root = re.escape(os.path.normpath(
    os.path.join(data_dir, "shared", "skills")
  ))
  codex_root = re.escape(os.path.normpath(
    os.path.join(data_dir, ".codex", "skills")
  ))
  # The boundary keeps relative forms from matching the tail of an unrelated
  # absolute path. Shell punctuation terminates a resource path; the detector
  # is lexical on purpose and never reads the referenced file.
  boundary = r"(?<![A-Za-z0-9._/-])"
  name_part = r"[A-Za-z0-9][A-Za-z0-9._-]*"
  shared_pattern = re.compile(
    boundary
    + rf"(?:{shared_root}|shared/skills)/"
    + rf"(?P<shared_name>{name_part})"
    + r"(?:\.md\b|/(?i:SKILL\.md)\b)"
  )
  codex_pattern = re.compile(
    boundary
    + rf"(?:{codex_root}|\.codex/skills)/"
    + rf"(?:\.system/)?(?P<codex_name>{name_part})/(?i:SKILL\.md)\b"
  )

  matches = [
    (match.start(), match.group("shared_name"))
    for match in shared_pattern.finditer(command)
  ]
  matches.extend(
    (match.start(), match.group("codex_name"))
    for match in codex_pattern.finditer(command)
  )

  names: list[str] = []
  for _, name in sorted(matches):
    # Reading a generated index is consulting a listing, not loading a skill.
    if name not in names and name not in GENERATED_INDEX_STEMS:
      names.append(name)
  return names


def _observe_skill_reads(
  item: Any, sdk: dict[str, Any], *, bc: Any, chat_id: str,
) -> None:
  """Fire-and-forget `skill_loaded` events for skill-file shell reads.

  Mirrors `observe_skill_file_read` in claude_sdk_runner: same targeted wire
  receipt, same activity record (most-used-skills aggregation).
  Never raises — observability must not break the notification loop.
  """
  try:
    if not isinstance(item, sdk["CommandExecutionThreadItem"]):
      return
    from app import activity
    from app.config import get_settings
    command = _extract_bash_command(item.command or "")
    skills = _skill_names_in_command(command, get_settings().data_dir)
    tool_use_id = getattr(item, "id", None)
    for skill in skills:
      bc.publish({
        "type": "skill_loaded",
        "skill": skill,
        **({"tool_use_id": tool_use_id} if tool_use_id else {}),
      })
      activity.log_skill_load(chat_id, skill)
  except Exception:
    log.debug("codex skill_loaded observability failed", exc_info=True)


def _file_change_patch_summary(changes: list[Any]) -> str:
  """Summarize file changes without exposing provider model reprs."""
  lines: list[str] = []
  for change in changes:
    change_dict = _model_dump(change) or {}
    raw_kind = change_dict.get("kind")
    kind = raw_kind if isinstance(raw_kind, dict) else {}
    kind_type = str(kind.get("type") or "update")
    path = change_dict.get("path", "")
    move_path = kind.get("move_path")
    if kind_type == "add":
      line = f"Added {path}".strip()
    elif kind_type == "delete":
      line = f"Deleted {path}".strip()
    elif isinstance(move_path, str) and move_path:
      line = f"Moved {path} → {move_path}".strip()
    else:
      line = f"Updated {path}".strip()
    if line:
      lines.append(line)
  return "\n".join(lines)


def _file_change_edit_preview(changes: list[Any]) -> dict | None:
  """Normalize SDK file changes into the shared bounded diff preview."""
  return codex_edit_preview([_model_dump(change) for change in changes])
