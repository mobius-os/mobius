"""Codex SDK turn runner for Möbius.

Codex's `TurnHandle.steer()` is the product win that motivated this
module. Unlike Claude's serial SDK flow, Codex exposes a live turn
handle that can accept in-band user steering while the current turn is
still running. Möbius can keep its existing queued-message behavior for
Claude while upgrading Codex chats to true mid-turn injection.

This module runs one Codex SDK turn, translates streamed SDK
notifications into Möbius broadcast events, relies on the SDK's
default auto-approval behavior, and stores the live `ActiveCodexTurn`
in the shared runner registry, keyed by `(chat_id, RunnerKind.CODEX_SDK)`,
so Stop and queued-message steering can reach it.

**AskUserQuestion parity is shipped via the `request_user_input`
tool.** The underlying wire surface is `item/tool/requestUserInput`
JSON-RPC requests emitted by the app-server when the model calls
the tool. `CodexClient.approval_handler` is the documented
constructor argument that receives them (public as of
openai-codex 0.134.0; was a private attribute on a less-stable
path before). Only the sync `CodexClient` accepts
`approval_handler` in its constructor. The async wrappers
(`AsyncCodex`, `AsyncCodexClient`) don't, so we set the
attribute on `codex._client._sync` after construction, targeting
the same callable slot the public constructor argument populates.
See `_install_request_user_input_handler` below.

Why we don't drop `AsyncCodex` and construct `CodexClient`
directly to pass `approval_handler` as a kwarg: doing so would
mean rebuilding everything `AsyncCodex` / `AsyncThread` /
`AsyncTurnHandle` give us for free. That list includes lazy
`start()` + `initialize()` + metadata validation, the
`ApprovalMode` enum translation to `(approval_policy,
approvals_reviewer)` via private `_approval_mode_settings`
helpers, `ThreadStartParams` / `TurnStartParams` Pydantic
construction, `_normalize_run_input` + `_to_wire_input`
translation, `register_turn_notifications` +
`next_turn_notification` polling that terminates on
`turn/completed`, and the `AsyncThread` / `AsyncTurnHandle`
context. That's ~100 lines of plumbing built on four private SDK
helpers, replacing one public-attribute set on a wrapper-internal
chain. The current pattern has the smaller fragility surface.
Revisit if `AsyncCodex` ever grows `approval_handler` in its
constructor (forwarded down to `_sync`), at which point
`_install_request_user_input_handler` collapses to a kwarg.

The tool is gated by the `default_mode_request_user_input`
feature flag (stage `UnderDevelopment`, default off), enabled via
`features.default_mode_request_user_input=true` in the
`CodexConfig.config_overrides` list. Once enabled, the model
sees `request_user_input` in its tool list and uses it the same
way Claude uses its `AskUserQuestion` tool — both producers
publish a `question` event on the Möbius wire and both wait on
the shared `_pending_questions` future for the user's answer.
The handler then translates Möbius's text-keyed answer back into
Codex's id-keyed `{answers: {qid: {answers: [label]}}}` schema.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import json
import logging
import os
import re
import signal
import shutil
import time
from typing import Any, Callable

from app.codex_appserver import _extract_bash_command
from app.providers import get_skill_path
from app.runtime_types import RunnerResult
from app.runner_registry import RunnerKind, registry
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


def _env_flag_on(name: str, *, default: bool) -> bool:
  """Read a boolean env var: ``off``/``0``/``false``/``no``/empty disable it;
  anything else enables; unset falls back to ``default``."""
  raw = os.environ.get(name)
  if raw is None:
    return default
  return raw.strip().lower() not in ("off", "0", "false", "no", "")


def _codex_config_overrides() -> list[str]:
  """Assemble the Codex ``CodexConfig.config_overrides`` for a turn.

  ``request_user_input`` (AskUserQuestion parity) is always on. Multi-agent
  (collab / spawn_agent — the Codex analog of Claude's Task fleet, whose
  ``collabAgentToolCall`` items the dispatch surfaces as ordinary background
  activity) is on by DEFAULT but behind a RUNTIME kill switch: set the env var
  ``MOEBIUS_CODEX_MULTI_AGENT`` to off/0/false/no to disable it and restart
  uvicorn — a runtime rollback that needs no image rebuild, since the overrides
  are read fresh per turn.

  When enabled, the tool namespace is PINNED to ``agents``. Codex #31864: the
  pinned SDK source still DEFAULTS multi_agent_v2's spawn_agent tool to the
  ``collaboration`` namespace, which gpt-5.6 reserves, so the Responses API can
  reject the tool schema on EVERY turn (not only spawn turns). A live probe on
  0.144.5 spawned a sub-agent cleanly under the observed default, but the model
  rollout is server-side and mutable — so we do not depend on that observation:
  pinning ``agents`` (the reporter-confirmed bypass in #31864) keeps enablement
  robust to a rollout change, not just to the binary we probed. Re-run the
  delegate probe after any @openai/codex bump.
  """
  overrides = ["features.default_mode_request_user_input=true"]
  if _env_flag_on("MOEBIUS_CODEX_MULTI_AGENT", default=True):
    overrides += [
      "features.multi_agent_v2.enabled=true",
      "features.multi_agent_v2.tool_namespace=agents",
      "suppress_unstable_features_warning=true",
    ]
  return overrides


def _codex_app_server_launch_args(
  codex_bin: str | None,
  config_overrides: list[str],
) -> list[str] | None:
  """Build an app-server command isolated in its own Unix session.

  The Python SDK starts Codex with a plain ``subprocess.Popen`` and, on
  ``close()``, terminates only that one PID.  Tool commands are descendants of
  the app-server; if a shell exits while one of its children is still running,
  that child is re-parented to PID 1 and survives both the tool result and the
  SDK close.  A memory-hungry survivor can therefore outlive its chat and push
  the whole platform cgroup into OOM.

  ``setsid`` makes the app-server the leader of a private process group/session
  without requiring a change in the upstream SDK.  The terminal cleanup below
  can then signal exactly that group, never uvicorn or another concurrent chat.
  Return ``None`` outside the Linux/container runtime so local SDK resolution
  keeps its existing fallback behavior.
  """
  setsid_bin = shutil.which("setsid")
  if not codex_bin or not setsid_bin:
    return None
  args = [setsid_bin, codex_bin]
  for override in config_overrides:
    args.extend(["--config", override])
  args.extend(["app-server", "--listen", "stdio://"])
  return args


def _codex_process_group_id(codex: Any) -> int | None:
  """Return the isolated app-server PGID, or ``None`` if not provable.

  The SDK does not expose its child PID publicly.  We already target its sync
  client for the documented approval-handler slot, so keep this second private
  access in one defensive helper.  Refuse a group that is not led by the SDK
  process: on an old/non-``setsid`` launch that group can be uvicorn's own.
  """
  sync_client = getattr(getattr(codex, "_client", None), "_sync", None)
  proc = getattr(sync_client, "_proc", None)
  pid = getattr(proc, "pid", None)
  if not isinstance(pid, int) or pid <= 1:
    return None
  try:
    pgid = os.getpgid(pid)
  except (OSError, ProcessLookupError):
    return None
  if pgid != pid or pgid == os.getpgrp():
    log.error(
      "Codex app-server process group is not isolated pid=%s pgid=%s; "
      "descendant cleanup disabled",
      pid,
      pgid,
    )
    return None
  return pgid


def _terminate_codex_process_group(
  pgid: int | None,
  *,
  grace_seconds: float = 0.25,
) -> bool:
  """Terminate every process left in one completed Codex turn's group.

  Called only after the SDK context has closed its app-server PID.  SIGTERM
  gives ordinary shell/browser helpers a brief exit window; SIGKILL is the
  bounded backstop for the exact failure mode this guards (a CPU-heavy child
  that ignores or never receives its parent's termination).  Returns whether
  a live group was found.  All failures are best-effort because terminal chat
  persistence must not fail merely because the OS already reaped the group.
  """
  if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
    return False
  try:
    os.killpg(pgid, signal.SIGTERM)
  except ProcessLookupError:
    return False
  except OSError as exc:
    log.warning("Codex descendant SIGTERM failed pgid=%s: %s", pgid, exc)
    return False

  deadline = time.monotonic() + max(0.0, grace_seconds)
  while time.monotonic() < deadline:
    try:
      os.killpg(pgid, 0)
    except ProcessLookupError:
      return True
    except OSError:
      break
    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))

  try:
    os.killpg(pgid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  except OSError as exc:
    log.warning("Codex descendant SIGKILL failed pgid=%s: %s", pgid, exc)
  return True


class _BridgeError(Exception):
  """Signals the sync AskUserQuestion handler to return an error response
  to Codex rather than continuing with empty or fabricated answers.

  Module-level so test code and any future bridge paths can catch it by
  name without importing from inside a closure.
  """


class _OverlapError(_BridgeError):
  """An AskUserQuestion was submitted while one was already pending.

  The sync handler returns a JSON-RPC error so Codex fails the tool call
  rather than continuing with empty answers (B2/B5 from round-5 review).
  """


class _SteerOverlapError(_BridgeError):
  """A user steer won admission before request_user_input registered.

  The Codex SDK has one stdout reader thread. It handles server requests
  synchronously, while ``AsyncTurnHandle.steer`` waits in another worker for
  that same reader to route its response. Parking the reader on a new question
  during steer would therefore deadlock the steer acknowledgement. Rejecting
  the not-yet-published tool call lets the reader route the already-admitted
  user steer; no durable question or owner answer is discarded.
  """


async def _persist_session_id(db, chat_id: str, session_id: str | None) -> None:
  """Best-effort early persistence for provider resume continuity.

  Advances two records from the same sighting: the CURRENT-session pointer on
  the chat row (via the single-writer actor, since it lives on the hot Chat
  row), and the append-only ``chat_session_links`` map (via
  ``record_session_link_async``, which commits on its own short-lived session in
  a worker thread — never the runner's ``db``, which chat.py closes before the
  long run). This funnel runs on BOTH a fresh ``thread_start`` and a
  ``thread_resume``: the caller sets ``thread.id`` from either path before
  invoking it, so the codex thread id is recorded (and re-sighted idempotently
  on resume) with no second call site. The link record is what survives the
  provider switch / session reset that later NULLs ``Chat.session_id``. Mirrors
  ``claude_sdk_runner._persist_session_id``. Out-of-band callers such as the
  nightly Reflection runner pass ``db=None`` because their synthetic chat id
  has no durable Chat row; they must not enter these chat-only write paths.
  """
  if db is None or not chat_id or not session_id:
    return
  try:
    from app.chat_writer import PersistSessionId, await_ack, get_writer
    from app.session_links import record_session_link_async
    ack = get_writer().submit(
      PersistSessionId(chat_id=chat_id, session_id=session_id)
    )
    await await_ack(ack)
    await record_session_link_async("codex", session_id, chat_id)
  except Exception:
    log.warning(
      "Codex session id persistence failed chat_id=%s session_id=%s",
      chat_id,
      session_id,
      exc_info=True,
    )


class ActiveCodexTurn:
  """Stop + steer handle registered for SDK-backed Codex turns.

  Wraps the live `(thread, turn_handle)` pair so callers can either steer
  (via `.turn`) or interrupt (via `.interrupt()`). The interrupt method
  signals `turn.interrupt()` upstream — which per the SDK is signal-only
  (`v2_all.py:4260-4264` returns an empty `TurnInterruptResponse`) — then
  awaits `_finished`, which the runner resolves once its notification
  loop drains the resulting `TurnCompletedNotification(status=interrupted)`.
  Same shape as Claude's `ActiveClaudeClient`.
  """

  def __init__(self, thread: Any, turn: Any, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CODEX_SDK
    self.thread = thread
    self.turn = turn
    # Admission flag shared with request_user_input on the runner loop. Set
    # synchronously before turn.steer's first await so a not-yet-registered
    # question cannot park the SDK reader ahead of the steer acknowledgement.
    self._steer_in_flight = False
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

  @property
  def steer_in_flight(self) -> bool:
    return self._steer_in_flight

  async def interrupt(self) -> None:
    """Signals the live turn and waits for runner-side drain."""
    try:
      await self.turn.interrupt()
    except Exception as exc:
      log.warning("codex interrupt() raised: %s", exc)
    try:
      await asyncio.wait_for(self._finished, timeout=5.0)
    except asyncio.TimeoutError:
      log.warning(
        "codex active_turn._finished never resolved within 5s; runner is wedged"
      )
      return

  async def stop(self, timeout: float = 2.0) -> bool:
    """Interrupts the active turn and waits up to `timeout` seconds."""
    try:
      await asyncio.wait_for(self.interrupt(), timeout=timeout)
      return True
    except asyncio.CancelledError:
      raise
    except asyncio.TimeoutError:
      log.warning(
        "Codex SDK stop timed out chat_id=%s", self.chat_id,
      )
      return False
    except Exception:
      log.exception(
        "Codex SDK stop failed chat_id=%s", self.chat_id,
      )
      return False

  def mark_finished(self) -> None:
    """Resolves the stop waiter once the runner is fully drained."""
    if not self._finished.done():
      self._finished.set_result(None)


def _sdk_imports() -> dict[str, Any]:
  """Imports the SDK lazily so this module stays importable without it.

  The current upstream git refs are packaging-broken, so Möbius keeps
  the import inside the runtime path for now. Docker import verification
  can still succeed while dispatch wiring catches up to a real install.

  We intentionally import notification and item types from
  `openai_codex.generated.v2_all`, which is a private/generated path.
  The upstream stable surface does not yet expose these typed classes
  publicly. This is brittle: an SDK bump can rename or move them
  freely. TG-NEW should add a contract test that imports these symbols
  at test time so breakage is caught immediately.
  """
  from openai_codex import ApprovalMode, AsyncCodex, Sandbox
  from openai_codex.client import CodexConfig
  from openai_codex.errors import CodexRpcError, InvalidParamsError
  from openai_codex.types import ReasoningEffort, ReasoningSummary
  from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionOutputDeltaNotification,
    CommandExecutionThreadItem,
    ContextCompactedNotification,
    DynamicToolCallThreadItem,
    ErrorNotification,
    FileChangePatchUpdatedNotification,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemGuardianApprovalReviewCompletedNotification,
    ItemGuardianApprovalReviewStartedNotification,
    ItemStartedNotification,
    McpToolCallThreadItem,
    ReasoningSummaryTextDeltaNotification,
    ReasoningTextDeltaNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    WebSearchThreadItem,
  )

  # Multi-agent (collab) types exist only on multi-agent-capable SDKs (the
  # openai-codex multi_agent_v2 line). Import them defensively in their own
  # block so an SDK that predates them still boots — a missing type here must
  # not break the whole runner import. A None entry means "this SDK cannot emit
  # collab items / spawned-child thread notifications", and every dispatch
  # branch guards on non-None before its isinstance check. ThreadStartedNotification
  # rides the same block because the only stream occurrence we act on is a
  # spawned child announcing itself, which only happens once collab exists.
  # SubAgentActivityThreadItem (the sub-agent lifecycle marker Codex persists in
  # the parent thread's item history) landed natively in openai-codex
  # rust-v0.145.0-alpha.13; importing it here replaces the earlier resume-time
  # validation-error fallback that reconstructed the thread handle when the SDK
  # could not parse this variant. Its dispatch is a documented no-op (see
  # _tool_start_event / _tool_completed_events). test_codex_sdk_contract asserts
  # this symbol stays importable so a future SDK that renames/drops it fails
  # loudly instead of silently reintroducing the resume gap.
  try:
    from openai_codex.generated.v2_all import (
      CollabAgentToolCallThreadItem,
      SubAgentActivityThreadItem,
      ThreadStartedNotification,
    )
  except ImportError:
    CollabAgentToolCallThreadItem = None
    SubAgentActivityThreadItem = None
    ThreadStartedNotification = None

  return {
    "CollabAgentToolCallThreadItem": CollabAgentToolCallThreadItem,
    "SubAgentActivityThreadItem": SubAgentActivityThreadItem,
    "ThreadStartedNotification": ThreadStartedNotification,
    "AgentMessageDeltaNotification": AgentMessageDeltaNotification,
    "AgentMessageThreadItem": AgentMessageThreadItem,
    "ApprovalMode": ApprovalMode,
    "AsyncCodex": AsyncCodex,
    "CodexConfig": CodexConfig,
    "CodexRpcError": CodexRpcError,
    "CommandExecutionOutputDeltaNotification": (
      CommandExecutionOutputDeltaNotification
    ),
    "CommandExecutionThreadItem": CommandExecutionThreadItem,
    "ContextCompactedNotification": ContextCompactedNotification,
    "DynamicToolCallThreadItem": DynamicToolCallThreadItem,
    "ErrorNotification": ErrorNotification,
    "FileChangePatchUpdatedNotification": FileChangePatchUpdatedNotification,
    "FileChangeThreadItem": FileChangeThreadItem,
    "InvalidParamsError": InvalidParamsError,
    "ReasoningEffort": ReasoningEffort,
    "ReasoningSummary": ReasoningSummary,
    "Sandbox": Sandbox,
    "ItemCompletedNotification": ItemCompletedNotification,
    "ItemGuardianApprovalReviewCompletedNotification": (
      ItemGuardianApprovalReviewCompletedNotification
    ),
    "ItemGuardianApprovalReviewStartedNotification": (
      ItemGuardianApprovalReviewStartedNotification
    ),
    "ItemStartedNotification": ItemStartedNotification,
    "McpToolCallThreadItem": McpToolCallThreadItem,
    "ReasoningSummaryTextDeltaNotification": (
      ReasoningSummaryTextDeltaNotification
    ),
    "ReasoningTextDeltaNotification": ReasoningTextDeltaNotification,
    "ThreadTokenUsageUpdatedNotification": (
      ThreadTokenUsageUpdatedNotification
    ),
    "TurnCompletedNotification": TurnCompletedNotification,
    "WebSearchThreadItem": WebSearchThreadItem,
  }


def _model_dump(value: Any) -> Any:
  """Turns pydantic models into plain JSON-safe values."""
  if value is None:
    return None
  if hasattr(value, "model_dump"):
    return value.model_dump(by_alias=True, exclude_none=True, mode="json")
  return value


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
  # The invariant is that Codex collab items are ordinary tool activity because
  # the parent stream exposes no per-helper identity. RUNTIME REALITY (verified
  # live on codex 0.144.5, gpt-5.6-sol delegating a sub-task): the SDK streams
  # the collab tool ONLY as the `wait` op (a
  # CollabAgentToolCallThreadItem, unwrapped at payload.item.root), whose
  # ``receiver_thread_ids`` and ``agents_states`` are both EMPTY. The `Task`
  # vocabulary folds this generic wait into ActivityStretch as "Working in the
  # background" without falsely opening Claude's task lifecycle contract. The
  # named child and its result remain the Workflows app parser's job via the
  # forked child rollout's parent_thread_id.
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
    return {
      "type": "tool_start",
      "tool": "Edit",
      "input": path,
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
  # The invariant is that a Codex collab completion closes the ordinary tool
  # activity opened by _tool_start_event and never manufactures task_done. The
  # optional summary remains defensive for a future SDK that populates
  # agents_states; it is always absent at runtime on codex 0.144.5.
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
    events: list[dict[str, Any]] = []
    if output:
      events.append({"type": "tool_output", "content": output})
    events.append({"type": "tool_end"})
    return events

  if isinstance(item, sdk["FileChangeThreadItem"]):
    lines: list[str] = []
    for change in item.changes:
      change_dict = _model_dump(change) or {}
      kind = change_dict.get("kind", "?")
      path = change_dict.get("path", "")
      line = f"{kind} {path}".strip()
      if line:
        lines.append(line)
    events: list[dict[str, Any]] = []
    if lines:
      events.append({"type": "tool_output", "content": "\n".join(lines)})
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


def _skill_names_in_command(command: str, data_dir: str) -> list[str]:
  """Extracts Möbius skill names a shell command reads.

  Codex has no Read tool and no `can_use_tool` hook — its closest
  interception point is the command-execution item stream, where a
  skill load looks like `cat /data/shared/skills/<name>.md` (or a
  sed/head/grep over the same path). Any reference to a skill file in
  a command counts as a load; that over-counts an edit-in-place,
  which is acceptable for an aggregate most-used signal. Returns
  deduped names in first-mention order.
  """
  if not command:
    return []
  prefix = re.escape(
    os.path.normpath(os.path.join(data_dir, "shared", "skills"))
  )
  names: list[str] = []
  for match in re.finditer(prefix + r"/([A-Za-z0-9._-]+)\.md\b", command):
    name = match.group(1)
    if name not in names:
      names.append(name)
  return names


def _observe_skill_reads(
  item: Any, sdk: dict[str, Any], *, bc: Any, chat_id: str,
) -> None:
  """Fire-and-forget `skill_loaded` events for skill-file shell reads.

  Mirrors `observe_skill_file_read` in claude_sdk_runner: same wire
  event (chip), same activity record (most-used-skills aggregation).
  Never raises — observability must not break the notification loop.
  """
  try:
    if not isinstance(item, sdk["CommandExecutionThreadItem"]):
      return
    from app import activity
    from app.config import get_settings
    command = _extract_bash_command(item.command or "")
    skills = _skill_names_in_command(command, get_settings().data_dir)
    for skill in skills:
      bc.publish({"type": "skill_loaded", "skill": skill})
      activity.log_skill_load(chat_id, skill)
  except Exception:
    log.debug("codex skill_loaded observability failed", exc_info=True)


def _file_change_patch_summary(changes: list[Any]) -> str:
  """Summarizes one file-change patch update as `kind path` lines."""
  lines: list[str] = []
  for change in changes:
    change_dict = _model_dump(change) or {}
    kind = change_dict.get("kind", "?")
    path = change_dict.get("path", "")
    line = f"{kind} {path}".strip()
    if line:
      lines.append(line)
  return "\n".join(lines)


def _is_closed_turn_error(exc: BaseException) -> bool:
  """Returns True when the live turn handle is already closed/dead."""
  sdk: dict[str, Any] | None = None
  try:
    sdk = _sdk_imports()
  except ModuleNotFoundError:
    sdk = None
  if sdk is not None and isinstance(
    exc, (sdk["InvalidParamsError"], sdk["CodexRpcError"])
  ):
    text = str(exc).lower()
    return "closed" in text or "not running" in text or "broken pipe" in text
  if exc.__class__.__name__ == "TransportClosedError":
    return True
  if isinstance(exc, RuntimeError):
    text = str(exc).lower()
    return "closed" in text or "not running" in text or "broken pipe" in text
  return False


# JSON-RPC method the Codex app-server sends when the model invokes the
# `request_user_input` tool. Confirmed via probe3.py (the abbreviated
# `tool/requestUserInput` string in the upstream README is incomplete —
# the wire prepends `item/`). If a future SDK bump renames this, the
# AskUserQuestion bridge will silently regress (model's question will
# fall through to the default approval handler, which returns `{}` and
# the turn will likely fail). Lock this in with a contract test against
# the installed SDK's surface.
_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"


def _install_request_user_input_handler(
  codex: Any,
  *,
  loop: asyncio.AbstractEventLoop,
  chat_id: str,
  bc: Any,
  pending_questions: dict,
  db: Any,
) -> None:
  """Wires Möbius's question-bridge into `codex._client._sync._approval_handler`.

  As of openai-codex 0.142.5, `CodexClient.approval_handler` is a
  documented constructor argument on the sync client. Neither
  `AsyncCodex` nor `AsyncCodexClient` accept it in their
  constructors, so we still set the attribute on the underlying sync
  client after construction, targeting the same callable slot the
  public constructor argument populates. See the module docstring for
  the full reasoning on why we don't drop `AsyncCodex` and construct
  `CodexClient` directly.

  The handler runs on the SDK's sync worker thread, so anything that
  touches asyncio state (the future, the broadcast, the DB session)
  must be marshaled onto the runner's loop via
  `asyncio.run_coroutine_threadsafe`.

  For non-`request_user_input` approval methods, defers to the SDK's
  default handler (auto-accept commandExecution / fileChange), which
  preserves the trust-the-agent posture documented in CLAUDE.md.
  """
  from app.pending_questions import PendingQuestion
  from uuid import uuid4

  # _BridgeError / _OverlapError are module-level classes (see top of file).
  # `park_question` raises them to signal the sync handler to return a
  # JSON-RPC error to Codex rather than continuing with empty answers
  # (B2/B5 from round-5 review).

  async def park_question(questions_payload: list[dict]) -> dict:
    """Asyncio side of the bridge — creates the future, publishes the
    `question` event, waits for the answer, returns it keyed by
    question id (Codex's native shape). Raises on overlap / cancel /
    notify_cb failure so the sync handler can surface a real error
    to Codex (B2, B4, B5 from round-5 review).
    """
    # Admission is atomic on the runner loop: steer_into_active_turn marks the
    # ActiveCodexTurn before its first await, and this coroutine also runs on
    # that loop. If the user steer won, fail this not-yet-persisted tool call
    # instead of parking the SDK's sole reader thread before it can route the
    # steer response. A question that registered first is protected by the
    # route's questions.is_waiting gate and remains answerable.
    active = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
    if isinstance(active, ActiveCodexTurn) and active.steer_in_flight:
      raise _SteerOverlapError(
        "Question superseded by steering input; continue with the new input."
      )

    # Refuse to overlap with an existing pending question on the same
    # chat. Returning empty here would silently swallow the second
    # question — instead raise so the sync handler can fail the tool
    # call with a real error response (Codex won't continue with bad
    # data).
    existing = pending_questions.get(chat_id)
    if existing is not None and not existing.future.done():
      raise _OverlapError(
        f"AskUserQuestion already pending for chat {chat_id}"
      )
    future = asyncio.get_running_loop().create_future()
    pending = PendingQuestion(
      question_id=str(uuid4()),
      questions=questions_payload,
      future=future,
      # The turn's persistence run token (the sink carries it). The
      # answer route submits AnswerQuestion keyed on this so the writer
      # actor fences the right (chat_id, run_token) snapshot before
      # merging the answer; None for a sink/broadcast without one →
      # broad-fence by chat.
      run_token=getattr(bc, "run_token", None),
    )
    pending_questions[chat_id] = pending

    try:
      # Save-before-broadcast (Candidate B): the card's question_id MUST
      # persist before the SSE event shows it. `publish_question` submits
      # a QuestionCommit, awaits its ack, then broadcasts — and RAISES if
      # the commit didn't land. A failed commit is surfaced as a
      # _BridgeError so the sync handler returns a real error to Codex
      # (no unpersisted card on the wire, no fallback direct write).
      #
      # Push notification on AskUserQuestion is AGENT-DRIVEN: the
      # skill/seed tells the agent to `curl POST /api/notifications/send`
      # itself, so it sees push success/failure (bash output) and decides
      # per-question whether to buzz the user. The cb arg is kept in the
      # signature for compatibility but not invoked here.
      try:
        await bc.publish_question({
          "type": "question",
          "question_id": pending.question_id,
          "questions": questions_payload,
        })
      except Exception as exc:
        log.error(
          "AskUserQuestion save-before-broadcast failed chat_id=%s: %s",
          chat_id, exc,
        )
        raise _BridgeError("could not save the question")

      try:
        answers = await future
      except (asyncio.CancelledError, _cf.CancelledError):
        # Cancellation can arrive as either flavor depending on whether
        # the cancel came from the asyncio loop side (asyncio) or from
        # the SDK worker thread side via fut.cancel() (concurrent).
        # Propagate so the sync handler returns the cancel error.
        raise _BridgeError("cancelled")
      return answers or {}
    finally:
      # B1/B4: always clean up the registry entry, no matter how the
      # park exited (success, raise, cancel). Without this a failed
      # park leaves the entry pinned and the next question on the
      # same chat hits the overlap guard immediately.
      if pending_questions.get(chat_id) is pending:
        pending_questions.pop(chat_id, None)

  def handler(method: str, params: dict | None) -> dict:
    if method != _REQUEST_USER_INPUT_METHOD:
      # Replicate the SDK's default auto-accept for the two known
      # approval methods (mirrors CodexClient._default_approval_handler
      # in openai_codex.client). We inline rather than delegate so the
      # bridge stays decoupled from the SDK's internal layout — a
      # future SDK rename won't break this fallback.
      if method == "item/commandExecution/requestApproval":
        return {"decision": "accept"}
      if method == "item/fileChange/requestApproval":
        return {"decision": "accept"}
      return {}

    # Strict payload validation: a malformed requestUserInput payload
    # (missing `questions` key, wrong type, items lacking `id`/`text`)
    # used to silently return empty answers. Silent acceptance hides a
    # real SDK shape change behind a model that keeps fabricating
    # answers. Surface it as a tool-call error so Codex aborts instead.
    if not isinstance(params, dict):
      err = (
        "invalid requestUserInput payload: expected object params, "
        f"got {type(params).__name__}"
      )
      log.error("Codex bridge: %s chat_id=%s", err, chat_id)
      return {"error": {"message": err}}
    if "questions" not in params:
      err = "invalid requestUserInput payload: missing 'questions' key"
      log.error("Codex bridge: %s chat_id=%s", err, chat_id)
      return {"error": {"message": err}}
    questions = params.get("questions")
    if not isinstance(questions, list):
      err = (
        "invalid requestUserInput payload: 'questions' must be a list, "
        f"got {type(questions).__name__}"
      )
      log.error("Codex bridge: %s chat_id=%s", err, chat_id)
      return {"error": {"message": err}}
    if not questions:
      # Empty list (vs missing key): accept and proceed.
      return {"answers": {}}
    for idx, q in enumerate(questions):
      if not isinstance(q, dict):
        err = (
          f"invalid requestUserInput payload: questions[{idx}] not an "
          f"object, got {type(q).__name__}"
        )
        log.error("Codex bridge: %s chat_id=%s", err, chat_id)
        return {"error": {"message": err}}
      if not q.get("id"):
        err = (
          f"invalid requestUserInput payload: questions[{idx}] missing "
          f"'id'"
        )
        log.error("Codex bridge: %s chat_id=%s", err, chat_id)
        return {"error": {"message": err}}
      # The wire field for the question text varies (`question` on the
      # public schema, `text` in some upstream samples); accept either.
      if not (q.get("question") or q.get("text") or q.get("header")):
        err = (
          f"invalid requestUserInput payload: questions[{idx}] missing "
          f"'question'/'text'/'header'"
        )
        log.error("Codex bridge: %s chat_id=%s", err, chat_id)
        return {"error": {"message": err}}

    # Bridge from sync (this thread) to async (runner loop). It deliberately
    # has no user-answer timeout: an AskUserQuestion is a human pause point, and
    # only an answer, Stop/cancel, or a real bridge failure should resolve it.
    # On failures, translate to a JSON-RPC-shaped error so Codex actually fails
    # the tool call instead of continuing with empty answers (B2, B5).
    try:
      fut = asyncio.run_coroutine_threadsafe(
        park_question(questions), loop,
      )
    except RuntimeError as exc:
      # Loop closed between handler install and invocation.
      log.error(
        "Codex bridge: asyncio loop unavailable for chat_id=%s: %s",
        chat_id, exc,
      )
      return {"error": {"message": "Möbius bridge unavailable."}}

    try:
      text_keyed = fut.result()
    except _SteerOverlapError as exc:
      log.info(
        "Codex bridge: question lost steer admission race chat_id=%s", chat_id,
      )
      return {"error": {"message": str(exc)}}
    except _OverlapError as exc:
      log.warning(
        "Codex bridge: overlap rejected chat_id=%s: %s", chat_id, exc,
      )
      return {"error": {"message": str(exc)}}
    except (asyncio.CancelledError, _cf.CancelledError):
      log.info("Codex bridge: cancelled chat_id=%s", chat_id)
      return {"error": {"message": "Interrupted by Stop."}}
    except Exception as exc:
      log.exception(
        "Codex request_user_input bridge failed chat_id=%s: %s",
        chat_id, exc,
      )
      return {"error": {"message": str(exc)}}

    # B3: walk questions by INDEX and map by id, falling back to text
    # match. The previous text-only map silently collided when two
    # questions shared text or when q.question was empty. Möbius's UI
    # currently POSTs `{question_text: label}`; we look up by both
    # the canonical text (`question` field) and the header so the
    # bridge survives UI changes that prefer one or the other.
    answers_by_qid: dict[str, dict] = {}
    for q in questions:
      qid = q.get("id")
      if not qid:
        continue  # malformed question, skip
      label = None
      for key in (q.get("question"), q.get("header"), q.get("id")):
        if key and key in text_keyed:
          label = text_keyed[key]
          break
      if label is None:
        continue
      # Schema expects `answers: list[str]` — Möbius UI is
      # single-choice today so we wrap the single label in a list.
      answers_by_qid[qid] = {
        "answers": [label] if isinstance(label, str) else list(label)
      }
    return {"answers": answers_by_qid}

  # Reach the sync client. AsyncCodex._client is AsyncCodexClient;
  # AsyncCodexClient._sync is CodexClient — the level that
  # owns the public `approval_handler` constructor argument. Test
  # fakes legitimately lack the chain entirely; only real
  # openai-codex installs reach the strict check.
  sync_client = None
  try:
    sync_client = codex._client._sync
  except AttributeError:
    log.warning(
      "Codex SDK has no _client._sync chain — request_user_input "
      "bridge NOT installed for chat_id=%s (likely a unit-test fake).",
      chat_id,
    )
    return
  if not hasattr(sync_client, "_approval_handler"):
    # Real openai-codex client without the expected attribute means
    # the SDK was refactored — silently no-opping would make the
    # model's AskUserQuestion calls vanish. Fail loudly so the
    # operator pins a known-good version instead of debugging silent
    # question loss.
    raise RuntimeError(
      "openai-codex API broken: CodexClient._approval_handler "
      "missing — pin a known-good version"
    )
  sync_client._approval_handler = handler
  log.debug(
    "Codex request_user_input bridge installed chat_id=%s", chat_id,
  )


async def run_codex_sdk_turn(
  user_message: str,
  session_id: str | None,
  base_env: dict[str, str],
  cwd: str,
  chat_id: str,
  bc,
  pending_questions: dict,
  db,
  agent_settings: dict | None = None,
  system_prompt: str | None = None,
  should_abort: Callable[[], bool] | None = None,
) -> RunnerResult:
  """Runs one Codex SDK turn and publishes Möbius-shaped events.

  Args:
    user_message: Fully prepared user prompt for this turn.
    session_id: Existing Codex thread id, or None for a new thread.
    base_env: Environment for the SDK app-server process.
    cwd: Working directory for the Codex thread.
    chat_id: Möbius chat identifier for registries.
    bc: Chat broadcast used for `bc.publish(event)`.
    pending_questions: Shared AskUserQuestion registry owned by
      chat.py — keyed by chat_id. Used by the request_user_input
      bridge to park on a future while the user answers.
    db: SQLAlchemy session for durable-chat persistence paths, or None for an
      out-of-band turn with no Chat row (for example nightly Reflection).

  Returns:
    Dict with `session_id`, `cost_usd`, and `error`.
  """
  sdk = _sdk_imports()
  # chat.py always pre-merges the per-chat overrides on top of the
  # global file defaults; treat a missing dict as empty rather than
  # re-reading the file here. Standalone callers (tests) pass `{}`.
  if agent_settings is None:
    agent_settings = {}
  # The per-chat picker writes the `model` key.
  model = agent_settings.get("model")
  # Cross-provider mismatch defense. Chats persisted before the
  # snapshot logic learned to provider-validate (see chat.py
  # snapshot-on-first-send and effective_agent_settings) can end up
  # with a Claude model on a Codex chat (the global default file
  # remembered the last Claude pick when a fresh Codex chat was
  # created). Sending that to Codex 400s every turn with "model
  # not supported". Quietly normalize to the Codex default so
  # existing chats keep working; the user can re-pick in the
  # picker if they want a specific Codex model.
  from app.providers import _model_belongs_to_other_provider, DEFAULT_MODELS
  if model and _model_belongs_to_other_provider(model, "codex"):
    log.warning(
      "codex turn started with non-codex model %r — normalizing to %r",
      model, DEFAULT_MODELS["codex"],
    )
    model = DEFAULT_MODELS["codex"]

  # Reasoning effort — Codex's `ReasoningEffort` enum accepts
  # none/minimal/low/medium/high/xhigh; the Möbius picker exposes the
  # last four. Pass through the string and let the SDK convert; if the
  # value is unknown (e.g. a future picker addition the SDK doesn't
  # yet accept), surface the SDK's error rather than silently dropping
  # the choice.
  effort_str = agent_settings.get("effort")
  effort = None
  if effort_str:
    try:
      effort = sdk["ReasoningEffort"](effort_str)
    except (ValueError, KeyError):
      log.warning(
        "Codex: unknown effort %r — passing turn without effort override",
        effort_str,
      )

  reasoning_summary = _reasoning_summary_setting(sdk)

  base_instructions: str | None = None
  if session_id is None:
    if system_prompt is not None:
      base_instructions = system_prompt
    else:
      skill = get_skill_path()
      if skill is not None:
        try:
          base_instructions = skill.read_text(encoding="utf-8")
        except OSError:
          base_instructions = None

  env = dict(base_env)
  env.setdefault("CODEX_HOME", "/data/cli-auth/codex")

  # config_overrides carries the request_user_input (AskUserQuestion parity) and
  # multi-agent enablement flags — assembled, with the #31864 tool_namespace pin
  # and the MOEBIUS_CODEX_MULTI_AGENT kill switch, in _codex_config_overrides().
  codex_bin = shutil.which("codex")
  config_overrides = _codex_config_overrides()
  launch_args = _codex_app_server_launch_args(codex_bin, config_overrides)
  config_kwargs: dict[str, Any] = dict(
    codex_bin=codex_bin,
    cwd=cwd,
    env=env,
    config_overrides=config_overrides,
  )
  if launch_args is not None:
    config_kwargs["launch_args_override"] = launch_args
  else:
    log.warning(
      "Codex app-server process-group isolation unavailable; "
      "descendant cleanup is best-effort only"
    )
  config = sdk["CodexConfig"](**config_kwargs)

  thread = None
  turn = None
  current_session_id = session_id
  completed_turn: Any | None = None
  process_group_id: int | None = None

  def abort_requested() -> bool:
    return bool(should_abort and should_abort())

  def aborted_result() -> RunnerResult:
    return {
      "session_id": current_session_id,
      "cost_usd": None,
      "error": None,
    }

  try:
    async with sdk["AsyncCodex"](config=config) as codex:
      process_group_id = _codex_process_group_id(codex)
      # Install AskUserQuestion bridge on the sync CodexClient's
      # approval_handler attribute. `approval_handler` is a public
      # sync-client constructor argument as of openai-codex 0.142.5;
      # neither AsyncCodex nor AsyncCodexClient accept it, so we
      # set it on `codex._client._sync` after construction. Staying
      # on AsyncCodex (instead of dropping to CodexClient to pass
      # the kwarg natively) keeps ~100 lines of SDK glue out of this
      # module. See the module docstring for the full reasoning.
      # When the model calls the `request_user_input` tool (enabled by
      # the features.default_mode_request_user_input config_override
      # above), the app-server sends an `item/tool/requestUserInput`
      # JSON-RPC request to our handler; we park on the shared
      # `_pending_questions` future (same registry Claude uses), publish
      # a `question` event to the SSE wire (same UI), and translate the
      # user's answer back into the Codex response shape. For other
      # approval methods (commandExecution / fileChange), defer to the
      # SDK's default auto-accept behavior so we keep our trust-the-agent
      # posture.
      #
      # Threading note: approval_handler runs on the SDK's sync JSON-RPC
      # worker thread, NOT this asyncio loop. We use
      # asyncio.run_coroutine_threadsafe to bridge into the loop where
      # the pending-question future lives, and block the worker on the
      # resulting concurrent.futures.Future. That keeps the JSON-RPC
      # round-trip blocked (correct — the app-server is waiting for our
      # response) while letting asyncio handle the user's answer POST.
      _install_request_user_input_handler(
        codex,
        loop=asyncio.get_running_loop(),
        chat_id=chat_id,
        bc=bc,
        pending_questions=pending_questions,
        db=db,
      )

      # We use the SDK's `ApprovalMode.auto_review`, which maps to
      # `approvalPolicy=on_request` with `approvalsReviewer=auto_review`
      # (rather than an unconditional `approvalPolicy=never`). That may
      # still surface a human approval prompt in some cases; see
      # `.pm/features/_003-tech-debt-and-test-gaps.md` OQ-5 for the
      # required live equivalence check.

      # Sandbox.full_access maps to wire SandboxMode.danger_full_access
      # and disables bwrap. Möbius runs
      # inside a Docker container where the default bwrap-based
      # workspace_write sandbox fails with `bwrap: No permissions to
      # create a new namespace, likely because the kernel does not
      # allow non-privileged user namespaces` (the docker default
      # seccomp profile blocks CLONE_NEWUSER even when the host
      # allows it). That blocked every tool that spawned a
      # sub-process — including the Read tool reading PNGs, which
      # silently broke the agent's ability to verify its own
      # screenshots. Full access here follows the same reasoning, and
      # Möbius's design philosophy
      # ("trust the agent; container is the sandbox") is consistent.
      _sandbox = sdk["Sandbox"].full_access
      if session_id is None:
        thread = await codex.thread_start(
          approval_mode=sdk["ApprovalMode"].auto_review,
          sandbox=_sandbox,
          base_instructions=base_instructions,
          cwd=cwd,
          model=model,
        )
      else:
        # Resume parses the thread's persisted history, which can include
        # subAgentActivity items. The SDK's generated ThreadItem union models
        # that variant natively (openai-codex rust-v0.145.0-alpha.13+), so
        # thread_resume no longer raises the validation error the old
        # _resume_codex_thread wrapper caught and worked around. Möbius uses only
        # the returned handle's id + turn() (it never re-renders resumed
        # history), so a native parse is a straight pass-through here.
        thread = await codex.thread_resume(
          session_id,
          approval_mode=sdk["ApprovalMode"].auto_review,
          sandbox=_sandbox,
          cwd=cwd,
          model=model,
        )

      current_session_id = thread.id
      if abort_requested():
        log.info("Codex turn aborted before turn setup chat_id=%s", chat_id)
        return aborted_result()
      if session_id is not None and current_session_id != session_id:
        error_text = (
          "Codex resume returned a different session id "
          f"({current_session_id}) than requested ({session_id}); "
          "start a fresh chat turn."
        )
        log.warning(
          "Codex stale resume detected for chat %s: requested=%s actual=%s",
          chat_id,
          session_id,
          current_session_id,
        )
        bc.publish({"type": "error", "message": error_text})
        return {
          "session_id": current_session_id,
          "cost_usd": None,
          "error": error_text,
        }
      bc.publish({
        "type": "session_init",
        "session_id": current_session_id,
      })

      turn = await thread.turn(
        user_message,
        cwd=cwd,
        model=model,
        effort=effort,
        summary=reasoning_summary,
      )
      if abort_requested():
        try:
          await turn.interrupt()
        except Exception:
          log.warning(
            "Codex stale turn interrupt failed chat_id=%s",
            chat_id,
            exc_info=True,
          )
        log.info("Codex turn aborted before stream registration chat_id=%s", chat_id)
        return aborted_result()
      active_turn = ActiveCodexTurn(thread, turn, chat_id=chat_id)
      registry.register(active_turn)

      # Persist the session id AFTER registering the live turn: this is a
      # best-effort write (the actor persist + the append-only session-link
      # record), and Stop/steer reachability must never wait on it — mirrors the
      # Claude runner, which also registers before persisting. It runs after the
      # stale-resume check above so a rejected (mismatched) session is never
      # recorded.
      await _persist_session_id(db, chat_id, current_session_id)

      async for notification in turn.stream():
        payload = notification.payload

        if isinstance(payload, sdk["AgentMessageDeltaNotification"]):
          if payload.delta:
            event = {"type": "text", "content": payload.delta}
            item_id = getattr(payload, "item_id", None)
            if item_id:
              event["text_item_id"] = item_id
            bc.publish(event)
          continue

        # Reasoning deltas are Codex's analog of Claude's thinking_delta:
        # both publish the same `thinking` event so the provider-agnostic
        # frontend renders the collapsed "Thinking…" trace either way.
        # Codex emits one of two visible reasoning delta streams depending on
        # SDK/app-server version and summary config: item/reasoning/textDelta
        # or item/reasoning/summaryTextDelta. We request `auto` summaries for
        # the richest public API surface, but handle both SDK event names so a
        # version bump does not silently drop the trace.
        if isinstance(
          payload,
          (
            sdk["ReasoningTextDeltaNotification"],
            sdk["ReasoningSummaryTextDeltaNotification"],
          ),
        ):
          if payload.delta:
            bc.publish(_thinking_event(
              payload.delta,
              _codex_thinking_segment_id(payload),
            ))
          continue

        if isinstance(
          payload,
          sdk["CommandExecutionOutputDeltaNotification"],
        ):
          if payload.delta:
            event = {"type": "tool_output", "content": payload.delta}
            _stamp_notification_item_id(event, payload)
            bc.publish(event)
          continue

        if isinstance(payload, sdk["ItemStartedNotification"]):
          item = payload.item.root if hasattr(payload.item, "root") else payload.item
          if isinstance(item, sdk["AgentMessageThreadItem"]):
            bc.publish({"type": "text_boundary"})
            continue
          event = _tool_start_event(item, sdk)
          if event is not None:
            _stamp_tool_use_id(event, item)
            bc.publish(event)
          _observe_skill_reads(item, sdk, bc=bc, chat_id=chat_id)
          # A spawn's child thread ids first appear on its collab item; record
          # the session->chat link now so the child rollout stays attributed
          # even if we never resume it directly.
          await _record_collab_child_links(item, sdk, chat_id=chat_id)
          continue

        if isinstance(payload, sdk["FileChangePatchUpdatedNotification"]):
          summary = _file_change_patch_summary(payload.changes)
          if summary:
            event = {"type": "tool_output", "content": summary}
            _stamp_notification_item_id(event, payload)
            bc.publish(event)
          continue

        if isinstance(payload, sdk["ItemCompletedNotification"]):
          item = payload.item.root if hasattr(payload.item, "root") else payload.item
          for event in _tool_completed_events(item, sdk):
            _stamp_tool_use_id(event, item)
            bc.publish(event)
          # Also record child links here (idempotent) in case receiver_thread_ids
          # only populates on completion — a missed link silently loses the
          # attribution this recording exists to provide.
          await _record_collab_child_links(item, sdk, chat_id=chat_id)
          continue

        if isinstance(payload, sdk["ThreadTokenUsageUpdatedNotification"]):
          # Token usage is reported but currently not surfaced; the
          # SDK already exposes it via the thread handle for any
          # consumer that needs it.
          continue

        if isinstance(
          payload,
          sdk["ItemGuardianApprovalReviewStartedNotification"],
        ):
          continue

        if isinstance(
          payload,
          sdk["ItemGuardianApprovalReviewCompletedNotification"],
        ):
          continue

        if isinstance(payload, sdk["ContextCompactedNotification"]):
          log.info("Codex context compacted for chat %s", chat_id)
          continue

        if sdk.get("ThreadStartedNotification") is not None and isinstance(
          payload, sdk["ThreadStartedNotification"]
        ):
          # A spawned sub-agent announces its own thread on the parent turn's
          # stream. The invariant is that this notification stays silent because
          # emitting session_init would repoint the chat at the child thread.
          # Generic live work is already represented by the ordinary collab tool
          # activity, while the Workflows parser attributes the named child via
          # parent_thread_id.
          continue

        if isinstance(payload, sdk["TurnCompletedNotification"]):
          completed_turn = payload.turn
          break

        if (
          notification.method == "error"
          and isinstance(payload, sdk["ErrorNotification"])
        ):
          message = getattr(payload.error, "message", None)
          if getattr(payload, "will_retry", False):
            log.warning(
              "Codex turn error will retry for chat %s: %s",
              chat_id,
              message or "Codex error",
            )
            continue
          raise RuntimeError(str(message or "Codex error"))

      error_text = None
      # The installed SDK routes notifications by `turnId` and drops late
      # `turn/completed` events once the queue is unregistered, so normal
      # turn streams are expected to terminate with TurnCompleted.
      if completed_turn is not None and completed_turn.error is not None:
        error_text = getattr(completed_turn.error, "message", None)
      return {
        "session_id": current_session_id,
        "cost_usd": None,
        "error": error_text,
      }
  except Exception as exc:
    return {
      "session_id": current_session_id,
      "cost_usd": None,
      "error": str(exc),
    }
  finally:
    current = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
    if isinstance(current, ActiveCodexTurn) and current.turn is turn:
      registry.unregister(chat_id, RunnerKind.CODEX_SDK)
      current.mark_finished()
    # AsyncCodex.close() terminates only its direct Popen PID.  Reap the
    # isolated group after that context exit so a tool child cannot be
    # re-parented to container init and consume CPU/RAM after the turn.  A
    # worker keeps the short grace period off the FastAPI event loop; shield
    # ensures task cancellation cannot prevent the SIGKILL backstop from
    # running in that worker once cleanup has started.
    if process_group_id is not None:
      await asyncio.shield(asyncio.to_thread(
        _terminate_codex_process_group,
        process_group_id,
      ))


async def steer_into_active_turn(
  chat_id: str,
  message: str,
) -> bool:
  """Delivers a message into the active Codex turn via `steer()`.

  Args:
    chat_id: Möbius chat identifier to look up in the registry.
    message: Text to inject into the in-flight turn.

  Returns:
    True when the turn existed and accepted the steering input.
  """
  current = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  if not isinstance(current, ActiveCodexTurn) or current.turn is None:
    return False
  if current.steer_in_flight:
    return False

  # The pinned SDK's async steer is asyncio.to_thread(sync.turn_steer). The
  # sync request waits for the sole reader thread to route its response; that
  # same reader invokes request_user_input handlers synchronously. Mark
  # admission BEFORE the first await so park_question can reject the losing
  # side of that race. Do not impose a route-side timeout: cancelling
  # asyncio.to_thread cannot cancel its underlying JSON-RPC request, so a late
  # success would be indistinguishable from failure and could duplicate the
  # still-durable pending row when it later drains.
  current._steer_in_flight = True

  try:
    await current.turn.steer(message)
  except (AttributeError, TypeError):
    return False
  except Exception as exc:
    if _is_closed_turn_error(exc):
      if registry.get_handle(chat_id, RunnerKind.CODEX_SDK) is current:
        registry.unregister(chat_id, RunnerKind.CODEX_SDK)
        current.mark_finished()
      return False
    raise
  finally:
    current._steer_in_flight = False
  return True
