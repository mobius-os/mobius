"""Claude SDK turn runner for Möbius.

This module isolates the Claude Agent SDK integration behind one
function that executes exactly one Möbius chat turn and publishes the
same event shapes the rest of the backend already understands.

Design choices:

- The runner stays in the SDK's default permission mode and registers a
  dummy PreToolUse keepalive hook so `can_use_tool` still fires.
  `can_use_tool` auto-approves every tool except `AskUserQuestion`, which
  becomes an explicit partner choice in the Möbius UI. Using
  `permission_mode="bypassPermissions"` would skip `can_use_tool` and
  break that interception.
- `ClaudeSDKClient` is used instead of one-shot `query()` because
  Möbius needs the bidirectional control surface: explicit `connect()`,
  `query()`, streaming `receive_response()`, and external
  `interrupt()` support for Stop.
- `AskUserQuestion` is intercepted via the SDK's `can_use_tool`
  callback (NOT PreToolUse/PostToolUse hooks). The callback parks a
  future in the shared `pending_questions` registry, broadcasts the
  question event, and awaits the user's answer. When the answer
  arrives (POST /messages with body.answers, resolved by
  routes/chats_stream.py), the callback returns
  `PermissionResultAllow(updated_input={"questions": ..., "answers": ...})`
  — the SDK then runs `AskUserQuestion` with the answers as input and
  the tool's headless implementation echoes them back as the result
  the model sees. PreToolUse/PostToolUse was explored and rejected:
  the SDK does NOT fire PostToolUse for AskUserQuestion in headless
  mode, so the two-hook flow never worked.
- Stop and steer support is wired through the shared runner registry. The
  caller looks up the registered `ActiveClaudeClient` handle and
  interrupts the live SDK client while this runner keeps draining
  `receive_response()` until the terminal result arrives.
- `system_prompt` is passed on EVERY turn, not just the first. The
  installed SDK transport
  (`claude_agent_sdk/_internal/transport/subprocess_cli.py:227-228`)
  serializes `system_prompt is None → --system-prompt ""`, which on
  resume silently wipes the original session's system prompt. Since
  ClaudeAgentOptions defaults `system_prompt` to `None`, omitting the
  kwarg has the same effect. Always passing `skill_text` keeps the
  skill load-bearing across resumes and matches our "skill is always-
  on" contract — passing the same text on resume is a no-op; passing
  updated text after a deploy correctly updates the resumed session.
- We deliberately pass `skill_text` as a custom string (not
  `SystemPromptPreset{append=skill_text, exclude_dynamic_sections=True}`).
  The preset+append form would layer Claude Code's default
  engineer-facing preset on top of our Möbius skill — adding
  generic tool-use / communication guidance that our skill already
  defines in Möbius-specific terms (and sometimes contradicts).
  `exclude_dynamic_sections` only applies with the default preset
  (the CLI ignores it with `--system-prompt`, per the CLI's own
  `--help`), so for our custom-string path it would be a no-op
  even if we set it. Möbius owns its system prompt end-to-end;
  the skill is the contract, not a layer on top of someone else's.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from collections import deque
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import (
  AssistantMessage,
  PermissionResultAllow,
  PermissionResultDeny,
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

from app import activity
from app.pending_questions import PendingQuestion
from app.runner_registry import RunnerKind, registry
from app.runtime_types import RunnerResult
from app.sdk_emit import emit_unknown_enabled, unknown_event
from app.tool_summaries import summarize_tool_input
from app.tool_sources import normalize_tool_sources, sources_from_websearch_text


def _thinking_event(content: str) -> dict:
  """Build the provider-agnostic reasoning event with runner time."""
  return {
    "type": "thinking",
    "content": content,
    "ts": int(time.time() * 1000),
  }


async def _persist_session_id(db, chat_id: str, session_id: str | None) -> None:
  """Best-effort early persistence for provider resume continuity."""
  if db is None or not chat_id or not session_id:
    return
  try:
    from app.chat_writer import PersistSessionId, await_ack, get_writer
    ack = get_writer().submit(
      PersistSessionId(chat_id=chat_id, session_id=session_id)
    )
    await await_ack(ack)
  except Exception:
    log.warning(
      "Claude session id persistence failed chat_id=%s session_id=%s",
      chat_id,
      session_id,
      exc_info=True,
    )


def _resumable(
  session_id: str | None, cwd: str, config_dir: str | None = None
) -> bool:
  """True iff a transcript .jsonl for session_id exists for this cwd.

  `claude --resume <id>` reads the transcript the CLI stored under
  `<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/<id>.jsonl`, where the
  project dir encodes the cwd by stripping the leading slash and
  replacing every `/` with `-` (cwd `/data` -> `-data`, cwd
  `/data/apps/news-2` -> `-data-apps-news-2`). A stored id can fail to
  resolve two ways, both of which make `--resume` die "No conversation
  found" (exit 1): a pre-fix PHANTOM id (the codex plugin's SessionStart
  hook minted an id that got a `session-env/<id>` dir but never a
  transcript), or a real id whose transcript the CLI's ~30-day cleanup
  has since deleted. Callers use this to fall back to a DB-transcript
  reseed instead of letting the turn hard-fail.

  The `-data` derivation is verified against prod: every stored
  session id that resolves on disk lives under `projects/-data/`, and
  `fork-chat.sh` resumes chat sessions from the same `/data` cwd.
  """
  if not session_id:
    return False
  base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
  if not base:
    return False
  proj = "-" + cwd.strip("/").replace("/", "-")
  return os.path.isfile(
    os.path.join(base, "projects", proj, f"{session_id}.jsonl")
  )


class ActiveClaudeClient:
  """Stop/steer handle registered for SDK-backed Claude turns.

  Ordering contract: `interrupt()` signals the SDK and then awaits
  `_finished`, which the runner resolves ONLY after `client.disconnect()`
  returns. Callers (stop_chat / stop_chat_for) therefore block until
  the SDK subprocess is fully torn down, so a late `bc.publish(done)`
  from the runner cannot land after `bc.mark_completed()` has already
  closed the broadcast for live SSE subscribers.
  """

  def __init__(self, client: ClaudeSDKClient, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self._client = client
    # FIFO of mid-turn steer texts: two rapid sends must both reach Claude
    # (both are already persisted to the transcript), so a single slot would
    # silently drop the first. The runner drains the whole list on interrupt.
    self.pending_steer: list[str] = []
    # A steer was requested but not yet cut over. The runner clears the
    # turn at the NEXT completed content-block boundary (an AssistantMessage)
    # rather than mid-token, so the user sees the finished sentence/thought
    # before the steer takes over. Set by `steer()`, consumed by the runner.
    self._steer_requested = False
    # One interrupt is in flight: an `interrupt()` has been signalled but the
    # terminal ResultMessage that ends the interrupted turn has not arrived
    # yet. Guards the boundary cut so a second steer (or a second
    # AssistantMessage) in the drain window can't fire a duplicate interrupt
    # before the SDK has closed the first; the runner clears it on the
    # terminal result. Stop's hard `interrupt()` does not consult this — Stop
    # always cuts immediately.
    self._interrupt_in_flight = False
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

  async def steer(self, text: str) -> bool:
    """Buffers a steer to cut in at the next content-block boundary.

    Claude's SDK cannot append to an in-flight tool loop, and the only
    mid-turn lever is `interrupt()` — but interrupting on the token that
    happens to be streaming throws away a half-finished sentence or
    thinking trace. Instead we record the redirect text and FLAG the
    request; the runner watches its own `receive_response()` loop and
    interrupts only after the next COMPLETED content block is published
    (an `AssistantMessage`), so the user sees the finished thought, then
    the steer takes over. The existing drain-then-requery path on the
    interrupt's terminal result delivers the buffered text on the same
    connected client, preserving session context. (Stop is the separate
    immediate-cut path — see `interrupt()`.)
    """
    if self._finished.done():
      return False
    self.pending_steer.append(text)
    self._steer_requested = True
    return True

  async def interrupt(self) -> None:
    """Interrupts the live run and waits for runner-side drain.

    Bounds the `_finished` wait at 5s as a defense-in-depth so a
    wedged runner (one that never reaches its `finally` block) can't
    hang Stop indefinitely. `chat.py:stop_chat_for` adds its own 2s
    bound at the call site; this inner timeout protects any other
    direct caller.

    Stop is the hard, immediate-cut path: it drops any buffered steer
    (clearing `pending_steer` + `_steer_requested` so no boundary cut or
    requery fires for work the user just abandoned) and interrupts the
    live turn right now, without waiting for a content-block boundary.
    """
    self.pending_steer = []
    self._steer_requested = False
    await self._client.interrupt()
    try:
      await asyncio.wait_for(self._finished, timeout=5.0)
    except asyncio.TimeoutError:
      import logging
      logging.getLogger("moebius.chat").warning(
        "ActiveClaudeClient._finished never resolved within 5s; "
        "runner is wedged",
      )

  async def stop(self, timeout: float = 2.0) -> bool:
    """Interrupts the SDK run and waits up to `timeout` seconds."""
    try:
      await asyncio.wait_for(self.interrupt(), timeout=timeout)
      return True
    except asyncio.CancelledError:
      raise
    except asyncio.TimeoutError:
      log.warning(
        "Claude SDK stop timed out chat_id=%s", self.chat_id,
      )
      return False
    except Exception:
      log.exception(
        "Claude SDK stop failed chat_id=%s", self.chat_id,
      )
      return False

  def mark_finished(self) -> None:
    """Resolves the stop waiter once the runner is fully drained."""
    if not self._finished.done():
      self._finished.set_result(None)


def _steer_redirect_message(text: str) -> str:
  """Frames a Claude steer as a redirect on the still-connected client."""
  return (
    "The user added this while you were working. Incorporate it and "
    "continue the same task:\n\n"
    f"{text}"
  )


async def steer_into_active_turn(chat_id: str, text: str) -> bool:
  """Interrupts a live Claude SDK turn so it can resume with `text`."""
  handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
  if not isinstance(handle, ActiveClaudeClient):
    return False
  return await handle.steer(text)


def _skill_file_read_name(
  tool_name: str, input_data: Any, cwd: str,
) -> str:
  """Returns the skill name when a Read targets a Möbius skill file.

  The in-product agent loads its skills by Reading
  `<data_dir>/shared/skills/<name>.md` — on the default posture
  (skills_enabled off) the SDK Skill tool is never offered, so the
  Read input is the only place skill loads are actually observable.
  The match is purely lexical (normpath, no filesystem access) and
  returns "" for anything that isn't a direct skill-file read. A
  relative path is resolved against the turn's cwd: the agent runs
  with cwd=/data, so `shared/skills/memory.md` is the same load.
  """
  if tool_name != "Read" or not isinstance(input_data, dict):
    return ""
  raw = input_data.get("file_path")
  if not isinstance(raw, str) or not raw.strip():
    return ""
  path = raw.strip()
  if not os.path.isabs(path):
    path = os.path.join(cwd or "/", path)
  path = os.path.normpath(path)
  from app.config import get_settings
  skills_dir = os.path.normpath(
    os.path.join(get_settings().data_dir, "shared", "skills")
  )
  parent, filename = os.path.split(path)
  if parent != skills_dir or not filename.endswith(".md"):
    return ""
  return filename[: -len(".md")]


def observe_skill_file_read(
  tool_name: str,
  input_data: Any,
  *,
  bc,
  chat_id: str,
  cwd: str,
) -> None:
  """Fire-and-forget skill observability for skill-file Reads.

  Publishes the same `skill_loaded` event + activity record the Skill
  tool path emits (see the dispatch below), so the activity log's
  most-used-skills cross-check sees Read-based loads too — before
  this, the cross-check endpoint returned empty every night because
  the agent never goes through the Skill tool. Never raises: a broken
  broadcast or a full disk must not block or fail the tool call being
  intercepted.
  """
  try:
    skill = _skill_file_read_name(tool_name, input_data, cwd)
    if not skill:
      return
    bc.publish({"type": "skill_loaded", "skill": skill})
    activity.log_skill_load(chat_id, skill)
  except Exception:
    log.debug("skill_loaded read observability failed", exc_info=True)


def _memory_node_read_id(
  tool_name: str, input_data: Any, cwd: str,
) -> str:
  """Returns the memory-graph node id when a Read targets a note or MOC.

  The agent descends the graph by Reading
  `<data_dir>/shared/memory/{notes,mocs}/<slug>.md` — exactly the
  explicit-read signal the per-chat read-trace wants (the injected
  block is recorded separately at the injection site). index.md,
  inbox.md, and recent-chats.md don't count: they arrive injected, so
  a Read of them says nothing about what the agent dug for. Purely
  lexical, like `_skill_file_read_name` above; returns "" for anything
  that isn't a direct note/MOC read."""
  if tool_name != "Read" or not isinstance(input_data, dict):
    return ""
  raw = input_data.get("file_path")
  if not isinstance(raw, str) or not raw.strip():
    return ""
  path = raw.strip()
  if not os.path.isabs(path):
    path = os.path.join(cwd or "/", path)
  path = os.path.normpath(path)
  from app.config import get_settings
  memory_root = os.path.normpath(
    os.path.join(get_settings().data_dir, "shared", "memory")
  )
  parent, filename = os.path.split(path)
  if os.path.dirname(parent) != memory_root:
    return ""
  if os.path.basename(parent) not in ("notes", "mocs"):
    return ""
  if not filename.endswith(".md"):
    return ""
  return filename[: -len(".md")]


def observe_memory_node_read(
  tool_name: str,
  input_data: Any,
  *,
  chat_id: str,
  cwd: str,
) -> None:
  """Fire-and-forget read-trace entry for memory-node Reads.

  Mirrors `observe_skill_file_read`: called from `can_use_tool` on
  every non-question tool, filters down to note/MOC reads, and merges
  the slug into this chat's read-trace file so the nightly Reflection
  pass can see what the agent went looking for. Never raises — a full
  disk or broken trace file must not block or fail the Read being
  intercepted."""
  try:
    node_id = _memory_node_read_id(tool_name, input_data, cwd)
    if not node_id:
      return
    from app import memory_trace
    from app.config import get_settings
    memory_trace.record_note_read(
      get_settings().data_dir, chat_id, node_id
    )
  except Exception:
    log.debug("memory read-trace observability failed", exc_info=True)


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


def _should_retry_without_model(error_text: str | None) -> bool:
  """True when Claude rejected the explicit model selection."""
  if not error_text:
    return False
  text = error_text.lower()
  return (
    "selected model" in text
    and "may not exist or you may not have access" in text
  )


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


async def _maybe_await(value: Any) -> Any:
  """Awaits a value only when the callback returned an awaitable."""
  if inspect.isawaitable(value):
    return await value
  return value


def _emit_unknown(bc, kind: str, raw: Any) -> None:
  """Logs an unknown SDK event and emits it on the wire when enabled.

  The DEBUG log fires unconditionally so noisy sessions stay
  inspectable in `chat.log` even when wire emission is turned off
  via ``MOBIUS_EMIT_UNKNOWN=0``.
  """
  event = unknown_event(kind, raw)
  if emit_unknown_enabled():
    bc.publish(event)


def _usage_event(usage: dict[str, Any]) -> dict:
  """Builds the wire-shape `usage` event from an SDK usage dict.

  The SDK's usage shape evolves — we extract the fields we know
  about today and pass the full dict through under ``raw`` so a
  later UI can pick up newly-added counters without a runner change.
  """
  return {
    "type": "usage",
    "input_tokens": usage.get("input_tokens"),
    "output_tokens": usage.get("output_tokens"),
    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
    "raw": dict(usage),
  }


def dispatch_sdk_message(
  sdk_msg: Any,
  bc,
  current_session_id: str | None,
) -> tuple[str | None, dict | None]:
  """Translates one SDK message into broadcast events.

  Returns ``(new_session_id, terminal_result_or_None)``. When the
  message is a ResultMessage, the caller receives the final result
  dict and stops draining the SDK stream. For every other message
  type the caller updates ``current_session_id`` from the first
  return value and keeps reading.

  Extracted from the runner loop so unit tests can exercise the
  full dispatch matrix (named events, unknown fallthrough, usage
  + stop_reason side channels) without spinning up a live SDK
  subprocess.
  """
  if isinstance(sdk_msg, SystemMessage):
    if isinstance(sdk_msg, TaskStartedMessage):
      bc.publish({
        "type": "task_start",
        "task_id": sdk_msg.task_id,
        "description": sdk_msg.description,
        "task_type": sdk_msg.task_type,
      })
      return current_session_id, None
    if isinstance(sdk_msg, TaskProgressMessage):
      bc.publish({
        "type": "task_progress",
        "task_id": sdk_msg.task_id,
        "usage": dict(sdk_msg.usage) if sdk_msg.usage else None,
        "last_tool_name": sdk_msg.last_tool_name,
      })
      return current_session_id, None
    if isinstance(sdk_msg, TaskNotificationMessage):
      bc.publish({
        "type": "task_done",
        "task_id": sdk_msg.task_id,
        "status": sdk_msg.status,
        "summary": sdk_msg.summary,
      })
      return current_session_id, None
    if sdk_msg.subtype == "init":
      # Setup metadata only — no Möbius-side render.
      return current_session_id, None
    _emit_unknown(bc, f"system:{sdk_msg.subtype}", sdk_msg)
    return current_session_id, None

  if isinstance(sdk_msg, StreamEvent):
    if sdk_msg.session_id:
      current_session_id = sdk_msg.session_id
    event = sdk_msg.event
    event_type = event.get("type")
    if event_type == "content_block_delta":
      delta = event.get("delta", {})
      delta_type = delta.get("type")
      if delta_type == "text_delta":
        text = delta.get("text")
        if text:
          bc.publish({"type": "text", "content": text})
        return current_session_id, None
      if delta_type == "thinking_delta":
        thinking = delta.get("thinking") or delta.get("text") or ""
        if thinking:
          bc.publish(_thinking_event(thinking))
        return current_session_id, None
      _emit_unknown(bc, f"stream:content_block_delta:{delta_type}", delta)
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
      if isinstance(cb, dict) and cb.get("type") == "text":
        bc.publish({"type": "text_boundary"})
        return current_session_id, None
    _emit_unknown(bc, f"stream:{event_type}", event)
    return current_session_id, None

  if isinstance(sdk_msg, AssistantMessage):
    if sdk_msg.session_id:
      current_session_id = sdk_msg.session_id
    server_tools: dict[str, str] = {}
    for block in sdk_msg.content:
      if isinstance(block, ToolUseBlock):
        bc.publish({
          "type": "tool_start",
          "tool": block.name,
          "input": "",
        })
        summary = summarize_tool_input(block.name, block.input)
        if summary:
          bc.publish({
            "type": "tool_input",
            "tool": block.name,
            "input": summary,
          })
        # Skill observability: when the agent loads a skill, surface it
        # as its own `skill_loaded` event (the frontend stamps a chip
        # onto the Skill tool block) and append a record to the activity
        # log so "most-used skills" can be aggregated. This fires
        # whenever the Skill tool runs at all; whether skills are even
        # OFFERED to the agent is the separate, gated `skills_enabled`
        # decision below — observability is correct either way.
        if block.name == "Skill":
          skill = _skill_name_from_input(block.input)
          if skill:
            bc.publish({"type": "skill_loaded", "skill": skill})
            activity.log_skill_load(getattr(bc, "chat_id", None), skill)
        continue
      if isinstance(block, ServerToolUseBlock):
        server_tools[block.id] = block.name
        if block.name == "web_search":
          bc.publish({
            "type": "tool_start",
            "tool": "WebSearch",
            "input": _server_web_search_input(block.input),
          })
          continue
        _emit_unknown(
          bc, f"assistant_block:{type(block).__name__}", block,
        )
        continue
      if isinstance(block, ServerToolResultBlock):
        tool_name = server_tools.get(block.tool_use_id)
        if (
          tool_name == "web_search"
          or _is_web_search_tool_result(block.content)
        ):
          sources = normalize_tool_sources(block.content)
          if sources:
            bc.publish({"type": "tool_sources", "sources": sources})
          bc.publish({"type": "tool_end"})
          continue
        _emit_unknown(
          bc, f"assistant_block:{type(block).__name__}", block,
        )
        continue
      if isinstance(block, ThinkingBlock):
        # Streamed via thinking_delta already — snapshot duplicate.
        continue
      if isinstance(block, TextBlock):
        # Streamed via text_delta already — snapshot duplicate.
        continue
      _emit_unknown(
        bc, f"assistant_block:{type(block).__name__}", block,
      )
    if sdk_msg.usage:
      bc.publish(_usage_event(sdk_msg.usage))
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
        output = _format_tool_output(block.content)
        bc.publish({
          "type": "tool_output",
          "content": output,
        })
        if output.startswith("Web search results for query"):
          sources = sources_from_websearch_text(output)
          if sources:
            bc.publish({"type": "tool_sources", "sources": sources})
        bc.publish({"type": "tool_end"})
        continue
      _emit_unknown(bc, f"user_block:{type(block).__name__}", block)
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
    if sdk_msg.usage:
      bc.publish(_usage_event(sdk_msg.usage))
    if sdk_msg.stop_reason:
      bc.publish({
        "type": "stop_reason",
        "reason": sdk_msg.stop_reason,
      })
    return current_session_id, {
      "session_id": current_session_id,
      "cost_usd": sdk_msg.total_cost_usd,
      "usage": dict(sdk_msg.usage) if sdk_msg.usage else None,
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

  # Any SDK message class we didn't enumerate — never silently dropped.
  _emit_unknown(bc, f"sdk_message:{type(sdk_msg).__name__}", sdk_msg)
  return current_session_id, None


# Appended to a turn's prompt when the owner picks the "ultracode" effort
# tier. "ultracode" is not an SDK EffortLevel — it is the Claude Code CLI's
# ultracode mode (xhigh effort + dynamic multi-agent Workflow orchestration).
# The SDK only exposes effort as `--effort <value>` and that flag rejects
# "ultracode", so the effort knob is set to xhigh separately and the
# orchestration is armed via the CLI's documented "ultracode" keyword
# trigger: a turn whose prompt contains the word opts that turn into the
# Workflow tool. The trigger is default-on, model-gated to ultracode-capable
# (Opus-tier) models, and a graceful no-op on older CLIs or lesser models —
# in which case the turn simply runs at xhigh with no orchestration. The
# literal token "ultracode" below is what arms it; keep it in the text.
_ULTRACODE_TRIGGER = (
  "\n\n<system-reminder>Ultracode mode is enabled for this turn: you are "
  "running at xhigh effort with the Workflow tool available for dynamic "
  "multi-agent orchestration. For substantial multi-step work, decompose it "
  "and use the Workflow tool; answer trivial turns directly. (The ultracode "
  "keyword in this reminder is what arms the mode.)</system-reminder>"
)


async def run_claude_sdk_turn(
  user_message: str,
  session_id: str | None,
  base_env: dict[str, str],
  cwd: str,
  chat_id: str,
  skill_text: str,
  bc,
  pending_questions: dict,
  db,
  agent_settings: dict | None = None,
  skills_enabled: bool = False,
) -> RunnerResult:
  """Runs one Claude SDK turn and translates SDK messages to Möbius events.

  Args:
    user_message: Fully prepared user prompt for this turn.
    session_id: Existing Claude session to resume, or None on first turn.
    base_env: Environment passed through to the Claude subprocess.
    cwd: Working directory for the SDK run.
    chat_id: Möbius chat identifier used for registries.
    skill_text: Möbius skill/system prompt text, passed as the system
      prompt on every turn (including resumes).
    bc: Chat broadcast object with a publish(event) method.
    pending_questions: Shared AskUserQuestion registry owned by chat.py.
    db: SQLAlchemy session used by runner-side persistence.
    skills_enabled: When True, offer SDK skills to the agent
      (`setting_sources` including user+project + `skills="all"`). This
      is behavior-shifting and defaults OFF so the skill-observability
      path can ship without changing what the agent does — skill loads
      are still observed (chip + activity log) whenever a skill does
      load, regardless of this flag.

  Returns:
    A dict containing the resulting session ID, final cost, and error.
  """
  current_session_id = session_id
  cost_usd: float | None = None

  # Canonical AskUserQuestion handling via can_use_tool, per
  # https://code.claude.com/docs/en/agent-sdk/user-input
  # The SDK does NOT fire PostToolUse for AskUserQuestion (empirically
  # confirmed). The correct injection point is `can_use_tool`: return
  # PermissionResultAllow with updated_input containing the original
  # questions array plus an `answers` dict {question_text: label}.
  # The SDK then runs the tool with that input and the model sees the
  # answers as the tool's result.
  #
  # `bypassPermissions` would skip the can_use_tool callback entirely —
  # use the documented dummy PreToolUse keepalive + default permission
  # mode so the callback fires only on AskUserQuestion (other tools are
  # auto-approved by the keepalive hook returning continue_=True).
  async def can_use_tool(
    tool_name: str,
    input_data: dict[str, Any],
    context,
  ) -> PermissionResultAllow | PermissionResultDeny:
    del context
    # Auto-approve every tool except AskUserQuestion — this preserves
    # the "trust the agent" posture (no tool gating) while still
    # intercepting AskUserQuestion for the partner UX. The callback is
    # also the canonical observation point for skill-file Reads (the
    # agent loads /data/shared/skills/*.md via Read, not the Skill
    # tool); the observe call is fire-and-forget and never blocks or
    # fails the tool.
    if tool_name != "AskUserQuestion":
      observe_skill_file_read(
        tool_name, input_data, bc=bc, chat_id=chat_id, cwd=cwd,
      )
      observe_memory_node_read(
        tool_name, input_data, chat_id=chat_id, cwd=cwd,
      )
      return PermissionResultAllow(updated_input=input_data)

    questions = input_data.get("questions", [])
    if not isinstance(questions, list):
      questions = []

    existing = pending_questions.get(chat_id)
    if existing is not None and not existing.future.done():
      return PermissionResultDeny(
        message=f"AskUserQuestion already pending for chat {chat_id}"
      )

    future = asyncio.get_running_loop().create_future()
    pending = PendingQuestion(
      question_id=str(uuid4()),
      questions=questions,
      future=future,
      # The turn's persistence run token (the sink carries it). The
      # answer route submits AnswerQuestion keyed on this so the writer
      # actor fences the right (chat_id, run_token) snapshot before
      # merging the answer; None for a sink/broadcast without one
      # (legacy/test) → the answer route broad-fences by chat.
      run_token=getattr(bc, "run_token", None),
    )
    pending_questions[chat_id] = pending

    # Save-before-broadcast (Candidate B): the card's question_id MUST be
    # durably persisted before the SSE event shows it, or a fast Submit
    # races the DB write and the answer is lost. `publish_question`
    # submits a QuestionCommit, awaits its ack, then broadcasts — and
    # RAISES if the commit didn't land. We register the pending entry
    # first (so the finally below always pops it) but on a failed commit
    # DENY the tool with a persistence-unavailable message rather than
    # broadcasting an unpersisted card or writing the blob directly.
    #
    # Push notification on AskUserQuestion is AGENT-DRIVEN: the skill/seed
    # tells the agent to `curl POST /api/notifications/send` itself, so it
    # has direct visibility into push success/failure and decides whether
    # a given question is worth a phone buzz.
    try:
      await bc.publish_question({
        "type": "question",
        "question_id": pending.question_id,
        "questions": questions,
      })
    except Exception as exc:
      log.error(
        "AskUserQuestion save-before-broadcast failed chat_id=%s: %s",
        chat_id, exc,
      )
      if pending_questions.get(chat_id) is pending:
        pending_questions.pop(chat_id, None)
      return PermissionResultDeny(
        message=(
          "Could not save the question (persistence unavailable); not "
          "asking. Please try again."
        )
      )

    try:
      answers = await future
    except asyncio.CancelledError:
      if pending_questions.get(chat_id) is pending:
        pending_questions.pop(chat_id, None)
      return PermissionResultDeny(
        message="AskUserQuestion cancelled."
      )

    if pending_questions.get(chat_id) is pending:
      pending_questions.pop(chat_id, None)

    # Per docs: return updated_input with BOTH the original questions
    # array AND an answers dict {question_text: selected_label}.
    # The SDK passes this through as the tool input; AskUserQuestion's
    # implementation in headless mode echoes the answers back as the
    # tool result the model sees.
    return PermissionResultAllow(
      updated_input={
        "questions": questions,
        "answers": answers,
      }
    )

  # Required workaround per the SDK docs: a dummy PreToolUse hook
  # returning continue_=True keeps the stream open so can_use_tool can
  # be invoked. Without this, the stream closes before the callback
  # fires. See https://code.claude.com/docs/en/agent-sdk/user-input
  async def keepalive_hook(
    hook_input: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
  ) -> dict[str, Any]:
    del hook_input, tool_use_id, context
    return {"continue_": True}

  # Per-chat model/effort overrides flow in via `agent_settings`
  # (merged in chat.py from global defaults + Chat.agent_settings_json).
  # Both are session-wide on the SDK but Möbius spawns one `query()`
  # per turn, so passing them here applies to *this* turn — which is
  # exactly the "apply on next turn" semantics the slash picker promises.
  _model = (agent_settings or {}).get("model") or None
  _effort = (agent_settings or {}).get("effort") or None
  # The "ultracode" tier maps to xhigh effort for the SDK flag (which only
  # accepts low/medium/high/xhigh/max) and arms the Workflow-tool
  # orchestration via the keyword trigger appended to this turn's prompt.
  _ultracode = _effort == "ultracode"
  if _ultracode:
    _effort = "xhigh"
  turn_message = user_message + _ULTRACODE_TRIGGER if _ultracode else user_message
  # Cross-provider mismatch defense (mirrors codex_sdk_runner).
  # Chats persisted before the snapshot logic learned to
  # provider-validate (see chat.py snapshot-on-first-send and
  # effective_agent_settings) can carry a Codex model on a Claude
  # chat. Sending that through here would surface as an obscure SDK
  # error. Quietly normalize so existing chats keep working.
  from app.providers import _model_belongs_to_other_provider, DEFAULT_MODELS
  if _model and _model_belongs_to_other_provider(_model, "claude"):
    log.warning(
      "claude turn started with non-claude model %r — normalizing to %r",
      _model, DEFAULT_MODELS["claude"],
    )
    _model = DEFAULT_MODELS["claude"]
  async def _run_once(model_override: str | None) -> RunnerResult:
    nonlocal current_session_id, cost_usd
    # Skills are gated behind the per-owner `skills_enabled` flag. OFF
    # (the default) keeps the historical posture: `setting_sources=None`
    # means the SDK loads NO user/project settings, so the Skill tool is
    # never offered and no skill can load. ON enables user+project
    # setting sources and `skills="all"` so the agent may load any
    # installed skill — a behavior-shifting change the owner opts into.
    # Observability (the skill_loaded event + activity log) lives in the
    # tool-use dispatch and works whenever a skill loads, independent of
    # this flag.
    # Capture the CLI subprocess's stderr. The SDK transport only pipes
    # stderr when a callback is registered; without one, a CLI that dies
    # before emitting a structured result surfaces the SDK's generic
    # placeholder ("Command failed ... Check stderr output for details")
    # with zero diagnostic content. Bounded so a chatty CLI can't balloon
    # memory; each line truncated. Used only to enrich an opaque failure
    # (see the except below).
    stderr_tail: deque[str] = deque(maxlen=50)

    def _capture_stderr(line: str) -> None:
      if line:
        stderr_tail.append(line.rstrip("\n")[:500])

    options_kwargs = {
      "system_prompt": skill_text,
      "resume": session_id if session_id is not None else None,
      "cwd": cwd,
      "env": base_env,
      "setting_sources": (
        ["user", "project"] if skills_enabled else None
      ),
      "include_partial_messages": True,
      "can_use_tool": can_use_tool,
      "cli_path": "/usr/local/bin/claude",
      "stderr": _capture_stderr,
      "hooks": {
        "PreToolUse": [
          HookMatcher(matcher=None, hooks=[keepalive_hook]),
        ],
      },
    }
    if skills_enabled:
      options_kwargs["skills"] = "all"
    if model_override:
      options_kwargs["model"] = model_override
    if _effort:
      options_kwargs["effort"] = _effort
    options = ClaudeAgentOptions(**options_kwargs)

    client = ClaudeSDKClient(options)
    active_client = ActiveClaudeClient(client, chat_id=chat_id)
    registry.register(active_client)

    try:
      try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
      except asyncio.TimeoutError:
        bc.publish({
          "type": "error",
          "message": "Claude SDK failed to start (connect timeout)",
        })
        return {
          "session_id": current_session_id,
          "cost_usd": None,
          "error": "connect timeout",
        }
      await client.query(turn_message)

      while True:
        async for sdk_msg in client.receive_response():
          # Persist the session id ONLY from real conversation messages.
          # SystemMessage and its subclasses — notably HookEventMessage,
          # which the codex plugin's SessionStart hook emits on every
          # resumed turn — carry a PHANTOM session id that gets a
          # `session-env/<id>` dir but never a transcript `.jsonl`.
          # Persisting that phantom overwrites Chat.session_id with an id
          # the CLI cannot resume, so the next turn dies "No conversation
          # found". Only StreamEvent/Assistant/User/Result carry the
          # resumable id (the same types dispatch advances the session from).
          if isinstance(
            sdk_msg,
            (StreamEvent, AssistantMessage, UserMessage, ResultMessage),
          ):
            incoming_session_id = getattr(sdk_msg, "session_id", None)
            if incoming_session_id and incoming_session_id != current_session_id:
              await _persist_session_id(db, chat_id, incoming_session_id)
          current_session_id, terminal = dispatch_sdk_message(
            sdk_msg, bc, current_session_id,
          )
          if terminal is None:
            # Boundary-fire a buffered steer. `steer()` only flags the
            # request (it no longer interrupts mid-token); we cut over at
            # the next COMPLETED content block, which is exactly when an
            # AssistantMessage is dispatched (the finished text / thinking /
            # tool_use block was just published to the broadcast). The
            # `_interrupt_in_flight` guard makes this fire exactly ONCE per
            # interrupt cycle: a second steer or another AssistantMessage
            # arriving before the interrupt's terminal ResultMessage closes
            # the turn must not issue a duplicate interrupt, and a racing
            # hard Stop (which clears pending_steer) makes the condition
            # no-op. The buffered text is delivered by the existing
            # drain-then-requery path below when that terminal arrives.
            if (
              isinstance(sdk_msg, AssistantMessage)
              and active_client._steer_requested
              and active_client.pending_steer
              and not active_client._interrupt_in_flight
              and not active_client._finished.done()
            ):
              active_client._steer_requested = False
              active_client._interrupt_in_flight = True
              await client.interrupt()
            continue
          # Terminal result: the interrupt cycle (if any) is closed, so a
          # fresh boundary cut may fire on a later turn.
          active_client._interrupt_in_flight = False
          steer_texts = active_client.pending_steer
          if steer_texts:
            active_client.pending_steer = []
            active_client._steer_requested = False
            await client.query(
              _steer_redirect_message("\n\n".join(steer_texts))
            )
            break
          cost_usd = terminal.get("cost_usd")
          return terminal
        else:
          # The stream ended without a terminal ResultMessage. Any buffered
          # steer still gets delivered here (the boundary cut may not have
          # fired — e.g. a tool-only turn with no AssistantMessage text
          # block — so this is the catch-all that preserves the original
          # pending_steer→requery contract).
          active_client._interrupt_in_flight = False
          steer_texts = active_client.pending_steer
          if steer_texts:
            active_client.pending_steer = []
            active_client._steer_requested = False
            await client.query(
              _steer_redirect_message("\n\n".join(steer_texts))
            )
            continue
          break

      return {
        "session_id": current_session_id,
        "cost_usd": cost_usd,
        "usage": None,
        "error": None,
      }
    except Exception as exc:
      msg = str(exc)
      # The SDK raises this generic placeholder when the CLI dies before a
      # structured result (early resume failure, auth, crash, OOM/SIGTERM
      # kill). Splice in the captured stderr tail ONLY then — gating on the
      # placeholder keeps _should_retry_without_model's text matching intact
      # for real structured errors. Empty tail means the process was killed
      # before writing stderr (the OOM/timeout case).
      if "Check stderr output for details" in msg:
        tail = "\n".join(stderr_tail).strip()
        if tail:
          msg = f"{msg}\nstderr (tail):\n{tail}"
        else:
          msg = (
            f"{msg}\n(no stderr captured — the CLI was likely killed "
            "before writing output, e.g. OOM or timeout)"
          )
      return {
        "session_id": current_session_id,
        "cost_usd": None,
        "usage": None,
        "error": msg,
      }
    finally:
      current_handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
      if current_handle is active_client:
        registry.unregister(chat_id, RunnerKind.CLAUDE_SDK)
      pending = pending_questions.get(chat_id)
      if pending is not None and not pending.future.done():
        pending.future.cancel()
      if pending is not None:
        pending_questions.pop(chat_id, None)
      try:
        await client.disconnect()
      finally:
        active_client.mark_finished()

  result = await _run_once(_model)
  if _model and _should_retry_without_model(result.get("error")):
    log.warning(
      "Claude model %r unavailable for chat %s; retrying without explicit model",
      _model,
      chat_id,
    )
    return await _run_once(None)
  return result
