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

# The background-task terminal-state surface (TaskUpdatedMessage + the
# TERMINAL_TASK_STATUSES set) is newer than our SDK FLOOR pin
# (claude-agent-sdk>=0.2.87 in the Dockerfile). Import it defensively so a build
# that resolves an in-range older SDK lacking these symbols still boots the
# whole Claude runner — the task_updated branch below simply becomes a no-op
# (its class is None, so the isinstance guard is never true), matching how the
# Codex runner guards its own newer collab types. When the type is present the
# branch handles killed/stopped background tasks whose terminal state arrives
# ONLY as a task_updated patch.
try:
  from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, TaskUpdatedMessage
except ImportError:
  TERMINAL_TASK_STATUSES = frozenset()
  TaskUpdatedMessage = None

from app import activity
from app.pending_questions import PendingQuestion
from app.runner_registry import RunnerKind, registry
from app.runtime_types import RunnerResult
from app.sdk_emit import emit_unknown_enabled, unknown_event
from app.tool_summaries import summarize_tool_input
from app.tool_sources import normalize_tool_sources, sources_from_websearch_text

log = logging.getLogger(__name__)

# The SDK's 1 MiB default is smaller than a single base64-encoded screenshot
# tool result, so the subprocess transport can reject an otherwise healthy
# turn before Möbius sees the message. This is a per-record ceiling, not a
# preallocation: keep it bounded while leaving enough room for image tools.
_CLAUDE_SDK_MAX_BUFFER_SIZE = 10 * 1024 * 1024


# Bounds for the subagent task_* text fields. Unlike ordinary tool output these
# never pass through the excerpt/stash reducer, so they are clipped at emission
# to keep an oversized provider string off the wire, the in-memory event log,
# and Chat.messages. A description/summary is a one-line label + short outcome; a
# last_tool_name is a tool name — both are generous.
_TASK_TEXT_CAP = 2000
_TASK_LABEL_CAP = 200


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


def _claude_thinking_config(model: str | None) -> dict[str, str] | None:
  """Request displayable thinking summaries on adaptive-thinking models."""
  mid = (model or "").lower()
  if not mid:
    return {"type": "adaptive", "display": "summarized"}
  adaptive_prefixes = (
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-4-7",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable-5",
    "claude-mythos",
  )
  if mid.startswith(adaptive_prefixes):
    return {"type": "adaptive", "display": "summarized"}
  return None


async def _persist_session_id(db, chat_id: str, session_id: str | None) -> None:
  """Best-effort early persistence for provider resume continuity.

  Advances two records from the same sighting: the CURRENT-session pointer on
  the chat row (via the single-writer actor, since it lives on the hot Chat
  row), and the append-only ``chat_session_links`` map. The link write goes
  through ``record_session_link_async``, which commits on its OWN short-lived
  session in a worker thread — NOT the runner's ``db`` (which chat.py closes
  before the long run, and which the later ``Chat.session_id`` save reuses), so
  a link-write stall or failure can neither block the loop nor poison that
  shared session. The ``db`` argument is unused here now, kept for the call
  signature. The link record is what survives the provider switch / session
  reset that later NULLs ``Chat.session_id``.
  """
  if not chat_id or not session_id:
    return
  try:
    from app.chat_writer import PersistSessionId, await_ack, get_writer
    from app.session_links import record_session_link_async
    ack = get_writer().submit(
      PersistSessionId(chat_id=chat_id, session_id=session_id)
    )
    await await_ack(ack)
    await record_session_link_async("claude", session_id, chat_id)
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
    # Transcript-side payload for the buffered steers: the steered user rows +
    # any queued rows they consume. The RUNNER drives the transcript split
    # (seal the pre-interrupt A1, append these user rows, reset the sink for
    # A2) when the interrupted turn ends — the first point the true A1/A2 cut
    # is known. The old route-driven split ran at HTTP arrival, before A1 had
    # streamed, so it sealed an empty A1 and the real A1 then merged with A2
    # after the steered row on reload (Q1, Q2, A1A2 instead of Q1, A1, Q2, A2).
    self._steer_user_msgs: list[dict] = []
    self._steer_consume_cids: list[str] = []
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

  async def steer(
    self,
    text: str,
    user_msgs: list[dict] | None = None,
    consume_pending_cids: list[str] | None = None,
  ) -> bool:
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

    `user_msgs` / `consume_pending_cids` are the transcript-side payload the
    runner replays into `sink.split_for_steer` when the interrupted turn
    ends (seal A1, append these rows, reset for A2). They are buffered here
    rather than split at the route so A1 is the real pre-interrupt text.
    """
    if self._finished.done():
      return False
    # Dedup before buffering. A repeated force-steer of the SAME still-live
    # pending row (common when the client retries a send right after an
    # interrupt) re-delivers the same user_msg / consume cid here. The queued
    # row is not consumed until the interrupt-boundary drain, so without this
    # guard the buffer grows to [msgA, msgA] and the writer persists the row
    # twice. A queued row carries a stable `cid` (see schemas.SendMessage.cid),
    # so keying on cid drops only true re-deliveries and never a genuinely
    # distinct send — even two sends with identical text carry distinct cids.
    #
    # The provider-facing `text` follows the SAME boundary: when every
    # delivered row is a cid-duplicate, the whole call is a re-delivery and
    # the redirect text must not queue a second time — otherwise the durable
    # transcript holds one user message while Claude receives it twice.
    appended_any = False
    if user_msgs:
      from app.chat_writer import cid_of
      buffered_cids = {cid_of(m) for m in self._steer_user_msgs}
      for m in user_msgs:
        mcid = cid_of(m)
        if mcid is not None and mcid in buffered_cids:
          continue
        self._steer_user_msgs.append(m)
        buffered_cids.add(mcid)
        appended_any = True
    if user_msgs and not appended_any:
      return True
    self.pending_steer.append(text)
    if consume_pending_cids:
      buffered_consume = set(self._steer_consume_cids)
      for cid in consume_pending_cids:
        if cid in buffered_consume:
          continue
        self._steer_consume_cids.append(cid)
        buffered_consume.add(cid)
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


async def steer_into_active_turn(
  chat_id: str,
  text: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Interrupts a live Claude SDK turn so it can resume with `text`.

  `user_msgs` / `consume_pending_cids` are buffered on the handle so the
  runner can seal A1 and append the steered rows at the interrupt boundary;
  see `ActiveClaudeClient.steer`.
  """
  handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
  if not isinstance(handle, ActiveClaudeClient):
    return False
  return await handle.steer(text, user_msgs, consume_pending_cids)


async def _seal_steer_split(bc, active_client, chat_id: str) -> None:
  """Seal the pre-interrupt A1 and append the buffered steered user row(s).

  Called at each requery boundary (so A1 is sealed before the answer A2
  streams) AND unconditionally in the turn-end `finally` (so a steer that was
  buffered but never sealed — an exception/early-return before the requery, or
  a hard Stop that cleared `pending_steer` — is still persisted rather than
  discarded with the handle). A1 is the sink's accumulated pre-interrupt
  content — complete once the turn closes — so `split_for_steer` seals it as
  its own message, appends the steered row(s) after it, and resets the sink so
  A2 lands fresh: reload order Q1, A1, Q2, A2.

  This is the fix for the steer-merge: the route cannot know where A1 ends (at
  HTTP arrival A1 has not streamed yet, so a route-side split sealed an empty
  A1 and the real A1 then merged with A2 after the steered row), but the runner
  does. `bc` is the live `_ChatEventSink`; a non-sink `bc` (legacy path / a
  test double) cannot persist here and drops the buffered rows.

  Durability contract (adversarial-review hardening):
  - The rows are snapshotted BEFORE the await and only the snapshotted count is
    removed on success, so a second steer landing during `split_for_steer`'s
    actor round-trips is not wiped (it survives for the next call / the
    finally).
  - On a persistence FAILURE the buffer is left intact so the turn-end
    `finally` retries the write; the rows are not silently dropped after the
    client was already told the steer landed. A persistent failure means the
    writer is down (the whole turn is failing to persist), not a steer-specific
    loss.
  """
  rows = list(active_client._steer_user_msgs)
  if not rows:
    return
  consume = list(active_client._steer_consume_cids)
  split = getattr(bc, "split_for_steer", None)
  if split is None:
    # No live sink (legacy/test caller): there is no streamed A1 to seal
    # against and no way to persist here — drop the buffer.
    active_client._steer_user_msgs = active_client._steer_user_msgs[len(rows):]
    active_client._steer_consume_cids = (
      active_client._steer_consume_cids[len(consume):]
    )
    return
  try:
    await split(rows, consume)
  except Exception:
    # Leave the buffer intact so the turn-end finally retries the write.
    log.exception(
      "steer split failed chat_id=%s; will retry at turn end", chat_id,
    )
    return
  # Success: remove ONLY the rows just sealed; a steer that landed during the
  # await was appended after them and must survive.
  active_client._steer_user_msgs = active_client._steer_user_msgs[len(rows):]
  active_client._steer_consume_cids = (
    active_client._steer_consume_cids[len(consume):]
  )


def _skill_file_read_name(
  tool_name: str, input_data: Any, cwd: str,
) -> str:
  """Returns the skill name when a Read targets a Möbius skill file.

  The in-product agent loads its skills by Reading
  `<data_dir>/shared/skills/<name>.md` (flat) or
  `<data_dir>/shared/skills/<name>/SKILL.md` (the external
  directory convention installed skills use) — on the default posture
  (skills_enabled off) the SDK Skill tool is never offered, so the
  Read input is the only place skill loads are actually observable.
  The match is purely lexical (normpath, no filesystem access) and
  returns "" for anything that isn't a direct skill-file read. A
  relative path is resolved against the turn's cwd: the agent runs
  with cwd=/data, so `shared/skills/example.md` is the same load.
  Deeper resource reads inside a skill directory deliberately do NOT
  count as loads — only the SKILL.md entry document does — and the
  generated `skills-index.md` is the index, not a skill.
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
  if parent == skills_dir and filename.endswith(".md"):
    from app.skills import GENERATED_INDEX_STEMS

    name = filename[: -len(".md")]
    return "" if name in GENERATED_INDEX_STEMS else name
  grandparent, dirname = os.path.split(parent)
  if grandparent == skills_dir and filename.upper() == "SKILL.MD" and dirname:
    return dirname
  return ""


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
      # tool_use_id ties this sub-task back to the parent turn's tool call that
      # spawned it, so an observer can nest task events under their tool block.
      # description/summary/last_tool_name are clipped at emission: unlike
      # ordinary tool output they bypass the excerpt/stash reduction, so an
      # oversized provider string would otherwise ride the wire, the in-memory
      # event log, and Chat.messages verbatim. Clipping also coerces a
      # non-string (SDK shape drift) to text so a downstream render can't crash.
      bc.publish({
        "type": "task_start",
        "task_id": sdk_msg.task_id,
        "description": _clip_task_text(sdk_msg.description, _TASK_TEXT_CAP),
        "task_type": sdk_msg.task_type,
        "tool_use_id": sdk_msg.tool_use_id,
      })
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
      bc.publish({
        "type": "task_done",
        "task_id": sdk_msg.task_id,
        "status": sdk_msg.status,
        "summary": _clip_task_text(sdk_msg.summary, _TASK_TEXT_CAP),
        "tool_use_id": sdk_msg.tool_use_id,
      })
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
        bc.publish({
          "type": "task_done",
          "task_id": sdk_msg.task_id,
          "status": sdk_msg.status,
          "summary": getattr(sdk_msg, "summary", None),
          "tool_use_id": getattr(sdk_msg, "tool_use_id", None),
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
          block_index = event.get("index")
          segment_id = (
            f"claude:content:{block_index}"
            if block_index is not None else None
          )
          bc.publish(_thinking_event(thinking, segment_id))
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
        if summary:
          bc.publish({
            "type": "tool_input",
            "tool": block.name,
            "input": summary,
            "tool_use_id": block.id,
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
            "tool_use_id": block.id,
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
        _emit_unknown(
          bc, f"assistant_block:{type(block).__name__}", block,
        )
        continue
      if isinstance(block, ThinkingBlock):
        # Streamed via thinking_delta already — snapshot duplicate.
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
        if block.text:
          bc.publish({"type": "text_final", "content": block.text})
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
        # Carry the tool_use_id (matches the ToolUseBlock's .id) so the sink can
        # key a stash of the full output and the block can fetch it by id.
        bc.publish({
          "type": "tool_output",
          "content": output,
          "tool_use_id": block.tool_use_id,
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


# Injected when the owner picks the "ultracode" effort tier. Ultracode (xhigh
# effort + standing dynamic-workflow orchestration) is armed the DOCUMENTED way
# — the CLI's `ultracode` settings flag, passed below — NOT by putting the word
# "ultracode" in the prompt. That keyword trigger (`workflowKeywordTriggerEnabled`,
# default-on) is an interactive-CLI convenience and is brittle here: a stray
# "ultracode" token in injected memory/context arms the whole Workflow fleet on a
# turn the owner never opted into (the observed "$32 for a restaurant question").
# We disable the keyword trigger and drive ultracode purely by the flag, so this
# reminder carries only behavioural guidance and deliberately contains NO arming
# keyword. Möbius's turn is one-shot (no post-turn re-invoke), so the agent must
# await its Workflow within the turn or the fleet's work is lost when the turn ends.
_ULTRACODE_REMINDER = (
  "\n\n<system-reminder>You have the Workflow tool for dynamic multi-agent "
  "orchestration this turn. Use it for substantial multi-step work; answer "
  "trivial turns directly.\n\n"
  "This runtime gives you exactly ONE turn per message and CANNOT wake you "
  "after it ends — there is no background notification and no follow-up turn. "
  "So any Workflow (or background task) you launch must be fully awaited AND its "
  "result delivered WITHIN this same turn, or the work is lost and the partner "
  "is left with a dead turn. Concretely: right after launching a Workflow, block "
  "on it here — call TaskOutput(task_id=..., block=True, timeout=600000) (run "
  "ToolSearch \"select:TaskOutput\" first if it is not loaded). If that returns "
  "retrieval_status: timeout while the workflow is still running, call it again "
  "and keep re-blocking until it finishes — verifying between checks that it is "
  "still making progress (if it is genuinely stuck, say so and deliver what you "
  "have rather than silently abandoning it). Then synthesize the result and give "
  "the full answer in this turn.\n\n"
  "All of this waiting is invisible harness mechanics. Never tell the partner "
  "you are waiting, blocking, or polling, and never mention Workflow, "
  "TaskOutput, subagents, or background tasks in chat. Before you first block, "
  "write ONE partner-facing sentence about what is being worked on (e.g. "
  "\"Reviewing all 13 apps now — this takes a few minutes.\"). While blocked, "
  "write nothing; when TaskOutput returns retrieval_status: timeout, call it "
  "again immediately with no text in between. Add prose only when you have a new "
  "finding to report — phrased as progress, not mechanism. NEVER let your final "
  "output be \"I'll let you know when it's done\" or \"waiting for the workflow "
  "to finish\" — you will not get another turn to finish.</system-reminder>"
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
  turn_message = user_message + _ULTRACODE_REMINDER if _ultracode else user_message
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
    # Most recent provider rate-limit reset time seen this attempt (from any
    # RateLimitEvent). Threaded into the terminal result so a 429/limit kill
    # can park until the STRUCTURED reset time rather than parsing the error
    # string (design §2.4). Lives HERE, in the attempt scope where it is
    # assigned — an outer-scope init would be shadowed by that assignment and
    # read unbound on turns with no rate-limit event.
    rate_limit_resets_at = None
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
      "max_buffer_size": _CLAUDE_SDK_MAX_BUFFER_SIZE,
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
    thinking_config = _claude_thinking_config(model_override)
    if thinking_config is not None:
      options_kwargs["thinking"] = thinking_config
    if _effort:
      options_kwargs["effort"] = _effort
    # Arm ultracode via its documented `ultracode` settings flag; on every other
    # turn set the documented `disableWorkflows` flag so a stray "ultracode" token
    # in injected memory/context can't arm the Workflow fleet on a turn the owner
    # did not opt into (the observed "$32 for a restaurant question"). Both keys
    # are documented + stable in the Claude Code settings reference — unlike the
    # binary-only `workflowKeywordTriggerEnabled`, which we deliberately avoid.
    # Passed via --settings as inline JSON.
    _cli_settings = {"ultracode": True} if _ultracode else {"disableWorkflows": True}
    options_kwargs["extra_args"] = {"settings": json.dumps(_cli_settings)}
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

      # At most one automatic re-query per turn (see the synthetic-resume
      # recovery in the terminal branch below), so a genuinely-empty resume
      # can never loop.
      did_auto_requery = False
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
          if isinstance(sdk_msg, RateLimitEvent):
            _resets = getattr(sdk_msg.rate_limit_info, "resets_at", None)
            if _resets is not None:
              rate_limit_resets_at = _resets
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
            # Seal A1 + append the steered row(s) BEFORE the requery so the
            # answer (A2) lands as a fresh message. The turn-end finally is the
            # durability catch-all for a steer that never reaches a requery.
            await _seal_steer_split(bc, active_client, chat_id)
            active_client.pending_steer = []
            active_client._steer_requested = False
            await client.query(
              _steer_redirect_message("\n\n".join(steer_texts))
            )
            break
          # Recover a synthetic no-op RESUME. When a resumed session's prior
          # turn was interrupted (e.g. a server restart with a dangling
          # background task), the Claude CLI can spend the resumed turn
          # RECONCILING state — it writes a synthetic "No response requested."
          # close-out and returns a CLEAN terminal (is_error False) WITHOUT ever
          # running the model on the real prompt, so the sink accrues zero
          # blocks and the reply silently vanishes (proven from CLI transcripts:
          # the "continue" case, chat 04ef66df). Re-ask the same prompt ONCE to
          # force a real answer — exactly what a manual re-send recovers, and
          # what a second reconciled turn produced in the wild. Bounded by
          # did_auto_requery so a legitimately-empty resume cannot loop; if the
          # retry is also empty the finalize backstop records a retry marker.
          if (
            session_id is not None            # a resume (non-first turn)
            and not terminal.get("error")     # clean terminal (is_error False)
            and terminal.get("api_error_status") != 429  # not a bare 429/park
            and not active_client.pending_steer
            and not did_auto_requery
            and len(bc.assistant_blocks) == 0  # zero blocks: the synthetic no-op
          ):
            did_auto_requery = True
            log.info(
              "claude resume produced no reply (synthetic no-op); "
              "auto-requerying once chat_id=%s", chat_id,
            )
            await client.query(turn_message)
            break
          cost_usd = terminal.get("cost_usd")
          if rate_limit_resets_at is not None:
            terminal.setdefault("rate_limit_resets_at", rate_limit_resets_at)
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
            # Seal A1 before the requery (see the terminal-result branch); the
            # turn-end finally covers the no-requery case.
            await _seal_steer_split(bc, active_client, chat_id)
            active_client.pending_steer = []
            active_client._steer_requested = False
            await client.query(
              _steer_redirect_message("\n\n".join(steer_texts))
            )
            continue
          break

      # Reaching here means the outer while broke out of the resultless-end
      # path above (line ~1237): the SDK stream ended WITHOUT a terminal
      # ResultMessage and with no pending steer to requery. A successful turn
      # returns its terminal at `return terminal` above and never falls
      # through here. So this is an error exit, not a clean turn — the CLI
      # died mid-stream (early resume failure, auth, OOM/SIGTERM kill) before
      # emitting a result. Return it error-shaped so chat.py publishes the
      # error and finalize() persists a durable error block, instead of the
      # old silent `error=None` that logged a clean $0 "done" and let the
      # just-consumed user message go unanswered with nothing to reconcile.
      return {
        "session_id": current_session_id,
        "cost_usd": cost_usd,
        "usage": None,
        "error": (
          "The response ended unexpectedly before it finished "
          "(the agent stopped without returning a result). Please try again."
        ),
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
      # Durability catch-all: persist any steer that was buffered but never
      # sealed at a requery boundary — an exception/early return above, or a
      # hard Stop that cleared pending_steer. Runs before disconnect so the
      # sink is still live. No-op when nothing is buffered (the normal path
      # sealed + cleared it already). Never raises (swallowed inside).
      await _seal_steer_split(bc, active_client, chat_id)
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
