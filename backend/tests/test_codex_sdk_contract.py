"""SDK-bump contract tests for the Codex AskUserQuestion bridge.

The bridge (`codex_sdk_runner._install_request_user_input_handler`)
monkey-patches `<AsyncCodex instance>._client._sync._approval_handler`.
Its startup check raises if the attribute is missing on the live
instance — a loud failure. What it does NOT catch:

  1. A future SDK that keeps the attribute name but ignores assignment
     (silent no-op via descriptor / __slots__). The model's
     AskUserQuestion calls would fall through to the SDK's default
     approval handler, the question card would never render, and the
     user would see a wedged turn — detectable only when a real user
     hits a clarifying question.

  2. The chain `_client._sync` itself being restructured (e.g. the SDK
     removes the sync-bridge accessor in favor of a different shape).
     The runner's attribute access would then raise AttributeError at
     startup, but only on the first chat that uses Codex.

  3. The JSON-RPC method string (`item/tool/requestUserInput`) being
     renamed by the SDK. Same silent-wedge failure mode.

These tests catch (1), (2), (3) at build time so a Codex SDK bump
(pinned in the Dockerfile to a specific commit SHA, but bumpable)
fails CI loudly rather than producing a silent runtime regression.

Earlier version of these tests probed `codex._client._sync` as a
module import and `CodexClient` as a class — neither matches what
the runner actually accesses. As a result both tests silently SKIPPED
in CI (the module didn't exist; pytest.importorskip swallowed it),
giving zero coverage of the contract they were named for. The current
file exercises the live instance path the runner actually uses.

See CLAUDE.md "Codex `_approval_handler` patch — known fragility" for
the broader context.
"""

import inspect
import json

import pytest


def _make_sync_handle():
  """Constructs the live object the runner patches.

  Mirrors what `_install_request_user_input_handler` reaches for:
  `AsyncCodex()._client._sync`. No network or subprocess — just
  instance construction. Skips cleanly if the SDK isn't installed
  (keeps the test useful on slim CI containers).
  """
  openai_codex = pytest.importorskip("openai_codex")
  ac = openai_codex.AsyncCodex()
  return ac._client._sync


def _make_async_handle():
  """Return the live async-client instance whose call bridge we replace."""
  openai_codex = pytest.importorskip("openai_codex")
  return openai_codex.AsyncCodex()._client


def test_codex_approval_handler_attribute_exists():
  """`_approval_handler` must exist on the live `_client._sync` object.

  If a SDK bump removes/renames it, `_install_request_user_input_handler`
  raises at the startup hasattr check — but only on the first Codex
  turn after deploy. This test surfaces the breakage at build time.
  """
  sync = _make_sync_handle()
  assert hasattr(sync, "_approval_handler"), (
    "openai_codex no longer exposes _approval_handler on "
    "AsyncCodex()._client._sync. The AskUserQuestion bridge in "
    "codex_sdk_runner.py will refuse to install at startup. Inspect "
    "the new SDK surface for the renamed hook and update "
    "_install_request_user_input_handler accordingly."
  )


def test_codex_approval_handler_assignment_takes_effect():
  """Monkey-patching `_approval_handler` must actually replace the slot.

  If a future SDK locks the attribute via __slots__, a property, or a
  descriptor that ignores plain assignment, our patch installs
  successfully but the original handler keeps running — the bridge
  silently no-ops. Verify the assignment is read-back-the-same on the
  live instance the runner targets.
  """
  sync = _make_sync_handle()
  original = sync._approval_handler
  sentinel = object()
  try:
    sync._approval_handler = sentinel
    assert sync._approval_handler is sentinel, (
      "Assignment to _approval_handler on AsyncCodex()._client._sync "
      "did not take effect — the SDK likely added a descriptor, "
      "__slots__, or property that blocks our bridge. The "
      "AskUserQuestion bridge would silently no-op; users would see "
      "wedged turns on clarifying questions."
    )
  finally:
    sync._approval_handler = original


def test_codex_async_call_bridge_assignment_takes_effect():
  """The per-chat executor must be installable on the pinned SDK instance."""
  client = _make_async_handle()
  original = client._call_sync

  async def replacement(_fn, /, *_args, **_kwargs):
    return "owned-executor"

  try:
    client._call_sync = replacement
    assert client._call_sync is replacement, (
      "Assignment to AsyncCodex()._client._call_sync did not take effect. "
      "Parked notification waits would fall back to asyncio's shared default "
      "executor and can take the shell and new chats offline."
    )
  finally:
    client._call_sync = original


def test_request_user_input_method_string_unchanged():
  """The JSON-RPC method string our handler keys on must still match.

  Today: `item/tool/requestUserInput`. If the SDK renames the RPC
  method (e.g. to `tool/requestUserInput`), `_approval_handler` is
  invoked but our `_REQUEST_USER_INPUT_METHOD` mismatch sends the
  request to the SDK's default-accept path — silent failure.
  """
  from app.codex_sdk_runner import _REQUEST_USER_INPUT_METHOD
  assert _REQUEST_USER_INPUT_METHOD == "item/tool/requestUserInput", (
    "_REQUEST_USER_INPUT_METHOD has changed from the expected SDK "
    "wire string. Verify codex/_client docs and update the constant "
    "if the SDK genuinely renamed the method."
  )


def test_request_user_input_bridge_has_no_user_answer_timeout():
  """The Codex bridge must honor the AskUserQuestion no-timeout contract.

  A bounded ``Future.result(timeout=...)`` turns a slow human answer into an
  artificial expiry and makes the agent end the turn before the user can
  approve. Stop/cancel remains the explicit escape hatch.
  """
  import inspect
  from app import codex_sdk_runner

  source = inspect.getsource(codex_sdk_runner._install_request_user_input_handler)
  assert "fut.result()" in source
  assert "fut.result(timeout=" not in source
  assert "_BRIDGE_USER_ANSWER_TIMEOUT" not in source


def test_subagent_activity_is_natively_modeled_and_fallback_removed():
  """Lock in the native subAgentActivity contract this SDK bump established.

  This is the inverse of the earlier tripwire. Before openai-codex
  rust-v0.145.0-alpha.13 the generated ThreadItem union omitted the
  `subAgentActivity` variant, so resuming a thread whose history contained one
  raised a validation error that `_resume_codex_thread` caught and worked
  around. The pinned SDK now models the variant natively, the workaround is
  gone, and the runner classifies the item explicitly (a documented no-op in
  the tool-event dispatch). Assert every leg of that so a future SDK that
  renames or drops the type — or a change that resurrects the fallback — fails
  loudly rather than silently reopening the resume gap.
  """
  import inspect

  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all
  from app import codex_sdk_runner

  schema = json.dumps(v2_all.ThreadItem.model_json_schema())
  assert getattr(v2_all, "SubAgentActivityThreadItem", None) is not None, (
    "The Codex SDK no longer exposes SubAgentActivityThreadItem. If upstream "
    "renamed/dropped it, resuming a thread with sub-agent history may raise "
    "again — decide the new handling before accepting the bump."
  )
  assert "subAgentActivity" in schema

  # The runner imports the native item and hands it to dispatch as a non-None
  # entry (defensive block, so a predates-it SDK still boots).
  assert codex_sdk_runner._sdk_imports()["SubAgentActivityThreadItem"] is not None

  # The item is classified explicitly at the dispatch sites, not dropped by
  # fall-through.
  assert "SubAgentActivityThreadItem" in inspect.getsource(
    codex_sdk_runner._tool_start_event
  )
  assert "SubAgentActivityThreadItem" in inspect.getsource(
    codex_sdk_runner._tool_completed_events
  )

  # The old compatibility path is gone: neither the resume wrapper nor its
  # error-classifier should linger as dead code.
  assert not hasattr(codex_sdk_runner, "_resume_codex_thread")
  assert not hasattr(codex_sdk_runner, "_is_subagent_activity_resume_validation_error")


def test_context_compaction_item_is_available_to_the_runner():
  """Pin the canonical item lifecycle that makes Codex compaction visible.

  Current app-server v2 emits ``contextCompaction`` through item completion
  and suppresses the deprecated ``thread/compacted`` notification. A runtime
  bump that drops or renames this generated item must fail here rather than
  silently removing every compaction marker from the chat timeline.
  """
  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all
  from app import codex_sdk_runner

  schema = json.dumps(v2_all.ThreadItem.model_json_schema())
  assert getattr(v2_all, "ContextCompactionThreadItem", None) is not None
  assert "contextCompaction" in schema
  assert codex_sdk_runner._sdk_imports()["ContextCompactionThreadItem"] is (
    v2_all.ContextCompactionThreadItem
  )


def test_native_image_view_item_is_wired_to_tool_dispatch():
  """Codex image inspection must not disappear from the visible transcript."""
  import inspect

  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all
  from app import codex_sdk_runner

  assert getattr(v2_all, "ImageViewThreadItem", None) is not None
  assert codex_sdk_runner._sdk_imports()["ImageViewThreadItem"] is (
    v2_all.ImageViewThreadItem
  )
  assert "ImageViewThreadItem" in inspect.getsource(
    codex_sdk_runner._tool_start_event
  )
  assert "ImageViewThreadItem" in inspect.getsource(
    codex_sdk_runner._tool_completed_events
  )


def test_native_image_view_path_is_plain_text_before_sse_replay():
  """The SDK's root-model path must not poison every stream reconnect."""
  import json

  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all
  from app import codex_sdk_runner

  item = v2_all.ImageViewThreadItem(
    id="image-1",
    path="/data/chats/chat-1/media/diagram.png",
    type="imageView",
  )
  event = codex_sdk_runner._tool_start_event(
    item, {"ImageViewThreadItem": v2_all.ImageViewThreadItem},
  )

  assert event == {
    "type": "tool_start",
    "tool": "ViewImage",
    "input": "/data/chats/chat-1/media/diagram.png",
  }
  assert json.loads(json.dumps(event)) == event


def test_lifecycle_notification_fields_and_status_enums_are_pinned():
  """Fail loudly if the generated lifecycle surface drifts under the runner."""
  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all

  assert set(v2_all.ItemStartedNotification.model_fields) >= {
    "item", "started_at_ms", "thread_id", "turn_id",
  }
  assert (v2_all.ItemStartedNotification.model_fields["started_at_ms"].alias
          == "startedAtMs")
  assert set(v2_all.ItemCompletedNotification.model_fields) >= {
    "item", "completed_at_ms", "thread_id", "turn_id",
  }
  assert (v2_all.ItemCompletedNotification.model_fields["completed_at_ms"].alias
          == "completedAtMs")
  assert set(v2_all.ThreadStartedNotification.model_fields) == {"thread"}
  assert set(v2_all.ThreadStatusChangedNotification.model_fields) == {
    "status", "thread_id",
  }
  assert set(v2_all.CollabAgentToolCallThreadItem.model_fields) >= {
    "id", "tool", "sender_thread_id", "receiver_thread_ids", "agents_states",
  }
  collab_schema = json.dumps(v2_all.CollabAgentState.model_json_schema())
  for status in ("completed", "errored", "interrupted", "shutdown"):
    assert status in collab_schema
  thread_schema = json.dumps(v2_all.ThreadStatus.model_json_schema())
  for status in ("active", "idle", "systemError", "notLoaded"):
    assert status in thread_schema
  assert {item.value for item in v2_all.CollabAgentTool} >= {
    "spawnAgent", "sendInput", "resumeAgent",
  }
  assert {item.value for item in v2_all.SubAgentActivityKind} >= {
    "started", "interacted", "interrupted",
  }


def test_web_search_results_survive_generated_sdk_schema_lag():
  """App-server result URLs must reach the source-pill extractor.

  Codex 0.145's Rust protocol emits ``webSearch.results`` while the generated
  Python leaf model still omits that field. The runner installs a narrow
  passthrough at import time; validate the real notification union so a future
  SDK refactor cannot silently resume dropping every search result.
  """
  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all
  from app import codex_sdk_runner

  codex_sdk_runner._sdk_imports()
  payload = v2_all.ItemCompletedNotification.model_validate({
    "completedAtMs": 1,
    "threadId": "thread-1",
    "turnId": "turn-1",
    "item": {
      "id": "search-1",
      "type": "webSearch",
      "query": "Möbius parity",
      "results": [{
        "title": "Structured search result",
        "url": "https://example.test/result",
      }],
    },
  })
  item = payload.item.root
  assert getattr(item, "results") == [{
    "title": "Structured search result",
    "url": "https://example.test/result",
  }]
  from app.codex_events import _web_search_sources

  assert _web_search_sources(item) == [{
    "title": "Structured search result",
    "url": "https://example.test/result",
  }]


def test_cache_write_usage_survives_generated_sdk_schema_evolution():
  """The cache-write counter reaches normalization on old and new SDKs."""
  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all
  from app import codex_sdk_runner

  codex_sdk_runner._sdk_imports()
  breakdown = {
    "inputTokens": 100,
    "cachedInputTokens": 10,
    "cacheWriteInputTokens": 60,
    "outputTokens": 20,
    "reasoningOutputTokens": 5,
    "totalTokens": 120,
  }
  payload = v2_all.ThreadTokenUsageUpdatedNotification.model_validate({
    "threadId": "thread-1",
    "turnId": "turn-1",
    "tokenUsage": {
      "last": breakdown,
      "total": breakdown,
      "modelContextWindow": 1_050_000,
    },
  })
  last = payload.token_usage.last
  assert (
    getattr(last, "cache_write_input_tokens", None) == 60
    or (last.model_extra or {}).get("cacheWriteInputTokens") == 60
  )
  assert codex_sdk_runner.normalize_codex_usage(
    payload.token_usage,
    payload.token_usage,
  )["cache_creation_input_tokens"] == 60


def test_turn_terminal_status_and_message_phase_contracts_are_pinned():
  """The runner must not confuse a terminal envelope with a final answer."""
  pytest.importorskip("openai_codex")
  from openai_codex.generated import v2_all

  assert {item.value for item in v2_all.TurnStatus} == {
    "completed", "interrupted", "failed", "inProgress",
  }
  assert {item.value for item in v2_all.MessagePhase} == {
    "commentary", "final_answer",
  }
  assert v2_all.Turn.model_fields["status"].annotation is v2_all.TurnStatus
  phase_annotation = v2_all.AgentMessageThreadItem.model_fields["phase"].annotation
  assert v2_all.MessagePhase in getattr(phase_annotation, "__args__", ())


def test_reasoning_effort_enum_tolerates_unknown_efforts():
  """Lock in the forgiving ReasoningEffort enum that unblocked models()/resume.

  The 0.144.x generated enum was strict (none/minimal/low/medium/high/xhigh)
  and rejected efforts the CLI advertises for newer models (e.g. gpt-5.6-sol's
  `max`/`ultra`), breaking codex.models() and ThreadResumeResponse validation.
  rust-v0.145.0-alpha.13 made it a forgiving `str, Enum` with a `_missing_`
  hook. If a future SDK reverts to a strict enum, this fails loudly.
  """
  pytest.importorskip("openai_codex")
  from openai_codex.types import ReasoningEffort

  for value in ("high", "xhigh", "max", "ultra", "some-future-effort"):
    assert ReasoningEffort(value).value == value


def test_transport_closed_error_is_still_exposed_by_the_sdk():
  """_is_transport_death decides whether an error reaches the owner at all.

  It binds to this class by isinstance, and a stop that SIGTERMs the turn's
  process group is reclassified as a clean interrupt only when the raised
  exception is one. If a future SDK renames or drops the symbol, the runner
  would stop recognizing its own teardown and go back to publishing a raw
  provider error over the pause note — so fail here first.
  """
  pytest.importorskip("openai_codex")
  from openai_codex.errors import CodexError, TransportClosedError

  assert issubclass(TransportClosedError, CodexError)
  # Not a RuntimeError: the runner relies on that to keep provider
  # ErrorNotifications (reraised as plain RuntimeErrors) out of this branch.
  assert not issubclass(TransportClosedError, RuntimeError)


def test_native_goal_resume_sdk_contract_is_pinned():
  """Fail at build time if Codex moves the goal lifecycle Möbius relies on."""
  pytest.importorskip("openai_codex")
  from openai_codex import AsyncCodex
  from openai_codex._goal import (
    _AsyncGoalNotificationStream,
    _GoalOperationState,
  )
  from openai_codex.errors import InvalidRequestError
  from openai_codex.generated.v2_all import (
    ThreadGoalGetResponse,
    ThreadGoalStatus,
  )

  client = AsyncCodex()._client
  for method in (
    "request",
    "register_goal_operation",
    "unregister_goal_operation",
    "start_goal_operation",
    "next_goal_notification",
    "cancel_goal_operation",
    "thread_goal_clear",
    "thread_goal_set",
    "turn_steer",
  ):
    assert callable(getattr(client, method, None)), (
      f"openai_codex removed AsyncCodex._client.{method}; update the native "
      "goal adapter before accepting this SDK bump"
    )
  assert _AsyncGoalNotificationStream is not None
  assert issubclass(InvalidRequestError, Exception)
  assert list(inspect.signature(_AsyncGoalNotificationStream).parameters)[:4] == [
    "state", "next_notification", "unregister", "cancel_goal",
  ]
  state = _GoalOperationState(thread_id="contract-thread")
  for method in (
    "active_turn",
    "wait_for_start",
    "finish",
    "wake_notification_reader",
  ):
    assert callable(getattr(state, method, None))
  assert hasattr(state, "_condition")
  assert hasattr(state, "_failure")
  assert set(ThreadGoalGetResponse.model_fields) == {"goal"}
  assert {status.value for status in ThreadGoalStatus} == {
    "active", "paused", "blocked", "usageLimited", "budgetLimited", "complete",
  }
