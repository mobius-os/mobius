"""Claude SDK turn runner for Möbius.

This module isolates the Claude Agent SDK integration behind one
function that executes exactly one Möbius chat turn and publishes the
same event shapes the rest of the backend already understands.

Design choices:

- `permission_mode="bypassPermissions"` preserves Möbius's existing
  trust posture. The agent is expected to have the normal Claude Code
  tool surface. We intercept only `AskUserQuestion`, because that tool
  must become an explicit partner choice in the Möbius UI.
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
- Stop support is wired through `active_clients[chat_id]`. The caller
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
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import (
  AssistantMessage,
  PermissionResultAllow,
  PermissionResultDeny,
  ResultMessage,
  StreamEvent,
  SystemMessage,
  ToolResultBlock,
  ToolUseBlock,
  UserMessage,
)

from app.pending_questions import PendingQuestion
from app.runner_registry import RunnerKind, registry
from app.runtime_types import RunnerResult
from app.tool_summaries import summarize_tool_input


class ActiveClaudeClient:
  """Stop handle stored in `chat._active_clients` for SDK-backed turns.

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
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

  async def interrupt(self) -> None:
    """Interrupts the live run and waits for runner-side drain.

    Bounds the `_finished` wait at 5s as a defense-in-depth so a
    wedged runner (one that never reaches its `finally` block) can't
    hang Stop indefinitely. `chat.py:stop_chat_for` adds its own 2s
    bound at the call site; this inner timeout protects any other
    direct caller.
    """
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


def _result_error_message(result: ResultMessage) -> str:
  """Builds a user-facing error string from an SDK result."""
  if isinstance(result.result, str) and result.result.strip():
    return result.result.strip()
  if result.errors:
    return "\n".join(err for err in result.errors if err).strip()
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


async def _maybe_await(value: Any) -> Any:
  """Awaits a value only when the callback returned an awaitable."""
  if inspect.isawaitable(value):
    return await value
  return value


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
) -> RunnerResult:
  """Runs one Claude SDK turn and translates SDK messages to Möbius events.

  Args:
    user_message: Fully prepared user prompt for this turn.
    session_id: Existing Claude session to resume, or None on first turn.
    base_env: Environment passed through to the Claude subprocess.
    cwd: Working directory for the SDK run.
    chat_id: Möbius chat identifier used for registries.
    skill_text: Möbius skill/system prompt text for first turn only.
    bc: Chat broadcast object with a publish(event) method.
    pending_questions: Shared AskUserQuestion registry owned by chat.py.
    db: SQLAlchemy session used by runner-side persistence.

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
    # intercepting AskUserQuestion for the partner UX.
    if tool_name != "AskUserQuestion":
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
    )
    pending_questions[chat_id] = pending

    bc.publish({
      "type": "question",
      "questions": questions,
    })
    # Push notification on AskUserQuestion is now AGENT-DRIVEN: the
    # skill/seed tells the agent to `curl POST /api/notifications/send`
    # itself when it asks a question. That gives the agent direct
    # visibility into push success/failure via the bash tool output,
    # avoids the silent-failure-mode the auto-notify path had, and
    # lets the agent decide whether a particular question is worth a
    # phone buzz.

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
    options_kwargs = {
      "system_prompt": skill_text,
      "resume": session_id if session_id is not None else None,
      "cwd": cwd,
      "env": base_env,
      "setting_sources": None,
      "include_partial_messages": True,
      "can_use_tool": can_use_tool,
      "hooks": {
        "PreToolUse": [
          HookMatcher(matcher=None, hooks=[keepalive_hook]),
        ],
      },
    }
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
      await client.query(user_message)

      async for sdk_msg in client.receive_response():
        if isinstance(sdk_msg, SystemMessage):
          if sdk_msg.subtype == "init":
            continue
          continue

        if isinstance(sdk_msg, StreamEvent):
          current_session_id = sdk_msg.session_id or current_session_id
          event = sdk_msg.event
          if event.get("type") != "content_block_delta":
            continue
          delta = event.get("delta", {})
          if delta.get("type") != "text_delta":
            continue
          text = delta.get("text")
          if text:
            bc.publish({"type": "text", "content": text})
          continue

        if isinstance(sdk_msg, AssistantMessage):
          if sdk_msg.session_id:
            current_session_id = sdk_msg.session_id
          for block in sdk_msg.content:
            if not isinstance(block, ToolUseBlock):
              continue
            bc.publish({
              "type": "tool_start",
              "tool": block.name,
              "input": "",
            })
            summary = summarize_tool_input(block.name, block.input)
            if not summary:
              continue
            bc.publish({
              "type": "tool_input",
              "tool": block.name,
              "input": summary,
            })
          continue

        if isinstance(sdk_msg, UserMessage):
          for block in sdk_msg.content if isinstance(sdk_msg.content, list) else []:
            if not isinstance(block, ToolResultBlock):
              continue
            bc.publish({
              "type": "tool_output",
              "content": _format_tool_output(block.content),
            })
            bc.publish({
              "type": "tool_end",
            })
          continue

        if isinstance(sdk_msg, ResultMessage):
          current_session_id = sdk_msg.session_id or current_session_id
          cost_usd = sdk_msg.total_cost_usd
          return {
            "session_id": current_session_id,
            "cost_usd": cost_usd,
            "usage": None,
            "error": (
              _result_error_message(sdk_msg)
              if sdk_msg.is_error else None
            ),
          }

      return {
        "session_id": current_session_id,
        "cost_usd": cost_usd,
        "usage": None,
        "error": None,
      }
    except Exception as exc:
      return {
        "session_id": current_session_id,
        "cost_usd": None,
        "usage": None,
        "error": str(exc),
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
