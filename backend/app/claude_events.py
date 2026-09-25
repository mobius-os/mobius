"""Claude SDK messages translated into stable Möbius wire events.

This module owns provider message interpretation only. Process acquisition, turn
control, permission callbacks, and retry policy remain in ``claude_sdk_runner``.
Keeping the translation matrix independent makes SDK shape changes testable
without entering the long-lived runner lifecycle.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk.types import (
  AssistantMessage,
  RateLimitEvent,
  ResultMessage,
  ServerToolResultBlock,
  ServerToolUseBlock,
  StreamEvent,
  SystemMessage,
  TaskNotificationMessage,
  TaskProgressMessage,
  TaskStartedMessage,
  TextBlock,
  ThinkingBlock,
  ToolResultBlock,
  ToolUseBlock,
  UserMessage,
)

try:
  from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, TaskUpdatedMessage
except ImportError:
  TERMINAL_TASK_STATUSES = frozenset()
  TaskUpdatedMessage = None

# The SDK's generic task ledger admits only finite delegated-agent work. Mirror
# that exact private constant here. Arbitrary background shells are excluded
# because they may never emit a terminal frame.
try:
  from claude_agent_sdk._internal.query import DEFERRING_TASK_TYPES
except ImportError:
  DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})

from app import activity
from app.tool_edit_preview import claude_edit_preview
from app.tool_summaries import summarize_tool_input
from app.tool_sources import normalize_tool_sources, sources_from_websearch_text
from app.usage_metrics import (
  claude_call_input_tokens,
  context_usage_event,
  normalize_claude_usage,
)

log = logging.getLogger(__name__)

# Bounds for the subagent task_* text fields. Unlike ordinary tool output these
# never pass through the excerpt/stash reducer, so they are clipped at emission
# to keep an oversized provider string off the wire, the in-memory event log,
# and Chat.messages. A description/summary is a one-line label + short outcome; a
# last_tool_name is a tool name — both are generous.
_TASK_TEXT_CAP = 2000
_TASK_LABEL_CAP = 200


@dataclass
class NativeContinuationTracker:
  """Track Claude-native work through the result that wakes its parent.

  A task may settle immediately before its spawning turn's ResultMessage. A
  plain in-flight set is empty at that boundary and makes the runner reap the
  CLI one result too early. Remember that ordering and defer exactly that next
  result; tasks already observed across a result need no extra deferral because
  their eventual result is the parent continuation itself. A root assistant
  message after completion proves the parent already reacted and closes the
  provisional deferral.

  Ordinary background shells can run forever, so they never own turn
  completion here.
  """

  _active_tasks: set[str] = field(default_factory=set)
  _tasks_seen_at_result: set[str] = field(default_factory=set)
  _settled_before_result: bool = False

  @property
  def pending_count(self) -> int:
    return len(self._active_tasks) + int(self._settled_before_result)

  def task_started(
    self,
    task_id: str | None,
    task_type: str | None,
    tool_use_id: str | None,
  ) -> None:
    if not task_id:
      return
    if task_type in DEFERRING_TASK_TYPES:
      self._active_tasks.add(task_id)

  def task_finished(self, task_id: str | None) -> None:
    if not task_id or task_id not in self._active_tasks:
      return
    self._active_tasks.discard(task_id)
    if task_id not in self._tasks_seen_at_result:
      self._settled_before_result = True
    self._tasks_seen_at_result.discard(task_id)

  def root_continuation_observed(self) -> None:
    """Mark a pre-result completion as already consumed by its parent.

    Task bookkeeping alone cannot distinguish a completion that will wake a
    parent after the next result from one the parent has already synthesized.
    A later root AssistantMessage is that missing boundary signal: its result
    closes the continuation instead of opening another one.
    """
    self._settled_before_result = False

  def observe_result(self) -> bool:
    """Record one result; return whether it is not yet the run boundary."""
    if self._active_tasks:
      self._tasks_seen_at_result.update(self._active_tasks)
      # This result also covers any task that settled just before it. Other
      # active work already keeps the connection open for the same follow-up.
      self._settled_before_result = False
      return True
    if self._settled_before_result:
      self._settled_before_result = False
      return True
    return False


def is_root_conversation_message(message: Any) -> bool:
  """Whether one Claude conversation frame belongs to the owner turn.

  Claude streams native Agent/Task work on the same SDK connection as its
  parent, but marks each child-sidechain StreamEvent, AssistantMessage, and
  UserMessage with ``parent_tool_use_id``. Only frames without that parent own
  the root chat row. Child activity is already represented by the separate
  Task lifecycle and by the parent's later synthesis.

  Result/System messages do not expose this field and are root lifecycle
  frames, so the runner can use this predicate for its broader session-id gate.
  """
  return getattr(message, "parent_tool_use_id", None) is None


def _clip_task_text(value: object, cap: int) -> str | None:
  """Coerce a task_* text field to a bounded string (or None).

  None passes through as None (a genuinely absent field). Anything else is
  str()-coerced — so SDK shape drift that hands us a non-string can't ride
  through to a React child and crash the render — then truncated to ``cap``.
  """
  if value is None:
    return None
  text = value if isinstance(value, str) else str(value)
  if len(text) > cap:
    return text[: cap - 1] + "…"
  return text


def _thinking_event(content: str, segment_id: str | None = None) -> dict:
  """Build a reasoning delta, preserving its content-block identity."""
  event = {
    "type": "thinking",
    "content": content,
    "ts": int(time.time() * 1000),
  }
  if segment_id:
    event["segment_id"] = segment_id
  return event


def _skill_name_from_input(input_data: Any) -> str:
  """Extracts the loaded skill's name from a Skill tool_use input.

  The Skill tool's input is `{"skill": "<name>", "args": "..."}` — the
  skill name lives under the `skill` key. Older / plugin-namespaced
  forms can carry it as `command` (the slash-command name), so fall
  back to that. Returns an empty string when neither is present so the
  caller can decide not to emit an empty chip.
  """
  if not isinstance(input_data, dict):
    return ""
  name = input_data.get("skill") or input_data.get("command") or ""
  return name.strip() if isinstance(name, str) else ""


def _result_error_message(result: ResultMessage) -> str:
  """Builds a user-facing error string from an SDK result."""
  if isinstance(result.result, str) and result.result.strip():
    return result.result.strip()
  if result.errors:
    # The bundled CLI attaches an internal `[ede_diagnostic] ...
    # stop_reason=tool_use/null` entry whenever its end-of-turn
    # validator trips — which it does on a Möbius-initiated interrupt
    # (the message list ends on a synthetic user-interrupt entry), not
    # only on real failures. Surfacing that raw string renders a scary
    # red error block for what was a clean Stop, eroding trust. Filter
    # ede entries out (mirroring the CLI's own diagnostic filter); if
    # that leaves nothing, fall through to the friendly message below.
    visible = [
      err for err in result.errors
      if err and not err.lstrip().startswith("[ede_diagnostic]")
    ]
    if visible:
      return "\n".join(visible).strip()
  if result.subtype == "error_during_execution":
    return "Execution interrupted."
  return "Claude SDK turn failed."


def _format_tool_output(content: Any) -> str:
  """Formats SDK tool-result content for Möbius tool_output events."""
  if content is None:
    return ""
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    parts: list[str] = []
    for item in content:
      if isinstance(item, str):
        parts.append(item)
        continue
      if isinstance(item, dict) and item.get("type") == "text":
        text = item.get("text")
        if isinstance(text, str):
          parts.append(text)
          continue
      parts.append(json.dumps(item, ensure_ascii=True))
    return "\n".join(part for part in parts if part).strip()
  return json.dumps(content, ensure_ascii=True)


def _server_web_search_input(inp: dict[str, Any]) -> str:
  """Return the displayed query from Claude's server web_search input."""
  if not isinstance(inp, dict):
    return ""
  query = inp.get("query")
  if isinstance(query, str):
    return query
  queries = inp.get("queries")
  if isinstance(queries, list):
    return ", ".join(q for q in queries if isinstance(q, str))
  return ""


def _is_web_search_tool_result(content: Any) -> bool:
  """True when Claude's opaque server result is a web-search result."""
  if not isinstance(content, dict):
    return False
  return content.get("type") == "web_search_tool_result"


def _log_unknown(kind: str, raw: Any) -> None:
  """Record an SDK event Möbius does not handle.

  Unhandled events are never broadcast: no client renders them, and on a
  busy chat the per-token progress events alone were most of the stream and
  of every reconnect replay.
  """
  log.debug("unhandled Claude SDK event: kind=%s raw=%r", kind, raw)


# Tools that are pure harness mechanics: they carry no owner-facing meaning, so
# they never become visible activity. `TaskOutput` is the agent waiting on work
# it already started. Möbius's own ultracode reminder tells the agent to wait in
# ten-minute blocks and re-issue the wait when one elapses, so a long turn used
# to render as a wall of "TaskOutput … retrieval_status: timeout" rows — a
# countdown expiring, read by the owner as the product timing out and failing.
# The work itself is reported as prose; the waiting is not something to show.
#
# Suppression is symmetric by `tool_use_id`: dropping the start while still
# publishing the end would leave the frontend reducer holding a `tool_end` for a
# block it never opened.
_MECHANICS_TOOLS = frozenset({"TaskOutput"})


def _suppressed_tool_ids(bc) -> set[str]:
  """Per-broadcast ids whose tool events are mechanics, not activity."""
  ids = getattr(bc, "_mechanics_tool_ids", None)
  if ids is None:
    ids = set()
    try:
      bc._mechanics_tool_ids = ids
    except AttributeError:
      # A test double that forbids attributes still suppresses the start; the
      # matching end simply falls through as it did before.
      return set()
  return ids


def _claude_block_starts(bc) -> dict:
  """Per-broadcast FIFO of streamed block indices, keyed by (message, kind).

  Claude Code re-sends each completed content block as its OWN single-block
  AssistantMessage, so the final's position inside that message is always 0
  while its stream events carry the real content-block index. Recording the
  index at each ``content_block_start`` lets a completed block reclaim the
  identity of the stream it completes: the Nth final text (or thinking) block
  of a message is the Nth streamed block of that kind.
  """
  starts = getattr(bc, "_claude_block_starts", None)
  if starts is None:
    starts = {}
    try:
      bc._claude_block_starts = starts
    except AttributeError:
      return {}
  return starts


def _claude_final_index(bc, message_id: str | None, kind: str) -> int | None:
  """The streamed content-block index a completed block of ``kind`` finishes."""
  starts = _claude_block_starts(bc)
  queue = starts.get((message_id, kind))
  if not queue:
    return None
  index = queue.pop(0)
  if not queue:
    del starts[(message_id, kind)]
  return index


def _claude_thinking_segment_id(
  message_id: str | None, index: object,
) -> str | None:
  """Turn-unique identity of one Claude thinking content block.

  The content-block index resets on every model call, so the index alone
  merged separate calls' thinking into one paragraph and could not name the
  block a completed ThinkingBlock repairs.
  """
  if index is None:
    return None
  if message_id is None:
    return f"claude:content:{index}"
  return f"claude:{message_id}:{index}"


def _claude_text_item_id(message_id: str | None, index: object) -> str | None:
  """A turn-unique id for a Claude text content block.

  Claude streams a text block's deltas and then re-sends its authoritative full
  text (the AssistantMessage TextBlock) to repair any dropped delta — but with
  no provider item id, so events.py could only pair delta and final
  POSITIONALLY. When one message carries several text blocks, that pairing
  lands the earlier block's authoritative text on the wrong (trailing) slot, so
  a leading chunk dropped from an earlier block is never repaired. The Anthropic
  message id plus the content-block index names the exact block on both sides,
  so the reducer repairs by identity. The index resets per message, so the
  message id is required to stay turn-unique. Returns None when either part is
  missing, so the reducer keeps its positional fallback (no regression) instead
  of minting a colliding id.
  """
  if message_id is None or index is None:
    return None
  return f"{message_id}:{index}"


def dispatch_sdk_message(
  sdk_msg: Any,
  bc,
  current_session_id: str | None,
  native_work: NativeContinuationTracker | None = None,
  usage_state: dict[str, Any] | None = None,
) -> tuple[str | None, dict | None]:
  """Translates one SDK message into broadcast events.

  Returns ``(new_session_id, terminal_result_or_None)``. When the
  message is a ResultMessage, the caller receives the final result
  dict and stops draining the SDK stream. For every other message
  type the caller updates ``current_session_id`` from the first
  return value and keeps reading.

  When ``native_work`` is passed, the same branches that classify native tasks
  maintain the continuation boundary. The runner can then keep the live client
  through Claude's parent follow-up
  without treating an unrelated long-running shell as turn-owned work.

  ``usage_state`` carries the latest root AssistantMessage usage to the
  terminal ResultMessage. Claude's result usage aggregates the whole agent
  turn; the assistant usage is the one-call value needed to measure current
  context occupancy without counting every tool-loop refill.

  Extracted from the runner loop so unit tests can exercise the
  full dispatch matrix (named events, unknown fallthrough, usage
  + stop_reason side channels) without spinning up a live SDK
  subprocess.
  """
  if (
    isinstance(sdk_msg, (StreamEvent, AssistantMessage, UserMessage))
    and not is_root_conversation_message(sdk_msg)
  ):
    # A native child turn is a sidechain, not another piece of the owner's
    # assistant row. Its Task lifecycle remains visible and the root agent
    # later emits its own synthesis.
    return current_session_id, None

  if isinstance(sdk_msg, SystemMessage):
    if isinstance(sdk_msg, TaskStartedMessage):
      # tool_use_id ties this sub-task back to the parent turn's tool call that
      # spawned it, so an observer can nest task events under their tool block.
      # description/summary/last_tool_name are clipped at emission: unlike
      # ordinary tool output they bypass the excerpt/stash reduction, so an
      # oversized provider string would otherwise ride the wire, the in-memory
      # event log, and Chat.messages verbatim. Clipping also coerces a
      # non-string (SDK shape drift) to text so a downstream render can't crash.
      public_event = {
        "type": "task_start",
        "task_id": sdk_msg.task_id,
        "description": _clip_task_text(sdk_msg.description, _TASK_TEXT_CAP),
        "task_type": sdk_msg.task_type,
        "tool_use_id": sdk_msg.tool_use_id,
      }
      recorder = getattr(bc, "record_lifecycle", None)
      if callable(recorder):
        recorder({**public_event,
          "provider_session_id": (
            getattr(sdk_msg, "session_id", None) or current_session_id
          ),
          "source_event_id": getattr(sdk_msg, "uuid", None),
        })
      bc.publish(public_event)
      if native_work is not None:
        native_work.task_started(
          sdk_msg.task_id,
          getattr(sdk_msg, "task_type", None),
          getattr(sdk_msg, "tool_use_id", None),
        )
      return current_session_id, None
    if isinstance(sdk_msg, TaskProgressMessage):
      bc.publish({
        "type": "task_progress",
        "task_id": sdk_msg.task_id,
        "usage": dict(sdk_msg.usage) if sdk_msg.usage else None,
        "last_tool_name": _clip_task_text(sdk_msg.last_tool_name, _TASK_LABEL_CAP),
        "tool_use_id": sdk_msg.tool_use_id,
      })
      return current_session_id, None
    if isinstance(sdk_msg, TaskNotificationMessage):
      public_event = {
        "type": "task_done",
        "task_id": sdk_msg.task_id,
        "status": sdk_msg.status,
        "summary": _clip_task_text(sdk_msg.summary, _TASK_TEXT_CAP),
        "tool_use_id": sdk_msg.tool_use_id,
      }
      recorder = getattr(bc, "record_lifecycle", None)
      if callable(recorder):
        recorder({**public_event,
          "provider_session_id": (
            getattr(sdk_msg, "session_id", None) or current_session_id
          ),
          "source_event_id": getattr(sdk_msg, "uuid", None),
        })
      bc.publish(public_event)
      if native_work is not None:
        native_work.task_finished(sdk_msg.task_id)
      return current_session_id, None
    if TaskUpdatedMessage is not None and isinstance(sdk_msg, TaskUpdatedMessage):
      # A background task's terminal state can arrive ONLY as a task_updated
      # patch, with no accompanying TaskNotificationMessage — a task stopped via
      # TaskStop reports status "killed" here and the matching notification is
      # sometimes suppressed. Publish the same task_done shape on a terminal
      # status so a consumer clears the task on a terminal signal from EITHER
      # message. Non-terminal updates (pending/running/paused, or a patch with
      # no status) carry no lifecycle-close a task_done would represent, so they
      # are intentionally dropped rather than surfaced as noise. summary and
      # tool_use_id are read via getattr — the SDK class omits them, so they
      # resolve to None and the task_done shape stays uniform across both paths.
      if sdk_msg.status in TERMINAL_TASK_STATUSES:
        private_event = {
          "type": "task_done",
          "task_id": sdk_msg.task_id,
          "status": sdk_msg.status,
          "summary": getattr(sdk_msg, "summary", None),
          "tool_use_id": getattr(sdk_msg, "tool_use_id", None),
          "provider_session_id": (
            getattr(sdk_msg, "session_id", None) or current_session_id
          ),
          "source_event_id": getattr(sdk_msg, "uuid", None),
          "occurred_at": (getattr(sdk_msg, "patch", None) or {}).get(
            "end_time"
          ),
        }
        recorder = getattr(bc, "record_lifecycle", None)
        if callable(recorder):
          recorder(private_event)
        bc.publish({key: private_event.get(key) for key in (
          "type", "task_id", "status", "summary", "tool_use_id")})
        if native_work is not None:
          native_work.task_finished(sdk_msg.task_id)
      return current_session_id, None
    if sdk_msg.subtype == "init":
      # Setup metadata only — no Möbius-side render.
      return current_session_id, None
    _log_unknown(f"system:{sdk_msg.subtype}", sdk_msg)
    return current_session_id, None

  if isinstance(sdk_msg, StreamEvent):
    if sdk_msg.session_id:
      current_session_id = sdk_msg.session_id
    event = sdk_msg.event
    event_type = event.get("type")
    if event_type == "message_start":
      # Capture the Anthropic message id so this message's text deltas can be
      # stamped with a turn-unique id that the AssistantMessage's authoritative
      # text_final matches by identity (see _claude_text_item_id). The id is not
      # on the content_block events themselves, only here.
      message = event.get("message") or {}
      bc.current_message_id = message.get("id")
      return current_session_id, None
    if event_type == "content_block_delta":
      delta = event.get("delta", {})
      delta_type = delta.get("type")
      if delta_type == "text_delta":
        text = delta.get("text")
        if text:
          item_id = _claude_text_item_id(
            getattr(bc, "current_message_id", None), event.get("index"),
          )
          bc.publish({
            "type": "text", "content": text,
            **({"text_item_id": item_id} if item_id else {}),
          })
        return current_session_id, None
      if delta_type == "thinking_delta":
        thinking = delta.get("thinking") or delta.get("text") or ""
        if thinking:
          segment_id = _claude_thinking_segment_id(
            getattr(bc, "current_message_id", None), event.get("index"),
          )
          bc.publish(_thinking_event(thinking, segment_id))
        return current_session_id, None
      _log_unknown(f"stream:content_block_delta:{delta_type}", delta)
      return current_session_id, None
    if event_type == "content_block_start":
      # A new assistant content block is starting. When it is a TEXT
      # block, emit a provider boundary so the reducer (events.py) starts
      # a fresh paragraph instead of concatenating into the prior text —
      # the Claude analog of the Codex AgentMessageThreadItem boundary.
      # The reducer self-guards (it only inserts a marker when the prior
      # block is non-empty text), so emitting on every text block-start
      # is safe: it only takes effect on the consecutive-text case, e.g.
      # text resuming after an AskUserQuestion answer, which otherwise
      # glued together as "answer1.answer2" with no separator.
      cb = event.get("content_block") or {}
      block_type = cb.get("type") if isinstance(cb, dict) else None
      message_id = getattr(bc, "current_message_id", None)
      index = event.get("index")
      if block_type in ("text", "thinking") and index is not None:
        _claude_block_starts(bc).setdefault(
          (message_id, block_type), [],
        ).append(index)
      if block_type == "text":
        bc.publish({"type": "text_boundary"})
        # A block may open with content already present; the deltas carry
        # only what follows it.
        initial = cb.get("text")
        if initial:
          item_id = _claude_text_item_id(message_id, index)
          bc.publish({
            "type": "text", "content": initial,
            **({"text_item_id": item_id} if item_id else {}),
          })
        return current_session_id, None
      if block_type == "thinking":
        initial = cb.get("thinking")
        if initial:
          bc.publish(_thinking_event(
            initial, _claude_thinking_segment_id(message_id, index),
          ))
        return current_session_id, None
    _log_unknown(f"stream:{event_type}", event)
    return current_session_id, None

  if isinstance(sdk_msg, AssistantMessage):
    if sdk_msg.session_id:
      current_session_id = sdk_msg.session_id
    if native_work is not None:
      native_work.root_continuation_observed()
    if usage_state is not None and sdk_msg.usage:
      usage_state["latest_model_usage"] = dict(sdk_msg.usage)
    server_tools: dict[str, str] = {}
    for block in sdk_msg.content:
      if isinstance(block, ToolUseBlock):
        # Mechanics never reach the transcript — see _MECHANICS_TOOLS. Remember
        # the id so the matching result is dropped with it.
        if block.name in _MECHANICS_TOOLS:
          _suppressed_tool_ids(bc).add(block.id)
          continue
        # block.id is the canonical tool_use_id; the matching ToolResultBlock
        # carries it as .tool_use_id. Thread it through so a large tool output
        # can be reduced on the wire and fetched lazily by id (contract rule 6).
        bc.publish({
          "type": "tool_start",
          "tool": block.name,
          "input": "",
          "tool_use_id": block.id,
        })
        summary = summarize_tool_input(block.name, block.input)
        edit_preview = claude_edit_preview(block.name, block.input)
        if summary or edit_preview:
          bc.publish({
            "type": "tool_input",
            "tool": block.name,
            "input": summary,
            "tool_use_id": block.id,
            **({"edit_preview": edit_preview} if edit_preview else {}),
          })
        # Skill observability: when the agent loads a skill, surface it
        # as its own targeted `skill_loaded` event (the frontend presents the
        # owning tool as a skill-read block) and append a record to the activity
        # log so "most-used skills" can be aggregated. This fires
        # whenever the Skill tool runs at all; whether skills are even
        # OFFERED to the agent is the separate, gated `skills_enabled`
        # decision below — observability is correct either way.
        if block.name == "Skill":
          skill = _skill_name_from_input(block.input)
          if skill:
            bc.publish({
              "type": "skill_loaded",
              "skill": skill,
              "tool_use_id": block.id,
            })
            activity.log_skill_load(getattr(bc, "chat_id", None), skill)
        continue
      if isinstance(block, ServerToolUseBlock):
        server_tools[block.id] = block.name
        if block.name == "web_search":
          bc.publish({
            "type": "tool_start",
            "tool": "WebSearch",
            "input": _server_web_search_input(block.input),
            "tool_use_id": block.id,
          })
          continue
        _log_unknown(f"assistant_block:{type(block).__name__}", block)
        continue
      if isinstance(block, ServerToolResultBlock):
        tool_name = server_tools.get(block.tool_use_id)
        if (
          tool_name == "web_search"
          or _is_web_search_tool_result(block.content)
        ):
          sources = normalize_tool_sources(block.content)
          if sources:
            bc.publish({
              "type": "tool_sources",
              "sources": sources,
              "tool_use_id": block.tool_use_id,
            })
          bc.publish({
            "type": "tool_end",
            "tool_use_id": block.tool_use_id,
          })
          continue
        _log_unknown(f"assistant_block:{type(block).__name__}", block)
        continue
      if isinstance(block, ThinkingBlock):
        # The completed block is the record; the thinking_delta stream was
        # its live preview. Emit it so the reducer replaces the streamed
        # segment it completes (a no-op when nothing was lost), or
        # materialises it when no delta for it arrived at all.
        if block.thinking:
          index = _claude_final_index(bc, sdk_msg.message_id, "thinking")
          segment_id = _claude_thinking_segment_id(sdk_msg.message_id, index)
          if index is None:
            # Never streamed (no block start seen): it is new content.
            bc.publish(_thinking_event(block.thinking, segment_id))
          else:
            bc.publish({
              "type": "thinking_final",
              "content": block.thinking,
              "segment_id": segment_id,
            })
        continue
      if isinstance(block, TextBlock):
        # The text already streamed live via text_delta events; this is the
        # AUTHORITATIVE full text of the just-completed assistant item. Do NOT
        # discard it — durable prose otherwise rides ONLY on the delta stream,
        # so a single dropped/coalesced delta persists a permanently truncated
        # message (the "I " bug). Emit it as a replace-semantics event; events.py
        # overwrites the streamed text block with this complete text (a no-op
        # when no delta was lost). Tool blocks are already sourced from this
        # same message object and so were always durable — this closes the gap
        # for text. Replace, never append: the reducer concatenates plain
        # "text" events, so re-emitting as "text" would double the prose.
        # The item id (message id + the STREAMED content-block index, not the
        # block's position in this single-block message) matches the streamed
        # deltas' id, so events.py replaces THIS block by identity instead of
        # guessing the trailing text block.
        if block.text:
          item_id = _claude_text_item_id(
            sdk_msg.message_id,
            _claude_final_index(bc, sdk_msg.message_id, "text"),
          )
          bc.publish({
            "type": "text_final", "content": block.text,
            **({"text_item_id": item_id} if item_id else {}),
          })
        continue
      _log_unknown(f"assistant_block:{type(block).__name__}", block)
    if sdk_msg.usage:
      bc.publish(context_usage_event(
        "claude", claude_call_input_tokens(sdk_msg.usage),
      ))
    if sdk_msg.stop_reason:
      bc.publish({
        "type": "stop_reason",
        "reason": sdk_msg.stop_reason,
      })
    return current_session_id, None

  if isinstance(sdk_msg, UserMessage):
    content = sdk_msg.content if isinstance(sdk_msg.content, list) else []
    for block in content:
      if isinstance(block, ToolResultBlock):
        # The paired half of the mechanics suppression above.
        if block.tool_use_id in _suppressed_tool_ids(bc):
          _suppressed_tool_ids(bc).discard(block.tool_use_id)
          continue
        output = _format_tool_output(block.content)
        # Carry the tool_use_id (matches the ToolUseBlock's .id) so the sink can
        # key a stash of the full output and the block can fetch it by id.
        bc.publish({
          "type": "tool_output",
          "content": output,
          "tool_use_id": block.tool_use_id,
          "output_complete": True,
          **({"output_exit_code": 1} if block.is_error else {}),
        })
        if output.startswith("Web search results for query"):
          sources = sources_from_websearch_text(output)
          if sources:
            # Carry the same tool_use_id as the output it was parsed from. A
            # turn can batch several WebSearch calls, and their results arrive
            # together — without the id the consumer can only guess "the last
            # WebSearch block", so every batch member lands on one block and
            # overwrites the previous, keeping only the final search's sources.
            bc.publish({
              "type": "tool_sources",
              "sources": sources,
              "tool_use_id": block.tool_use_id,
            })
        bc.publish({"type": "tool_end", "tool_use_id": block.tool_use_id})
        continue
      _log_unknown(f"user_block:{type(block).__name__}", block)
    return current_session_id, None

  if isinstance(sdk_msg, RateLimitEvent):
    info = sdk_msg.rate_limit_info
    bc.publish({
      "type": "rate_limit",
      "status": info.status,
      "resets_at": info.resets_at,
      "rate_limit_type": info.rate_limit_type,
      "utilization": info.utilization,
    })
    return current_session_id, None

  if isinstance(sdk_msg, ResultMessage):
    if sdk_msg.session_id:
      current_session_id = sdk_msg.session_id
    if sdk_msg.stop_reason:
      bc.publish({
        "type": "stop_reason",
        "reason": sdk_msg.stop_reason,
      })
    return current_session_id, {
      "session_id": current_session_id,
      "cost_usd": sdk_msg.total_cost_usd,
      "usage": dict(sdk_msg.usage) if sdk_msg.usage else None,
      "usage_metrics": normalize_claude_usage(
        sdk_msg.usage,
        sdk_msg.model_usage,
        (usage_state or {}).get("latest_model_usage"),
      ),
      "model_usage": (
        dict(sdk_msg.model_usage) if sdk_msg.model_usage else None
      ),
      "permission_denials": sdk_msg.permission_denials or None,
      "api_error_status": sdk_msg.api_error_status,
      "error": (
        _result_error_message(sdk_msg)
        if sdk_msg.is_error else None
      ),
    }

  # Any SDK message class we didn't enumerate is logged, never broadcast.
  _log_unknown(f"sdk_message:{type(sdk_msg).__name__}", sdk_msg)
  return current_session_id, None
