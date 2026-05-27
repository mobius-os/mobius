"""Codex SDK turn runner for Möbius.

Codex's `TurnHandle.steer()` is the product win that motivated this
module. Unlike Claude's serial SDK flow, Codex exposes a live turn
handle that can accept in-band user steering while the current turn is
still running. Möbius can keep its existing queued-message behavior for
Claude while upgrading Codex chats to true mid-turn injection.

This module runs one Codex SDK turn, translates streamed SDK
notifications into Möbius broadcast events, relies on the SDK's
default auto-approval behavior, and stores the live `ActiveCodexTurn`
in `active_sessions[chat_id]` so Stop and queued-message steering can
reach it.

**AskUserQuestion parity is shipped via the `request_user_input`
tool.** The underlying wire surface is `item/tool/requestUserInput`
JSON-RPC requests emitted by the app-server when the model calls
the tool. `AppServerClient.approval_handler` is the documented
constructor argument that receives them (public as of
openai-codex 0.134.0; was a private attribute on a less-stable
path before). The high-level `AsyncCodex` / `AsyncAppServerClient`
wrappers do not forward `approval_handler` to the underlying sync
client, so we set the attribute on `codex._client._sync` directly
after construction. See `_install_request_user_input_handler`
below — the attribute itself is part of the public API surface, we
just have to reach through the async wrappers to get at it.

The tool is gated by the `default_mode_request_user_input`
feature flag (stage `UnderDevelopment`, default off), enabled via
`features.default_mode_request_user_input=true` in the
`AppServerConfig.config_overrides` list. Once enabled, the model
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
import shutil
from typing import Any

from app.codex_appserver import _extract_bash_command
from app.providers import get_skill_path
from app.runtime_types import RunnerResult
from app.runner_registry import RunnerKind, registry

log = logging.getLogger("moebius.chat")


class ActiveCodexTurn:
  """Stop + steer handle stored in `chat._active_sessions` for Codex turns.

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
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

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
  from openai_codex import ApprovalMode, AsyncCodex
  from openai_codex.client import AppServerConfig
  from openai_codex.errors import AppServerRpcError, InvalidParamsError
  from openai_codex.types import ReasoningEffort, SandboxMode
  from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
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
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    WebSearchThreadItem,
  )

  return {
    "AgentMessageDeltaNotification": AgentMessageDeltaNotification,
    "ApprovalMode": ApprovalMode,
    "AppServerConfig": AppServerConfig,
    "AsyncCodex": AsyncCodex,
    "AppServerRpcError": AppServerRpcError,
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
    "SandboxMode": SandboxMode,
    "ItemCompletedNotification": ItemCompletedNotification,
    "ItemGuardianApprovalReviewCompletedNotification": (
      ItemGuardianApprovalReviewCompletedNotification
    ),
    "ItemGuardianApprovalReviewStartedNotification": (
      ItemGuardianApprovalReviewStartedNotification
    ),
    "ItemStartedNotification": ItemStartedNotification,
    "McpToolCallThreadItem": McpToolCallThreadItem,
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


def _tool_start_event(item: Any, sdk: dict[str, Any]) -> dict[str, Any] | None:
  """Builds one Möbius `tool_start` event from a typed item."""
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
    return [{"type": "tool_end"}]

  return []


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
    exc, (sdk["InvalidParamsError"], sdk["AppServerRpcError"])
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

  As of openai-codex 0.134.0, `AppServerClient.approval_handler` is a
  documented constructor argument. The high-level `AsyncCodex` /
  `AsyncAppServerClient` wrappers don't forward it, so we still set
  the attribute on the underlying sync client after construction —
  but the attribute itself is now part of the public surface, not a
  private internal we're sneaking past.

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

  # Sentinel return values the sync handler interprets to surface the
  # right error to Codex. Returning a literal `{}` would let Codex
  # continue the turn with empty answers, which silently drops the
  # interaction (B2/B5 from round-5 review). Instead we raise
  # `_OverlapError` / `_TimeoutError` from `park_question`, catch them
  # in `handler`, and translate to a JSON-RPC-shaped failure response.
  class _BridgeError(Exception):
    """Signals the sync handler to return an error response to Codex."""

  class _OverlapError(_BridgeError):
    pass

  async def park_question(questions_payload: list[dict]) -> dict:
    """Asyncio side of the bridge — creates the future, publishes the
    `question` event, waits for the answer, returns it keyed by
    question id (Codex's native shape). Raises on overlap / cancel /
    notify_cb failure so the sync handler can surface a real error
    to Codex (B2, B4, B5 from round-5 review).
    """
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
    )
    pending_questions[chat_id] = pending

    try:
      bc.publish({"type": "question", "questions": questions_payload})
      # Push notification on AskUserQuestion is AGENT-DRIVEN now: the
      # skill/seed tells the agent to `curl POST /api/notifications/send`
      # itself when it asks a question. That gives the agent direct
      # visibility into push success/failure (via bash tool output)
      # and lets it decide per-question whether to buzz the user.
      # The cb arg is kept in the signature for compatibility but
      # not invoked here.

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
      # approval methods (mirrors AppServerClient._default_approval_handler
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

    # Bridge from sync (this thread) to async (runner loop). On any
    # failure (timeout, overlap, asyncio loop closed), translate to a
    # JSON-RPC-shaped error so Codex actually fails the tool call
    # instead of continuing with empty answers (B2, B5).
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
      text_keyed = fut.result(timeout=600.0)  # 10 min cap on user
    except _OverlapError as exc:
      log.warning(
        "Codex bridge: overlap rejected chat_id=%s: %s", chat_id, exc,
      )
      return {"error": {"message": str(exc)}}
    except TimeoutError:
      # The user didn't answer in 10 minutes. Cancel the parked
      # future so the runner-side coroutine can clean up, and return
      # an error so Codex aborts the tool call instead of continuing
      # with fabricated empty answers (B5).
      fut.cancel()
      log.warning(
        "Codex bridge: user did not answer within 10 minutes "
        "chat_id=%s", chat_id,
      )
      return {"error": {"message": "User did not answer in time."}}
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

  # Reach the sync client. AsyncCodex._client is AsyncAppServerClient;
  # AsyncAppServerClient._sync is AppServerClient — the level that
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
      "openai-codex API broken: AppServerClient._approval_handler "
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
    db: SQLAlchemy session for runner-side persistence paths.

  Returns:
    Dict with `session_id`, `cost_usd`, and `error`.
  """
  sdk = _sdk_imports()
  # chat.py always pre-merges the per-chat overrides on top of the
  # global file defaults; treat a missing dict as empty rather than
  # re-reading the file here. Standalone callers (tests) pass `{}`.
  if agent_settings is None:
    agent_settings = {}
  # Per-chat picker writes the `model` key; the legacy file format
  # uses `codex_model`. Honor both so existing setups don't regress.
  model = agent_settings.get("model") or agent_settings.get("codex_model")
  # Cross-provider mismatch defense. Chats persisted before
  # initial_chat_defaults learned to provider-validate the snapshot
  # can end up with a Claude model on a Codex chat (the global
  # default file remembered the last Claude pick when a fresh Codex
  # chat was created). Sending that to Codex 400s every turn with
  # "model not supported". Quietly normalize to the Codex default
  # so existing chats keep working; the user can re-pick in the
  # picker if they want a specific Codex model.
  from app.providers import _model_belongs_to_other_provider, DEFAULT_MODELS
  if model and _model_belongs_to_other_provider(model, "codex"):
    log.warning(
      "codex turn started with non-codex model %r — normalizing to %r",
      model, DEFAULT_MODELS["codex"],
    )
    model = DEFAULT_MODELS["codex"]
  # `gpt-5.4-codex` requires API-key auth (not ChatGPT). Picker no
  # longer surfaces it, but legacy chats may still carry it.
  if model == "gpt-5.4-codex":
    model = "gpt-5.4"

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

  base_instructions: str | None = None
  if session_id is None:
    skill = get_skill_path()
    if skill is not None:
      try:
        base_instructions = skill.read_text(encoding="utf-8")
      except OSError:
        base_instructions = None

  env = dict(base_env)
  env.setdefault("CODEX_HOME", "/data/cli-auth/codex")

  config = sdk["AppServerConfig"](
    codex_bin=shutil.which("codex"),
    cwd=cwd,
    env=env,
    # Enable the experimental `request_user_input` tool in Default
    # collaboration mode. Without this override the tool isn't even
    # in the model's available tool list — the spike (probe3.py)
    # confirmed it surfaces immediately once the flag is on. The
    # TOML key is `[features].default_mode_request_user_input` per
    # codex-rs/features/src/lib.rs. Stage is `UnderDevelopment` so
    # this flag may rename or move in a future SDK bump; if turns
    # silently stop emitting `item/tool/requestUserInput` after an
    # upgrade, check the upstream features list first.
    config_overrides=[
      "features.default_mode_request_user_input=true",
    ],
  )

  thread = None
  turn = None
  current_session_id = session_id
  completed_turn: Any | None = None

  try:
    async with sdk["AsyncCodex"](config=config) as codex:
      # Install AskUserQuestion bridge on the sync AppServerClient's
      # approval_handler attribute. `approval_handler` is a public
      # constructor argument as of openai-codex 0.134.0, but the
      # higher-level AsyncCodex / AsyncAppServerClient don't forward
      # it, so we set it on `codex._client._sync` after construction.
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

      # The legacy subprocess path hardcoded `approvalPolicy=never` with
      # `sandbox=danger-full-access`. The SDK's `ApprovalMode.auto_review`
      # instead maps to `approvalPolicy=on_request` with
      # `approvalsReviewer=auto_review`. That may still surface a human
      # approval prompt in some cases; see `.pm/features/_003-tech-debt-and-test-gaps.md`
      # OQ-5 for the required live equivalence check.

      # SandboxMode.danger_full_access disables bwrap. Möbius runs
      # inside a Docker container where the default bwrap-based
      # workspace_write sandbox fails with `bwrap: No permissions to
      # create a new namespace, likely because the kernel does not
      # allow non-privileged user namespaces` (the docker default
      # seccomp profile blocks CLONE_NEWUSER even when the host
      # allows it). That blocked every tool that spawned a
      # sub-process — including the Read tool reading PNGs, which
      # silently broke the agent's ability to verify its own
      # screenshots. The legacy subprocess path used danger-full-
      # access for the same reason, and Möbius's design philosophy
      # ("trust the agent; container is the sandbox") is consistent.
      _sandbox = sdk["SandboxMode"].danger_full_access
      if session_id is None:
        thread = await codex.thread_start(
          approval_mode=sdk["ApprovalMode"].auto_review,
          sandbox=_sandbox,
          base_instructions=base_instructions,
          cwd=cwd,
          model=model,
        )
      else:
        thread = await codex.thread_resume(
          session_id,
          approval_mode=sdk["ApprovalMode"].auto_review,
          sandbox=_sandbox,
          cwd=cwd,
          model=model,
        )

      current_session_id = thread.id
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
      )
      active_turn = ActiveCodexTurn(thread, turn, chat_id=chat_id)
      registry.register(active_turn)

      async for notification in turn.stream():
        payload = notification.payload

        if isinstance(payload, sdk["AgentMessageDeltaNotification"]):
          if payload.delta:
            bc.publish({"type": "text", "content": payload.delta})
          continue

        if isinstance(
          payload,
          sdk["CommandExecutionOutputDeltaNotification"],
        ):
          if payload.delta:
            bc.publish({"type": "tool_output", "content": payload.delta})
          continue

        if isinstance(payload, sdk["ItemStartedNotification"]):
          item = payload.item.root if hasattr(payload.item, "root") else payload.item
          event = _tool_start_event(item, sdk)
          if event is not None:
            bc.publish(event)
          continue

        if isinstance(payload, sdk["FileChangePatchUpdatedNotification"]):
          summary = _file_change_patch_summary(payload.changes)
          if summary:
            bc.publish({"type": "tool_output", "content": summary})
          continue

        if isinstance(payload, sdk["ItemCompletedNotification"]):
          item = payload.item.root if hasattr(payload.item, "root") else payload.item
          for event in _tool_completed_events(item, sdk):
            bc.publish(event)
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
  return True
