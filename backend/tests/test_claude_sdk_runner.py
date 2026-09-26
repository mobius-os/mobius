"""Tests for the Claude SDK runner's event dispatch.

These tests exercise `dispatch_sdk_message` directly with hand-built
SDK message instances so the unit doesn't spin up the Claude
subprocess or the SDK transport. The dispatch is the load-bearing
behavior we care about: every SDK message type either translates
into a Möbius event or is logged as unhandled. Unhandled events are
never broadcast.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from claude_agent_sdk import ProcessError, ResultError
from claude_agent_sdk.types import (
  AssistantMessage,
  PermissionResultAllow,
  PermissionResultDeny,
  RateLimitEvent,
  RateLimitInfo,
  ResultMessage,
  ServerToolResultBlock,
  ServerToolUseBlock,
  StreamEvent,
  SystemMessage,
  TaskNotificationMessage,
  TaskProgressMessage,
  TaskStartedMessage,
  TaskUpdatedMessage,
  TextBlock,
  ThinkingBlock,
  ToolResultBlock,
  ToolUseBlock,
  UserMessage,
)

from app import claude_events, claude_sdk_runner, models
from app import connectors as connector_core
from app.claude_sdk_runner import (
  ActiveClaudeClient,
  dispatch_sdk_message,
  run_claude_sdk_turn,
  steer_into_active_turn,
)
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry


class _Bus:
  """Minimal stand-in for ChatBroadcast used by the dispatch tests.

  Records every publish call in order so assertions can check both
  the event sequence and the event payloads.
  """

  def __init__(self) -> None:
    self.events: list[dict] = []
    self.lifecycle_events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)

  def record_lifecycle(self, event: dict) -> None:
    self.lifecycle_events.append(event)


class _ChatBus(_Bus):
  chat_id = "chat-42"
  run_token = "run-1"


class _FakeClient:
  """Stand-in for `ClaudeSDKClient` that records what the runner did to it.

  Subclasses override `receive_response` to script the SDK stream; everything
  else (connect / query / interrupt / disconnect bookkeeping) is shared.
  """

  def __init__(self, options):
    self.options = options
    self.queries: list = []
    self.interrupts = 0
    self.disconnected = False

  async def connect(self):
    return None

  async def query(self, message):
    self.queries.append(message)

  async def interrupt(self):
    self.interrupts += 1

  async def disconnect(self):
    self.disconnected = True

  async def receive_response(self):
    yield _success_result()


def _install_fake_client(monkeypatch, client_cls=_FakeClient) -> list:
  """Patch the runner's client class; returns the list of created clients."""
  clients: list = []

  def _factory(options):
    client = client_cls(options)
    clients.append(client)
    return client

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _factory)
  return clients


async def _run_turn(
  chat_id: str,
  *,
  bc=None,
  prompt: str = "hello",
  session_id: str | None = None,
  cwd: str = "/tmp",
  db=None,
  **kwargs,
) -> dict:
  return await run_claude_sdk_turn(
    user_message=prompt,
    session_id=session_id,
    base_env={},
    cwd=cwd,
    chat_id=chat_id,
    skill_text="system",
    bc=_ChatBus() if bc is None else bc,
    pending_questions={},
    db=db,
    **kwargs,
  )


@pytest.mark.asyncio
async def test_claude_collects_fast_generated_file_at_turn_end(monkeypatch, tmp_path):
  """Turn-owned inbox capture does not depend on provider tool-hook timing."""
  class _GeneratedFileBus(_ChatBus):
    async def generated_file_capacity(self):
      return 1

    async def publish_generated_file(self, event):
      self.events.append(event)
      return event["name"]

  class _FastPdfClient(_FakeClient):
    async def query(self, message):
      self.queries.append(message)
      output_dir = pathlib.Path(self.options.env["MOBIUS_GENERATED_DIR"])
      (output_dir / "fast.pdf").write_bytes(b"%PDF-1.4")

  _install_fake_client(monkeypatch, _FastPdfClient)
  bus = _GeneratedFileBus()
  await _run_turn("fast-generated-file", cwd=str(tmp_path), bc=bus)

  [event] = [
    item for item in bus.events if item.get("type") == "generated_file"
  ]
  assert event["name"] == "fast.pdf"
  assert event["previewable"] is True
  from app.config import get_settings
  stored = claude_sdk_runner.generated_files.stored_dir(
    get_settings().data_dir, "fast-generated-file",
  )
  assert (stored / event["path"]).read_bytes() == b"%PDF-1.4"


@pytest.mark.asyncio
async def test_claude_mcp_set_stays_strict_with_native_skills_and_fd_retirement(
  monkeypatch,
):
  observed = {}
  from contextlib import contextmanager
  original_config_handle = connector_core.claude_mcp_config_handle

  @contextmanager
  def capture_config_handle(*args, **kwargs):
    with original_config_handle(*args, **kwargs) as handle:
      observed['handle'] = handle
      yield handle

  monkeypatch.setattr(connector_core, 'claude_mcp_config_handle', capture_config_handle)

  class _Client(_FakeClient):
    def __init__(self, options):
      super().__init__(options)
      assert options.strict_mcp_config is True
      assert options.setting_sources == ["user", "project"]
      assert options.skills == "all"
      observed["path"] = str(options.mcp_servers)
      assert observed["path"].startswith(f"/proc/{os.getpid()}/fd/")
      assert "private-key" not in observed["path"]
      with open(observed["path"], encoding="utf-8") as file:
        observed["config"] = file.read()

    async def connect(self):
      assert os.path.exists(observed["path"])

    async def query(self, _message):
      # connect() has consumed the config. Its argv-visible fd remains reserved
      # but harmless, rather than being reusable for unrelated process data.
      assert os.path.exists(observed["path"])
      with open(observed["path"], "rb") as retired_file:
        assert retired_file.read() == b""
      held_fd = int(observed["path"].rsplit("/", 1)[1])
      with tempfile.TemporaryFile() as unrelated:
        unrelated.write(b"unrelated-live-secret")
        unrelated.flush()
        assert unrelated.fileno() != held_fd
        with open(observed["path"], "rb") as retired_file:
          assert retired_file.read() == b""

  _install_fake_client(monkeypatch, _Client)
  plan = connector_core.ConnectorTurnPlan(claude_servers={
    "private": {
      "type": "http",
      "url": "https://mcp.example/mcp",
      "headers": {"Authorization": "Bearer private-key"},
    },
  })

  result = await _run_turn(
    "claude-mcp-config", connector_plan=plan, skills_enabled=True,
  )

  assert "private-key" in observed["config"]
  # Descriptor numbers may be reused after teardown; verify the owned file.
  assert observed['handle']._file.closed
  assert result["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("connect_error", "expected_error"),
  [
    (asyncio.TimeoutError(), "connect timeout"),
    (RuntimeError("connect failed"), "connect failed"),
  ],
)
async def test_claude_connection_secret_file_closes_when_connect_fails(
  monkeypatch,
  connect_error,
  expected_error,
):
  observed = {}

  class _FailingClient:
    def __init__(self, options):
      observed["path"] = str(options.mcp_servers)
      assert os.path.exists(observed["path"])

    async def connect(self):
      raise connect_error

    async def disconnect(self):
      with open(observed["path"], "rb") as retired_file:
        assert retired_file.read() == b""
      observed["disconnected"] = True

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FailingClient)
  plan = connector_core.ConnectorTurnPlan(claude_servers={
    "private": {
      "type": "http",
      "url": "https://mcp.example/mcp",
      "headers": {"Authorization": "Bearer private-key"},
    },
  })

  result = await run_claude_sdk_turn(
    user_message="hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id=f"claude-mcp-{expected_error}",
    skill_text="system",
    bc=_ChatBus(),
    pending_questions={},
    db=None,
    connector_plan=plan,
  )

  assert result["error"] == expected_error
  assert observed["disconnected"] is True
  assert not os.path.exists(observed["path"])


@pytest.mark.asyncio
async def test_claude_connection_secret_file_closes_when_connect_is_cancelled(
  monkeypatch,
):
  observed = {}
  connecting = asyncio.Event()

  class _CancelledClient:
    def __init__(self, options):
      observed["path"] = str(options.mcp_servers)

    async def connect(self):
      assert os.path.exists(observed["path"])
      connecting.set()
      await asyncio.Future()

    async def disconnect(self):
      with open(observed["path"], "rb") as retired_file:
        assert retired_file.read() == b""
      observed["disconnected"] = True

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _CancelledClient)
  plan = connector_core.ConnectorTurnPlan(claude_servers={
    "private": {
      "type": "http",
      "url": "https://mcp.example/mcp",
      "headers": {"Authorization": "Bearer private-key"},
    },
  })
  turn = asyncio.create_task(run_claude_sdk_turn(
    user_message="hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="claude-mcp-cancelled",
    skill_text="system",
    bc=_ChatBus(),
    pending_questions={},
    db=None,
    connector_plan=plan,
  ))

  await connecting.wait()
  turn.cancel()
  with pytest.raises(asyncio.CancelledError):
    await turn

  assert observed["disconnected"] is True
  assert not os.path.exists(observed["path"])


def _stream_delta(delta_type: str, **fields: Any) -> StreamEvent:
  """Build a StreamEvent carrying a single content_block_delta."""
  return StreamEvent(
    uuid="evt-1",
    session_id="sess-1",
    event={
      "type": "content_block_delta",
      "delta": {"type": delta_type, **fields},
    },
  )


@pytest.mark.asyncio
async def test_steer_into_active_turn_interrupts_immediately():
  """A registered Claude handle buffers the steer text AND fires the
  interrupt immediately (a soft interrupt on the same connected client),
  rather than deferring the cut to the next content-block boundary — a
  steer during a long-running tool call must land now, not whenever the
  tool happens to finish."""
  calls = []

  class _Client:
    async def interrupt(self):
      calls.append("interrupt")

  handle = ActiveClaudeClient(_Client(), chat_id="claude-steer")
  handle.mark_generating()
  registry.register(handle)
  try:
    assert await steer_into_active_turn("claude-steer", "use blue") is True
    assert handle.pending_steer == ["use blue"]
    # The steer interrupts the live turn right away.
    assert calls == ["interrupt"]
    # A second rapid steer must QUEUE behind the first (FIFO), not overwrite it
    # — both texts are already persisted to the transcript, so both must reach
    # Claude when the runner drains the mailbox. It must NOT fire a second
    # interrupt: `_interrupt_in_flight` guards the single cut until the first
    # interrupt's terminal result drains the whole buffer together.
    assert await steer_into_active_turn("claude-steer", "and bold") is True
    assert handle.pending_steer == ["use blue", "and bold"]
    assert calls == ["interrupt"]
  finally:
    registry.unregister("claude-steer", handle.kind)


@pytest.mark.asyncio
async def test_steer_before_generation_interrupts_once_streaming(
  monkeypatch,
):
  """The CLI drops an interrupt sent before its query is generating, which
  used to latch the cut so the steer landed only at natural turn end. A steer
  in that window must interrupt exactly once, when the model starts streaming."""
  trace: list[int] = []

  class _Client(_FakeClient):
    async def query(self, prompt):
      await super().query(prompt)
      if len(self.queries) == 1:
        assert await steer_into_active_turn("early-chat", "use blue") is True
        trace.append(self.interrupts)

    async def receive_response(self):
      if len(self.queries) == 1:
        yield _stream_delta("text_delta", text="starting")
        while self.interrupts < 1:
          await asyncio.sleep(0)
        yield _interrupt_result()
        return
      yield _stream_delta("text_delta", text="blue done")
      yield _success_result()

  clients = _install_fake_client(monkeypatch, _Client)
  result = await asyncio.wait_for(
    _run_turn("early-chat", prompt="start task"), timeout=5,
  )

  client = clients[0]
  assert trace == [0]
  assert client.interrupts == 1
  assert len(client.queries) == 2
  assert "use blue" in client.queries[1]
  assert result["error"] is None


@pytest.mark.asyncio
async def test_steer_into_active_turn_missing_or_finished_is_false():
  """Missing or already-finished Claude handles are not steerable."""
  assert await steer_into_active_turn("missing-claude", "x") is False

  class _Client:
    async def interrupt(self):
      raise AssertionError("finished handle must not interrupt")

  handle = ActiveClaudeClient(_Client(), chat_id="finished-claude")
  handle.mark_finished()
  registry.register(handle)
  try:
    assert await steer_into_active_turn("finished-claude", "x") is False
  finally:
    registry.unregister("finished-claude", handle.kind)


@pytest.mark.asyncio
async def test_steer_requeries_on_interrupt_terminal(monkeypatch):
  """A steer fired mid-delta interrupts immediately and re-queries on the
  SAME client when the interrupt's terminal ResultMessage arrives.

  The steer fires its soft interrupt as soon as it is requested (interrupts
  == 1), even though no completed content block preceded the terminal; the
  pending_steer -> requery path on the terminal result then delivers the
  steer text on the same session."""
  class _Client(_FakeClient):
    async def receive_response(self):
      if len(self.queries) == 1:
        yield _stream_delta("text_delta", text="working")
        assert await steer_into_active_turn("loop-chat", "use blue") is True
        yield _interrupt_result()
        return
      yield _stream_delta("text_delta", text="blue done")
      yield _success_result()

  clients = _install_fake_client(monkeypatch, _Client)
  bus = _ChatBus()
  result = await _run_turn("loop-chat", bc=bus, prompt="start task")

  client = clients[0]
  # The steer fired its interrupt immediately when requested; the terminal
  # ResultMessage then drove the requery.
  assert client.interrupts == 1
  assert client.disconnected is True
  assert client.queries[0] == "start task"
  assert client.queries[1].startswith(
    "New context arrived while you were working."
  )
  assert "use blue" in client.queries[1]
  assert result["error"] is None
  assert result["cost_usd"] == 0.02
  assert [e for e in bus.events if e["type"] == "text"] == [
    {"type": "text", "content": "working"},
    {"type": "text", "content": "blue done"},
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "sess-1"])
async def test_steer_interrupt_racing_turn_end_is_a_resumable_pause(
  monkeypatch, session_id,
):
  """A steer whose soft interrupt lands AT natural turn-end must not surface
  the raw "Execution interrupted." provider error, nor re-run the prompt.

  The turn finishes on its own exactly as the steer arrives: the steer buffers
  its text and fires interrupt(), but the CLEAN end_turn terminal wins the race,
  so the runner re-queries the steer text (draining pending_steer) and the stray
  interrupt() then aborts the RE-QUERY turn — whose terminal arrives with
  pending_steer already empty and no Stop in flight. The runner defuses the
  error (the interrupt was ours) and marks the turn resume_incomplete so the
  finalize seam renders a calm resumable note.

  On a RESUME turn (session_id set — the production steer condition) the
  defused error would also unlock the synthetic-no-op auto-requery guard
  (`_seal_steer_split` resets `assistant_blocks` to []); the explicit
  `stop_reason != "interrupt"` guard keeps the runner from silently re-running
  the ORIGINAL prompt and re-executing its side effects."""

  class _Client(_FakeClient):
    async def receive_response(self):
      if len(self.queries) == 1:
        yield _stream_delta("text_delta", text="working")
        assert await steer_into_active_turn("steer-race-chat", "use blue") is True
        yield _success_result()   # natural turn-end wins the race
        return
      # Re-query turn: the stray interrupt aborts it with zero accrued blocks.
      yield _interrupt_result()

  clients = _install_fake_client(monkeypatch, _Client)
  bus = _ChatBus()
  # The synthetic-no-op guard keys on the sink's accrued blocks being empty.
  bus.assistant_blocks = []

  result = await _run_turn(
    "steer-race-chat", bc=bus, prompt="start task", session_id=session_id,
  )

  # The original prompt is queried exactly once, then only the steer redirect.
  client = clients[0]
  assert client.queries.count("start task") == 1
  assert len(client.queries) == 2
  assert client.queries[1].startswith("New context arrived while you were working.")
  # The raw provider error never surfaces; the turn is a resumable interrupt.
  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"
  assert result["resume_incomplete"] is True


def _tool_boundary_interrupt_result(
  session_id: str = "sess-1", stop_reason: str | None = "tool_use",
) -> ResultMessage:
  """The terminal a soft interrupt produces when it lands WHILE a tool is the
  last action — exactly the card-commit case. `_result_error_message` maps the
  `error_during_execution` subtype to "Execution interrupted.", and the CLI
  reports stop_reason `tool_use`/null here (its own `[ede_diagnostic]`), NOT
  `interrupt` — so the defuse must key on our ownership, not stop_reason."""
  return ResultMessage(
    subtype="error_during_execution",
    duration_ms=10,
    duration_api_ms=5,
    is_error=True,
    num_turns=1,
    session_id=session_id,
    stop_reason=stop_reason,
    total_cost_usd=0.01,
    usage={"input_tokens": 1, "output_tokens": 2},
  )


_OWNER_CARD_RECEIPT = {
  "state": "waiting_for_owner",
  "question_id": "card-9",
  "next_action": "The turn is over.",
}


class _CardBus(_ChatBus):
  """A bus that owns exactly one continuation card, as the live sink does."""

  question_id = "card-9"

  def has_continuation_card(self, question_id: str) -> bool:
    return question_id == self.question_id


def _post_tool_use_hooks(options) -> list:
  """The runner's PostToolUse callbacks, in registration order."""
  return [
    hook
    for matcher in options.hooks["PostToolUse"]
    for hook in matcher.hooks
  ]


async def _fire_card_end_hook(options, **overrides) -> dict:
  """Invoke the card-end hook exactly as the CLI does after a tool result."""
  hook_input = {
    "hook_event_name": "PostToolUse",
    "tool_name": "mcp__mobius_control__request_approval",
    "tool_input": {"question": "Deploy?"},
    "tool_response": [{"type": "text", "text": json.dumps(_OWNER_CARD_RECEIPT)}],
    "tool_use_id": "toolu_1",
  }
  hook_input.update(overrides)
  # The hook is registered with matcher=None, so it is the last PostToolUse
  # callback; the WebSearch one never sees a card tool.
  return await _post_tool_use_hooks(options)[-1](hook_input, "toolu_1", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "sess-1"])
# The card end lands on a TOOL boundary, so its terminal carries stop_reason
# `tool_use`/null — never `interrupt`. Include the `interrupt` value too so the
# historic steer/stop shape stays covered.
@pytest.mark.parametrize("stop_reason", ["tool_use", None, "interrupt"])
async def test_owner_card_hook_cuts_the_turn_before_the_receipt_reaches_the_model(
  monkeypatch, session_id, stop_reason,
):
  """The card-end hook refuses to continue the agent loop, cutting at the card.

  `continue_: False` stops the CLI before it makes the next model request, so
  there is no post-card generation to race and NO interrupt is issued. The
  resulting terminal (observed live: stop_reason `tool_use`, terminal_reason
  `hook_stopped`, is_error False) is classified as a CLEAN completion: no
  requery, no resumable "Paused" note, no error block. On a resume turn the
  `card` owner must NOT masquerade as the synthetic-no-op auto-requery (which
  keys on an interrupt-free clean end) and re-run the original prompt.
  """
  decisions: list[dict] = []

  class _Client(_FakeClient):
    async def receive_response(self):
      if len(self.queries) == 1:
        yield _stream_delta("text_delta", text="here are your options")
        decisions.append(await _fire_card_end_hook(self.options))
        handle = registry.get_handle("card-chat", RunnerKind.CLAUDE_SDK)
        assert handle.owner_card_end is True
        yield _tool_boundary_interrupt_result(
          session_id=session_id or "sess-1", stop_reason=stop_reason,
        )
        return
      raise AssertionError("a card end must never requery")

  clients = _install_fake_client(monkeypatch, _Client)
  bus = _CardBus()
  bus.assistant_blocks = []

  result = await _run_turn(
    "card-chat", bc=bus, prompt="ask me a question", session_id=session_id,
  )

  client = clients[0]
  assert decisions == [
    {"continue_": False, "stopReason": "Saved owner card ends the turn."},
  ]
  # Generation is cut at its source, so nothing is interrupted; the original
  # prompt is the ONLY query — the card end never requeries.
  assert client.interrupts == 0
  assert client.disconnected is True
  assert client.queries == ["ask me a question"]
  # A continuation card is a clean completion, not a resumable pause: any raw
  # provider interruption error is defused (regardless of the tool-boundary
  # stop_reason) and no Resume is offered.
  assert result["error"] is None
  assert result["terminal_status"] == "completed"
  assert "resume_incomplete" not in result
  # The `stopReason` string is a CLI control value, never transcript content.
  assert [e for e in bus.events if e["type"] == "text"] == [
    {"type": "text", "content": "here are your options"},
  ]
  assert not [e for e in bus.events if e["type"] == "error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("label", "overrides"),
  [
    # A tool Möbius never saves a card through — including the Task/Agent
    # result that merely quotes a child agent's receipt.
    ("non_card_tool", {"tool_name": "Agent"}),
    # A receipt-shaped string that is not THIS turn's continuation card.
    (
      "foreign_card",
      {"tool_response": [{"type": "text", "text": json.dumps(
        {**_OWNER_CARD_RECEIPT, "question_id": "card-from-last-week"},
      )}]},
    ),
    # A child agent's own tool: cutting here would end the CHILD, not the turn.
    ("subagent_tool", {"agent_id": "agent-7", "agent_type": "general-purpose"}),
    # Ordinary output with no receipt at all.
    ("no_receipt", {"tool_response": {"stdout": "all tests passed", "stderr": ""}}),
  ],
)
async def test_owner_card_hook_leaves_the_turn_running_without_a_current_card(
  monkeypatch, label, overrides,
):
  """Only the root agent's own card, saved in THIS turn, may end the turn.

  Everything else keeps `continue_: True` so the agent loop runs on — the hook
  must never cut on receipt-shaped text alone.
  """
  decisions: list[dict] = []

  class _Client(_FakeClient):
    async def receive_response(self):
      decisions.append(await _fire_card_end_hook(self.options, **overrides))
      yield _success_result()

  clients = _install_fake_client(monkeypatch, _Client)
  bus = _CardBus()
  bus.assistant_blocks = []

  result = await _run_turn("card-chat-" + label, bc=bus, prompt="work")

  assert decisions == [{"continue_": True}]
  assert clients[0].interrupts == 0
  assert result["error"] is None
  handle_owner = registry.get_handle("card-chat-" + label, RunnerKind.CLAUDE_SDK)
  assert handle_owner is None


@pytest.mark.asyncio
async def test_owner_card_hook_defers_to_a_stop_that_already_owns_the_cut(
  monkeypatch,
):
  """Stop wins: its teardown semantics must survive a card saved in the race.

  The hook leaves the loop running so Stop's own interrupt and pause note own
  the ending, exactly as `begin_finish_after_owner_card` has always deferred.
  """
  decisions: list[dict] = []

  class _Client(_FakeClient):
    async def receive_response(self):
      handle = registry.get_handle("card-stop", RunnerKind.CLAUDE_SDK)
      handle._interrupt_owner = "stop"
      decisions.append(await _fire_card_end_hook(self.options))
      assert handle._interrupt_owner == "stop"
      assert handle.owner_card_end is False
      yield _success_result()

  _install_fake_client(monkeypatch, _Client)
  bus = _CardBus()
  bus.assistant_blocks = []

  await _run_turn("card-stop", bc=bus, prompt="work")

  assert decisions == [{"continue_": True}]


@pytest.mark.asyncio
async def test_owner_card_finish_defers_to_an_owner_that_already_interrupted():
  """A Stop or steer that already owns this turn's cut keeps its
  semantics: a later card commit must not override the owner or fire a second
  interrupt."""
  class _Client:
    def __init__(self):
      self.interrupts = 0

    async def interrupt(self):
      self.interrupts += 1

  client = _Client()
  handle = ActiveClaudeClient(client, chat_id="card-defers")
  # A Stop already owns the cut.
  handle._interrupt_owner = "stop"
  interrupt = handle.begin_finish_after_owner_card()
  if interrupt is not None:
    await interrupt
  assert client.interrupts == 0
  assert handle._interrupt_owner == "stop"
  assert handle.owner_card_end is False


@pytest.mark.asyncio
async def test_child_agent_card_receipt_still_interrupts_through_the_sink_path():
  """A native child agent's card is the one end the card-end hook cannot make.

  Refusing to continue inside a subagent's hook would end the CHILD, so that
  receipt only reaches Möbius later, echoed through its parent Task result.
  `begin_finish_after_owner_card` remains the fallback that ends such a turn.
  """
  class _Client:
    def __init__(self):
      self.interrupts = 0

    async def interrupt(self):
      self.interrupts += 1

  client = _Client()
  handle = ActiveClaudeClient(client, chat_id="child-card")
  interrupt = handle.begin_finish_after_owner_card()
  assert handle.owner_card_end is True
  assert interrupt is not None
  await interrupt
  assert client.interrupts == 1
  # The hook arriving afterwards must not fire a second cut.
  assert handle.claim_owner_card_end() is False


@pytest.mark.asyncio
async def test_owner_card_still_ends_the_turn_after_an_earlier_steer():
  """A steer's cut closes at its terminal; a later saved card must still end
  the turn instead of letting post-card text persist below the card."""
  class _Client:
    def __init__(self):
      self.interrupts = 0

    async def interrupt(self):
      self.interrupts += 1

  client = _Client()
  handle = ActiveClaudeClient(client, chat_id="steer-then-card")
  handle.mark_generating()
  assert await handle.steer("peer note") is True
  assert handle.claim_owner_card_end() is False  # the steer owns this cut

  # A clean terminal that won the race leaves the stray interrupt owned.
  assert handle.take_steer_for_requery(interrupt_landed=False) == ["peer note"]
  assert handle.claim_owner_card_end() is False
  handle.mark_generating()  # the requery is streaming
  await handle.steer("second note")
  assert handle.take_steer_for_requery(interrupt_landed=True) == ["second note"]
  assert handle.pending_steer == []
  assert handle.claim_owner_card_end() is True
  assert handle.owner_card_end is True
  assert client.interrupts == 2


@pytest.mark.asyncio
async def test_stop_stays_sticky_across_a_steer_requery_boundary():
  class _Client:
    async def interrupt(self):
      pass

  handle = ActiveClaudeClient(_Client(), chat_id="stop-sticky")
  await handle.steer("note")
  await handle.interrupt()
  assert handle.take_steer_for_requery(interrupt_landed=True) == []
  assert handle.interrupt_requested is True
  assert handle.claim_owner_card_end() is False


def _assistant_text(text: str, session_id: str = "sess-1") -> AssistantMessage:
  """A completed assistant TEXT block — the clean boundary the runner
  cuts a buffered steer on. (TextBlock is the snapshot of streamed
  text_delta; dispatch leaves it silent, but the AssistantMessage itself
  is the boundary signal the runner watches for.)"""
  return AssistantMessage(
    content=[TextBlock(text=text)],
    model="claude-opus",
    session_id=session_id,
  )


def _success_result(
  session_id: str = "sess-1", cost: float = 0.02,
) -> ResultMessage:
  return ResultMessage(
    subtype="success",
    duration_ms=20,
    duration_api_ms=15,
    is_error=False,
    num_turns=1,
    session_id=session_id,
    stop_reason="end_turn",
    total_cost_usd=cost,
    usage={"input_tokens": 3, "output_tokens": 4},
  )


class _OneStreamClient(_FakeClient):
  """Mimic the SDK: one message stream, `receive_response` stops per result.

  `ClaudeSDKClient.receive_response` is a view over the single connection
  stream that returns after each ResultMessage; calling it again keeps reading
  where it left off. Subclasses define `_messages()`.
  """

  def __init__(self, options):
    super().__init__(options)
    self._stream = iter(self._messages())
    self.read: list[object] = []

  def _messages(self) -> list:
    return [_success_result()]

  async def receive_response(self):
    for message in self._stream:
      self.read.append(message)
      yield message
      if isinstance(message, ResultMessage):
        return

  async def receive_messages(self):
    raise AssertionError("a turn reads its whole stream through one loop")
    yield  # pragma: no cover - keep this an async generator


def _task_started(task_id: str, spawn: str, session: str):
  return TaskStartedMessage(
    subtype="task_started", data={}, task_id=task_id,
    description="inspect the implementation", uuid=f"start-{task_id}",
    session_id=session, tool_use_id=spawn, task_type="local_agent",
  )


def _task_done(task_id: str, spawn: str, session: str):
  return TaskNotificationMessage(
    subtype="task_notification", data={}, task_id=task_id,
    status="completed", output_file=f"/tmp/{task_id}",
    summary="inspection complete", uuid=f"done-{task_id}",
    session_id=session, tool_use_id=spawn,
  )


def _child_frames(spawn: str) -> list:
  # Native child-sidechain frames share the connection but do not own the root
  # chat row. Only their Task lifecycle and the parent synthesis are visible.
  return [
    StreamEvent(
      uuid="child-delta-1", session_id="child-session",
      parent_tool_use_id=spawn,
      event={
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "Child raw report."},
      },
    ),
    AssistantMessage(
      content=[TextBlock(text="Child raw report.")], model="claude-sonnet",
      parent_tool_use_id=spawn, session_id="child-session",
    ),
  ]


async def _ignore_session_persistence(*_args):
  return None


@pytest.mark.asyncio
@pytest.mark.parametrize("settles_before_result", [False, True])
async def test_native_helper_followup_is_read_through_the_same_turn_loop(
  monkeypatch, settles_before_result,
):
  """The parent's post-helper continuation stays inside the one turn loop.

  A turn whose native helper outlives the spawning result is still running:
  the same loop keeps reading until Claude's own follow-up result, in either
  task/result ordering. There is no second reader and no silence deadline.
  """
  helper = [
    *_child_frames("spawn-1"),
    _task_done("agent-1", "spawn-1", "sess-native"),
  ]

  class _Client(_OneStreamClient):
    def _messages(self):
      return [
        _task_started("agent-1", "spawn-1", "sess-native"),
        *(helper if settles_before_result else []),
        _success_result("sess-native", cost=0.01),
        *([] if settles_before_result else helper),
        AssistantMessage(
          content=[TextBlock(text="Parent synthesized the native result.")],
          model="claude-sonnet", session_id="sess-native",
        ),
        _success_result("sess-native", cost=0.03),
      ]

  clients = _install_fake_client(monkeypatch, _Client)
  monkeypatch.setattr(
    claude_sdk_runner, "_persist_session_id", _ignore_session_persistence,
  )
  bus = _Bus()

  result = await _run_turn(
    "native-followup", bc=bus, prompt="delegate this inspection", cwd="/data",
  )

  assert result["cost_usd"] == 0.03
  assert not result.get("error")
  assert len(clients[0].read) == len(clients[0]._messages())
  event_types = [event["type"] for event in bus.events]
  assert event_types.index("task_done") < event_types.index("text_final")
  text_final = next(
    event for event in bus.events if event["type"] == "text_final"
  )
  assert text_final["content"] == "Parent synthesized the native result."
  assert all(
    "Child raw report" not in str(event) for event in bus.events
  )


@pytest.mark.asyncio
async def test_native_agent_completion_already_synthesized_uses_current_result(
  monkeypatch,
):
  """A root response after task completion is already the parent follow-up."""

  class _Client(_OneStreamClient):
    def _messages(self):
      return [
        _task_started("agent-covered", "spawn-covered", "sess-covered"),
        _task_done("agent-covered", "spawn-covered", "sess-covered"),
        AssistantMessage(
          content=[TextBlock(text="Parent already synthesized the result.")],
          model="claude-sonnet", session_id="sess-covered",
        ),
        _success_result("sess-covered", cost=0.02),
        AssistantMessage(
          content=[TextBlock(text="A later turn this one must not read.")],
          model="claude-sonnet", session_id="sess-covered",
        ),
      ]

  clients = _install_fake_client(monkeypatch, _Client)
  monkeypatch.setattr(
    claude_sdk_runner, "_persist_session_id", _ignore_session_persistence,
  )
  bus = _Bus()

  result = await _run_turn(
    "native-followup-already-synthesized", bc=bus,
    prompt="inspect and report", cwd="/data",
  )

  assert result["cost_usd"] == 0.02
  assert len(clients[0].read) == 4
  assert next(
    event for event in bus.events if event["type"] == "text_final"
  )["content"] == "Parent already synthesized the result."


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stream_ends", "stream_raises"])
async def test_helper_phase_ending_early_is_visible_and_keeps_spend(
  monkeypatch, ending,
):
  """A provider that dies mid-helper is reported, never settled as clean.

  The reply before the helper phase is already saved and paid for, so its
  cost and usage survive; only the helper's report back is lost.
  """

  class _Client(_OneStreamClient):
    def _messages(self):
      return [
        _task_started("agent-lost", "spawn-lost", "sess-lost"),
        _success_result("sess-lost", cost=0.01),
      ]

    async def receive_response(self):
      async for message in super().receive_response():
        yield message
      if ending == "stream_raises" and len(self.read) == 2:
        raise RuntimeError("CLI connection lost")

  _install_fake_client(monkeypatch, _Client)
  monkeypatch.setattr(
    claude_sdk_runner, "_persist_session_id", _ignore_session_persistence,
  )

  result = await _run_turn(
    "native-helper-stream-lost", bc=_Bus(), prompt="delegate", cwd="/data",
  )

  if ending == "stream_ends":
    assert result["error"] == claude_sdk_runner._HELPER_REPORT_LOST
  else:
    assert result["error"] == "CLI connection lost"
  assert result["cost_usd"] == 0.01
  assert result["usage"] == {"input_tokens": 3, "output_tokens": 4}


@pytest.mark.asyncio
async def test_stop_during_helper_phase_ends_the_turn_without_the_followup(
  monkeypatch,
):
  """Stop while a helper runs ends the turn at Claude's interrupt result."""
  stops: list[asyncio.Task] = []

  class _Client(_OneStreamClient):
    def _messages(self):
      return [
        _task_started("agent-stop", "spawn-stop", "sess-stop"),
        _success_result("sess-stop", cost=0.01),
        _interrupt_result(),
        AssistantMessage(
          content=[TextBlock(text="A follow-up Stop must not wait for.")],
          model="claude-sonnet", session_id="sess-stop",
        ),
      ]

    async def receive_response(self):
      if self.read:
        handle = registry.get_handle("helper-stop", RunnerKind.CLAUDE_SDK)
        stops.append(asyncio.create_task(handle.interrupt()))
        while not self.interrupts:
          await asyncio.sleep(0)
      async for message in super().receive_response():
        yield message

  clients = _install_fake_client(monkeypatch, _Client)
  monkeypatch.setattr(
    claude_sdk_runner, "_persist_session_id", _ignore_session_persistence,
  )

  result = await _run_turn(
    "helper-stop", bc=_Bus(), prompt="delegate", cwd="/data",
  )
  for stop in stops:
    await stop

  assert result["terminal_status"] == "interrupted"
  assert not result.get("error")
  assert clients[0].interrupts == 1
  assert len(clients[0].read) == 3


def test_native_continuation_defers_only_a_result_not_seen_while_active():
  fast = claude_events.NativeContinuationTracker()
  fast.task_started("fast", "local_agent", "spawn-fast")
  fast.task_finished("fast")
  assert fast.observe_result() is True
  assert fast.observe_result() is False

  covered = claude_events.NativeContinuationTracker()
  covered.task_started("covered", "local_agent", "spawn-covered")
  covered.task_finished("covered")
  covered.root_continuation_observed()
  assert covered.observe_result() is False

  slow = claude_events.NativeContinuationTracker()
  slow.task_started("slow", "local_agent", "spawn-slow")
  assert slow.observe_result() is True
  slow.task_finished("slow")
  assert slow.observe_result() is False


def _interrupt_result(session_id: str = "sess-1") -> ResultMessage:
  """The terminal an SDK interrupt produces — error_during_execution."""
  return ResultMessage(
    subtype="error_during_execution",
    duration_ms=10,
    duration_api_ms=5,
    is_error=True,
    num_turns=1,
    session_id=session_id,
    stop_reason="interrupt",
    total_cost_usd=0.01,
    usage={"input_tokens": 1, "output_tokens": 2},
  )


async def _run_claude_stop_outcome(monkeypatch, mode: str, *, owned: bool):
  """Run one fake response stream, with an owner Stop in flight when `owned`.

  The Stop goes through the public `interrupt()`: it records ownership before
  its first await, signals the client, then waits for the runner to finish —
  so the stream resumes once the client has seen the interrupt and the Stop
  task is collected after the turn resolves `_finished`.
  """
  process_error = ProcessError(
    f"Command failed with exit code {'1' if mode == 'process_failure' else '-15'}",
    exit_code=1 if mode == "process_failure" else -15,
    stderr="Check stderr output for details",
  )
  stops: list[asyncio.Task] = []

  class _Client(_FakeClient):
    def __init__(self, options):
      super().__init__(options)
      self._transport = type("Transport", (), {"_process": None})()

    async def receive_response(self):
      if owned:
        handle = registry.get_handle("claude-stop-shape", RunnerKind.CLAUDE_SDK)
        assert handle is not None
        stops.append(asyncio.create_task(handle.interrupt()))
        while not self.interrupts:
          await asyncio.sleep(0)
      if mode == "terminal":
        yield _interrupt_result()
        return
      if mode == "resultless":
        return
      if mode in ("process_error", "process_failure"):
        raise process_error
      if mode == "other_error":
        raise ValueError("unexpected notification payload")
      raise AssertionError(mode)

  _install_fake_client(monkeypatch, _Client)
  result = await _run_turn("claude-stop-shape")
  for stop in stops:
    await stop
  return result


@pytest.mark.asyncio
async def test_claude_interrupt_marks_owner_request_before_sdk_await():
  observed = []

  class _Client:
    async def interrupt(self):
      observed.append(handle.interrupt_requested)

  handle = ActiveClaudeClient(_Client(), chat_id="claude-owned-stop")
  task = asyncio.create_task(handle.interrupt())
  while not observed:
    await asyncio.sleep(0)
  handle.mark_finished()
  await task

  assert observed == [True]


def test_claude_force_stop_check_uses_public_process_exit_codes():
  assert claude_sdk_runner._claude_process_was_force_stopped(
    ProcessError("CLI terminated", exit_code=-15)
  ) is True
  assert claude_sdk_runner._claude_process_was_force_stopped(
    ProcessError("CLI killed", exit_code=-9)
  ) is True
  assert claude_sdk_runner._claude_process_was_force_stopped(
    ProcessError("CLI failed", exit_code=1)
  ) is False


def test_structured_result_error_is_not_rewritten_with_stderr_tail():
  error = ResultError(
    "Claude returned a structured API failure",
    data={"result": "API Error: overloaded"},
    exit_code=1,
  )
  assert claude_sdk_runner._process_error_with_stderr_tail(
    error,
    deque(["unrelated local stderr"]),
  ) == str(error)


def test_unstructured_process_error_gets_only_the_bounded_stderr_tail():
  error = ProcessError(
    "Command failed with exit code 1",
    exit_code=1,
    stderr="Check stderr output for details",
  )
  assert claude_sdk_runner._process_error_with_stderr_tail(
    error,
    deque(["first", "last"], maxlen=2),
  ).endswith("stderr (tail):\nfirst\nlast")


@pytest.mark.asyncio
async def test_owner_stop_turns_claude_interrupt_result_into_clean_terminal(
  monkeypatch,
):
  result = await _run_claude_stop_outcome(monkeypatch, "terminal", owned=True)

  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"
  assert result["cost_usd"] == 0.01
  assert result["usage"] == {"input_tokens": 1, "output_tokens": 2}


@pytest.mark.asyncio
async def test_interrupt_result_we_never_issued_stays_an_error(monkeypatch):
  # No steer or Stop of ours issued an interrupt, so a `stop_reason ==
  # "interrupt"` terminal (a CLI/provider-side abort mapped to the same
  # envelope) must stay a visible error, never a calm resumable "Paused" note.
  result = await _run_claude_stop_outcome(monkeypatch, "terminal", owned=False)

  assert result["error"] == "Execution interrupted."
  assert result.get("terminal_status") is None
  assert result.get("resume_incomplete") is None


@pytest.mark.asyncio
async def test_owner_stop_accepts_resultless_claude_stream_as_interrupted(
  monkeypatch,
):
  result = await _run_claude_stop_outcome(
    monkeypatch, "resultless", owned=True,
  )

  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"


@pytest.mark.asyncio
async def test_unrequested_resultless_claude_stream_stays_an_error(monkeypatch):
  result = await _run_claude_stop_outcome(
    monkeypatch, "resultless", owned=False,
  )

  assert "ended unexpectedly" in result["error"]
  assert result.get("terminal_status") is None


@pytest.mark.asyncio
async def test_owner_stop_reclassifies_typed_claude_process_exit(
  monkeypatch, caplog,
):
  result = await _run_claude_stop_outcome(
    monkeypatch, "process_error", owned=True,
  )

  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"
  assert any(
    record.levelname == "WARNING"
    and "Claude process exited during our own stop" in record.message
    for record in caplog.records
  )


@pytest.mark.asyncio
async def test_unrequested_claude_process_exit_stays_an_error(monkeypatch):
  result = await _run_claude_stop_outcome(
    monkeypatch, "process_error", owned=False,
  )

  assert "Command failed with exit code -15" in result["error"]
  assert result.get("terminal_status") is None


@pytest.mark.asyncio
async def test_owner_stop_does_not_hide_unrelated_claude_failure(monkeypatch):
  result = await _run_claude_stop_outcome(
    monkeypatch, "other_error", owned=True,
  )

  assert result["error"] == "unexpected notification payload"
  assert result.get("terminal_status") is None


@pytest.mark.asyncio
async def test_owner_stop_does_not_hide_other_claude_process_failure(monkeypatch):
  result = await _run_claude_stop_outcome(
    monkeypatch, "process_failure", owned=True,
  )

  assert "exit code 1" in result["error"]
  assert result.get("terminal_status") is None


@pytest.mark.asyncio
async def test_steer_interrupts_immediately_not_deferred_to_boundary(
  monkeypatch,
):
  """THE core contract: a steer requested mid-turn interrupts the live turn
  IMMEDIATELY — it does NOT wait for the next completed content block. The
  cut lands as soon as the steer is requested (matching Codex's immediate
  steer), then the interrupt's terminal result re-queries exactly once on
  the same client.

  The fake stream records the interrupt-call count at the moment each
  message is dispatched, so the test can assert the interrupt fired the
  instant the steer arrived (mid-delta), not at a later boundary."""
  # (message_label, interrupts_observed_when_this_message_was_yielded)
  interrupt_trace: list[tuple[str, int]] = []

  class _Client(_FakeClient):
    async def receive_response(self):
      if len(self.queries) == 1:
        # First turn: deltas stream, the user steers mid-block — the
        # interrupt fires RIGHT THEN, so the count is already 1 on the
        # very next delta (no waiting for a completed block).
        yield _stream_delta("text_delta", text="thinking ")
        interrupt_trace.append(("delta-1", self.interrupts))
        assert await steer_into_active_turn("boundary-chat", "use blue") \
          is True
        yield _stream_delta("text_delta", text="about it")
        interrupt_trace.append(("delta-2-after-steer", self.interrupts))
        # A completed block still arrives (a few tokens can stream before
        # the interrupt takes effect); it must NOT fire a second interrupt.
        yield _assistant_text("thinking about it")
        interrupt_trace.append(("assistant-boundary", self.interrupts))
        # The interrupt's terminal result. The runner's drain-then-
        # requery path delivers the buffered steer here.
        yield _interrupt_result()
        return
      # Second (re-queried) turn completes normally.
      yield _stream_delta("text_delta", text="blue done")
      yield _success_result()

  clients = _install_fake_client(monkeypatch, _Client)
  bus = _ChatBus()
  result = await _run_turn("boundary-chat", bc=bus, prompt="start task")

  client = clients[0]
  trace = dict(interrupt_trace)
  # No interrupt before the steer was requested.
  assert trace["delta-1"] == 0
  # The interrupt fired the instant the steer arrived — the next delta
  # already sees it (this is the whole point of the change).
  assert trace["delta-2-after-steer"] == 1
  # Exactly once — the later completed block did not double-interrupt.
  assert client.interrupts == 1
  # Exactly one re-query with the buffered steer (no double).
  assert client.queries[0] == "start task"
  assert len(client.queries) == 2
  assert client.queries[1].startswith(
    "New context arrived while you were working."
  )
  assert "use blue" in client.queries[1]
  assert result["error"] is None
  assert result["cost_usd"] == 0.02
  # The finished sentence the user saw before the cut, then the steered
  # continuation — in order, each emitted once.
  assert [e for e in bus.events if e["type"] == "text"] == [
    {"type": "text", "content": "thinking "},
    {"type": "text", "content": "about it"},
    {"type": "text", "content": "blue done"},
  ]


@pytest.mark.asyncio
async def test_steer_interrupts_once_despite_two_rapid_steers(monkeypatch):
  """Two rapid steers before the interrupt's terminal ResultMessage must
  fire only ONE interrupt — `_interrupt_in_flight` guards the single cut —
  and both buffered steers ride the single requery (FIFO, exactly once).
  Later completed blocks arriving in the drain window must not re-interrupt."""
  class _Client(_FakeClient):
    async def receive_response(self):
      if len(self.queries) == 1:
        # Two rapid steers: the first fires the interrupt, the second is
        # guarded by _interrupt_in_flight and only buffers its text.
        assert await steer_into_active_turn("multi-chat", "use blue") is True
        assert await steer_into_active_turn("multi-chat", "and bold") is True
        yield _assistant_text("first block")
        await asyncio.sleep(0)  # the claimed cut is sent once streaming
        # The SDK may still emit trailing completed blocks in the drain
        # window before the interrupt's terminal lands. They must NOT cause
        # a second interrupt.
        yield _assistant_text("straggler block")
        yield _interrupt_result()
        return
      yield _stream_delta("text_delta", text="done")
      yield _success_result()

  clients = _install_fake_client(monkeypatch, _Client)
  result = await _run_turn("multi-chat", prompt="start task")

  client = clients[0]
  # Exactly one interrupt despite two steers and two completed blocks in the
  # drain window — the in-flight guard held.
  assert client.interrupts == 1
  # Exactly one requery, carrying BOTH buffered steers in FIFO order.
  assert len(client.queries) == 2
  assert "use blue" in client.queries[1]
  assert "and bold" in client.queries[1]
  assert client.queries[1].index("use blue") < client.queries[1].index(
    "and bold"
  )
  assert result["error"] is None


@pytest.mark.asyncio
async def test_steer_after_content_already_streamed(monkeypatch):
  """A steer requested after some content has already streamed still
  interrupts immediately and re-queries once on the same client."""
  class _Client(_FakeClient):
    async def receive_response(self):
      if len(self.queries) == 1:
        yield _assistant_text("first block")
        # Steer arrives after the first block already streamed; it fires
        # the interrupt immediately all the same.
        assert await steer_into_active_turn("late-chat", "pivot") is True
        yield _assistant_text("second block")
        yield _interrupt_result()
        return
      yield _stream_delta("text_delta", text="pivoted")
      yield _success_result()

  clients = _install_fake_client(monkeypatch, _Client)
  result = await _run_turn("late-chat", prompt="start task")

  client = clients[0]
  assert client.interrupts == 1
  assert len(client.queries) == 2
  assert "pivot" in client.queries[1]
  assert result["error"] is None


@pytest.mark.asyncio
async def test_stop_drops_buffered_steer(monkeypatch):
  """Stop is the hard teardown path: after a steer has already fired its own
  soft interrupt, a Stop drops the buffered steer entirely (no requery for
  abandoned work) and fires its own immediate interrupt on top."""
  del monkeypatch  # this handle-level test needs no SDK patching

  calls: list[str] = []

  class _Client:
    async def interrupt(self):
      calls.append("interrupt")

  handle = ActiveClaudeClient(_Client(), chat_id="stop-chat")
  handle.mark_generating()
  registry.register(handle)
  try:
    # Steer buffers the text and fires its soft interrupt immediately.
    assert await steer_into_active_turn("stop-chat", "use blue") is True
    assert handle.pending_steer == ["use blue"]
    assert calls == ["interrupt"]

    # mark_finished so interrupt()'s _finished wait returns immediately.
    handle.mark_finished()
    await handle.interrupt()

    # Stop always cuts immediately — a second interrupt on top of the
    # steer's — and clears the buffer so no requery fires.
    assert calls == ["interrupt", "interrupt"]
    assert handle.pending_steer == []
  finally:
    registry.unregister("stop-chat", handle.kind)


@pytest.mark.asyncio
async def test_stop_timeout_preserves_runner_completion_future():
  class _Client:
    async def interrupt(self):
      return None

  handle = ActiveClaudeClient(_Client(), chat_id="timeout-identity")

  assert await handle.stop(timeout=0.01) is False
  assert handle._finished.done() is False
  handle.mark_finished()
  assert handle._finished.done() is True


@pytest.mark.asyncio
async def test_force_stop_signals_claude_group_only_once(monkeypatch):
  calls: list[int] = []
  monkeypatch.setattr(
    claude_sdk_runner,
    "_terminate_claude_process_group",
    lambda pgid: calls.append(pgid) or True,
  )

  class _Client:
    async def interrupt(self):
      return None

  handle = ActiveClaudeClient(_Client(), chat_id="hard-stop")
  handle.set_process_group_id(4321)
  first = asyncio.create_task(handle.force_stop(timeout=1))
  while not calls:
    await asyncio.sleep(0)
  handle.mark_finished()

  assert await first is True
  assert await handle.force_stop(timeout=1) is True
  assert calls == [4321]


def test_run_claude_sdk_turn_persists_session_id_before_terminal_result(
  monkeypatch,
):
  """Claude session ids are durable as soon as the stream reveals them."""
  class _Client(_FakeClient):
    async def receive_response(self):
      yield StreamEvent(
        uuid="evt-session",
        session_id="sess-early",
        event={
          "type": "content_block_delta",
          "delta": {"type": "text_delta", "text": "still running"},
        },
      )
      yield _success_result("sess-early")

  _install_fake_client(monkeypatch, _Client)

  db = SessionLocal()
  try:
    db.add(models.Chat(
      id="claude-early",
      title="t",
      messages=[],
      pending_messages=[],
      provider="claude",
      session_id=None,
    ))
    db.commit()

    result = asyncio.run(_run_turn("claude-early", db=db))

    assert result["session_id"] == "sess-early"
    db.expire_all()
    chat = db.query(models.Chat).filter(
      models.Chat.id == "claude-early"
    ).first()
    assert chat.session_id == "sess-early"
    # The same sighting also records the append-only session->chat link, so the
    # id resolves back to this chat even after a later switch NULLs
    # Chat.session_id.
    link = db.get(models.ChatSessionLink, ("claude", "sess-early"))
    assert link is not None
    assert link.chat_id == "claude-early"
    assert link.first_seen_at == link.last_seen_at
  finally:
    db.close()


def test_dispatch_text_delta_emits_text():
  bus = _Bus()
  msg = _stream_delta("text_delta", text="hello")
  new_sid, terminal = dispatch_sdk_message(msg, bus, None)
  assert terminal is None
  assert new_sid == "sess-1"
  assert bus.events == [{"type": "text", "content": "hello"}]


def test_dispatch_thinking_delta_emits_thinking(monkeypatch):
  monkeypatch.setattr(claude_events.time, "time", lambda: 1.234)
  bus = _Bus()
  msg = _stream_delta("thinking_delta", thinking="planning...")
  msg.event["index"] = 2
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{
    "type": "thinking",
    "content": "planning...",
    "ts": 1234,
    "segment_id": "claude:content:2",
  }]


def test_control_mcp_readiness_waits_for_every_platform_tool():
  from app import claude_sdk_runner
  from app.platform_tools import CONTROL_TOOL_NAMES

  class _Client:
    def __init__(self):
      self.calls = 0

    async def get_mcp_status(self):
      self.calls += 1
      if self.calls == 1:
        return {"mcpServers": [{
          "name": "mobius_control",
          "status": "pending",
        }]}
      if self.calls == 2:
        return {"mcpServers": [{
          "name": "mobius_control",
          "status": "connected",
          "tools": [{"name": "promote_goal"}],
        }]}
      from app.platform_tools import CONTROL_TOOL_NAMES
      return {"mcpServers": [{
        "name": "mobius_control",
        "status": "connected",
        "tools": [{"name": name} for name in CONTROL_TOOL_NAMES],
      }]}

  client = _Client()
  assert asyncio.run(
    claude_sdk_runner._await_control_mcp_ready(client, enabled=True)
  ) is None
  assert client.calls == 3


def test_control_mcp_readiness_accepts_delegated_coordination_subset():
  from app import claude_sdk_runner
  from app.platform_tools import DELEGATED_CONTROL_TOOL_NAMES

  class _Client:
    async def get_mcp_status(self):
      return {"mcpServers": [{
        "name": "mobius_control",
        "status": "connected",
        "tools": [{"name": name} for name in DELEGATED_CONTROL_TOOL_NAMES],
      }]}

  assert asyncio.run(claude_sdk_runner._await_control_mcp_ready(
    _Client(), enabled=True, expected_tool_names=DELEGATED_CONTROL_TOOL_NAMES,
  )) is None


def test_control_mcp_readiness_reports_terminal_failure_without_retrying():
  from app import claude_sdk_runner

  class _Client:
    async def get_mcp_status(self):
      return {"mcpServers": [{
        "name": "mobius_control",
        "status": "failed",
        "error": "stdio child exited",
      }]}

  assert asyncio.run(
    claude_sdk_runner._await_control_mcp_ready(_Client(), enabled=True)
  ) == "failed: stdio child exited"


def test_control_mcp_readiness_is_absent_for_restricted_turns():
  from app import claude_sdk_runner

  class _Client:
    async def get_mcp_status(self):
      raise AssertionError("restricted turns must not inspect owner controls")

  assert asyncio.run(
    claude_sdk_runner._await_control_mcp_ready(_Client(), enabled=False)
  ) is None


def test_claude_thinking_config_requests_summarized_adaptive_thinking():
  assert claude_sdk_runner._claude_thinking_config("claude-opus-4-8") == {
    "type": "adaptive",
    "display": "summarized",
  }
  assert claude_sdk_runner._claude_thinking_config(
    "claude-sonnet-4-6"
  ) == {
    "type": "adaptive",
    "display": "summarized",
  }
  assert claude_sdk_runner._claude_thinking_config(None) == {
    "type": "adaptive",
    "display": "summarized",
  }
  assert (
    claude_sdk_runner._claude_thinking_config("claude-opus-4-5-20251101")
    is None
  )


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "sess-1"])
async def test_claude_new_and_resumed_turns_exclude_native_owner_questions(
  monkeypatch, session_id,
):
  clients = _install_fake_client(monkeypatch)
  await _run_turn(
    "chat-owner-question", bc=_Bus(), cwd="/data", session_id=session_id,
  )
  assert {"AskUserQuestion", "request_user_input"} <= set(
    clients[0].options.disallowed_tools
  )


@pytest.mark.asyncio
async def test_run_claude_sdk_turn_requests_summarized_thinking(monkeypatch):
  clients = _install_fake_client(monkeypatch)

  await _run_turn(
    "chat-thinking", bc=_Bus(), cwd="/data",
    agent_settings={"model": "claude-opus-4-8", "effort": "high"},
  )

  options = clients[0].options
  assert options.model == "claude-opus-4-8"
  assert options.effort == "high"
  # The Claude runner appends its provider-authored concise register on top of
  # the shared base (documented amendment to system_prompts.py's contract): the
  # shared base is preserved verbatim, with the register appended after it.
  assert options.system_prompt.startswith(
    claude_sdk_runner._system_prompt_with_register("system")
  )
  assert "$MOBIUS_GENERATED_DIR" in options.system_prompt
  assert options.system_prompt.startswith("system")
  assert "# Concise register" in options.system_prompt
  assert "# Execution lifetimes in Möbius" in options.system_prompt
  assert "TaskOutput" not in options.system_prompt
  assert 'until [ -e "$TMPDIR/job.exit" ]' in options.system_prompt
  assert "confirm its saved receipt" in options.system_prompt
  assert options.max_buffer_size == 10 * 1024 * 1024
  assert set(claude_sdk_runner._CLAUDE_NATIVE_SCHEDULING_TOOLS) <= set(
    options.disallowed_tools
  )
  assert set(claude_sdk_runner._CLAUDE_NATIVE_OWNER_INPUT_TOOLS) <= set(
    options.disallowed_tools
  )
  assert set(claude_sdk_runner._CLAUDE_UNUSED_BUILTINS) <= set(
    options.disallowed_tools
  )
  assert options.thinking == {
    "type": "adaptive",
    "display": "summarized",
  }


@pytest.mark.asyncio
async def test_precompact_hook_publishes_context_compaction_marker(monkeypatch):
  clients = _install_fake_client(monkeypatch)
  bus = _Bus()
  await _run_turn("chat-compaction", bc=bus, cwd="/data")

  matcher = clients[0].options.hooks["PreCompact"][0]
  result = await matcher.hooks[0]({"trigger": "manual"}, None, {})

  assert result == {"continue_": True}
  assert {
    "type": "context_compacted",
    "provider": "claude",
    "trigger": "manual",
  } in bus.events


def test_unhandled_sdk_events_are_logged_not_broadcast(caplog):
  """No client renders unhandled provider events. Broadcasting them made the
  per-token progress events (thinking-token counters, tool-input fragments)
  most of a busy chat's stream and of every reconnect replay."""

  class FreshSdkMessage:  # Stand-in for a hypothetical future SDK type.
    pass

  bus = _Bus()
  with caplog.at_level("DEBUG", logger="app.claude_events"):
    for message in (
      _stream_delta("input_json_delta", partial_json="{\"a\":"),
      SystemMessage(subtype="thinking_tokens", data={"estimated_tokens": 1}),
      FreshSdkMessage(),
    ):
      dispatch_sdk_message(message, bus, None)

  assert bus.events == []
  logged = caplog.text
  assert "stream:content_block_delta:input_json_delta" in logged
  assert "system:thinking_tokens" in logged
  assert "sdk_message:FreshSdkMessage" in logged


def test_dispatch_assistant_thinking_block_is_silent():
  """ThinkingBlock is a snapshot duplicate of streamed thinking_delta —
  must not re-emit as thinking to avoid doubling the content."""
  bus = _Bus()
  msg = AssistantMessage(
    content=[ThinkingBlock(thinking="reflecting", signature="sig")],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


def test_dispatch_assistant_tool_use_emits_tool_start():
  bus = _Bus()
  msg = AssistantMessage(
    content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  types = [e["type"] for e in bus.events]
  assert "tool_start" in types


def test_child_sidechain_messages_never_enter_the_owner_assistant_row():
  """parent_tool_use_id is Claude's root-vs-child ownership boundary.

  Child deltas, completed messages, tools, and tool results share the SDK
  connection but must not emit root events, replace the root message identity,
  or advance its resumable session.
  """
  bus = _Bus()
  bus.current_message_id = "root-message"
  current_session_id = "root-session"
  child_messages = [
    StreamEvent(
      uuid="child-start",
      session_id="child-session",
      parent_tool_use_id="spawn-1",
      event={"type": "message_start", "message": {"id": "child-message"}},
    ),
    StreamEvent(
      uuid="child-delta",
      session_id="child-session",
      parent_tool_use_id="spawn-1",
      event={
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "child prose"},
      },
    ),
    AssistantMessage(
      content=[
        TextBlock(text="child prose"),
        ToolUseBlock(id="child-bash", name="Bash", input={"command": "pwd"}),
      ],
      model="claude-sonnet",
      parent_tool_use_id="spawn-1",
      message_id="child-message",
      session_id="child-session",
      usage={"input_tokens": 10, "output_tokens": 5},
      stop_reason="tool_use",
    ),
    UserMessage(
      content=[ToolResultBlock(tool_use_id="child-bash", content="/tmp")],
      parent_tool_use_id="spawn-1",
    ),
  ]

  for message in child_messages:
    current_session_id, terminal = dispatch_sdk_message(
      message, bus, current_session_id,
    )
    assert terminal is None

  assert current_session_id == "root-session"
  assert bus.current_message_id == "root-message"
  assert bus.events == []


def test_dispatch_claude_edit_carries_shared_diff_preview(tmp_path):
  path = tmp_path / "app.py"
  bus = _Bus()
  msg = AssistantMessage(
    content=[ToolUseBlock(id="edit-1", name="Edit", input={
      "file_path": str(path),
      "old_string": "before",
      "new_string": "after",
    })],
    model="claude-opus",
  )

  dispatch_sdk_message(msg, bus, None)

  assert [event["type"] for event in bus.events] == ["tool_start", "tool_input"]
  assert bus.events[1]["input"] == str(path)
  assert "-before\n+after" in bus.events[1]["edit_preview"]["diff"]


def test_failed_claude_edit_result_carries_explicit_failure_status():
  bus = _Bus()
  msg = UserMessage(content=[ToolResultBlock(
    tool_use_id="edit-1",
    content="old_string was not found",
    is_error=True,
  )])

  dispatch_sdk_message(msg, bus, None)

  assert bus.events[0] == {
    "type": "tool_output",
    "content": "old_string was not found",
    "tool_use_id": "edit-1",
    "output_complete": True,
    "output_exit_code": 1,
  }
  assert bus.events[1] == {"type": "tool_end", "tool_use_id": "edit-1"}


def test_dispatch_skill_tool_emits_skill_loaded_and_logs(monkeypatch):
  """A Skill tool_use emits a skill_loaded event AFTER its tool_start
  and appends one skill_loaded record to the activity log."""
  from app import activity

  logged: list[tuple] = []
  monkeypatch.setattr(
    activity, "log_skill_load",
    lambda chat_id, skill, ts=None: logged.append((chat_id, skill)),
  )

  class _ChatBus(_Bus):
    chat_id = "chat-42"

  bus = _ChatBus()
  msg = AssistantMessage(
    content=[ToolUseBlock(id="s1", name="Skill", input={"skill": "humanizer"})],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  types = [e["type"] for e in bus.events]
  # tool_start fires first, then the targeted skill-loaded receipt.
  assert types == ["tool_start", "tool_input", "skill_loaded"]
  loaded = [e for e in bus.events if e["type"] == "skill_loaded"]
  assert loaded == [{
    "type": "skill_loaded", "skill": "humanizer", "tool_use_id": "s1",
  }]
  assert logged == [("chat-42", "humanizer")]


def test_dispatch_skill_tool_without_name_does_not_emit(monkeypatch):
  """A Skill tool_use with no resolvable name emits no receipt or log."""
  from app import activity

  logged: list[tuple] = []
  monkeypatch.setattr(
    activity, "log_skill_load",
    lambda chat_id, skill, ts=None: logged.append((chat_id, skill)),
  )
  bus = _Bus()
  msg = AssistantMessage(
    content=[ToolUseBlock(id="s2", name="Skill", input={})],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert [e["type"] for e in bus.events if e["type"] == "skill_loaded"] == []
  assert logged == []


def test_dispatch_non_skill_tool_emits_no_skill_loaded(monkeypatch):
  """A non-Skill tool never produces a skill_loaded event."""
  from app import activity

  monkeypatch.setattr(
    activity, "log_skill_load",
    lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not log")),
  )
  bus = _Bus()
  msg = AssistantMessage(
    content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert [e for e in bus.events if e["type"] == "skill_loaded"] == []


def test_dispatch_assistant_text_block_emits_text_final():
  """TextBlock is the AUTHORITATIVE full text of the just-completed item; it is
  emitted as a replace-semantics `text_final` (NOT a plain `text`, which the
  reducer would concatenate and double the content). events.py overwrites the
  streamed block with it, repairing any dropped delta."""
  bus = _Bus()
  msg = AssistantMessage(
    content=[TextBlock(text="hello")],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{"type": "text_final", "content": "hello"}]


def test_dispatch_assistant_empty_text_block_is_silent():
  """An empty TextBlock emits nothing — no point publishing a no-op replace."""
  bus = _Bus()
  msg = AssistantMessage(
    content=[TextBlock(text="")],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


def test_each_root_model_call_publishes_its_live_context_occupancy():
  bus = _Bus()
  msg = AssistantMessage(
    content=[],
    model="claude-opus",
    usage={
      "input_tokens": 10,
      "cache_creation_input_tokens": 200,
      "cache_read_input_tokens": 3_000,
      "output_tokens": 5,
    },
  )
  dispatch_sdk_message(msg, bus, None)
  context = [e for e in bus.events if e["type"] == "context_usage"]
  # Uncached + cache-write + cache-read: the same figure the settled run
  # records as latest_model_input_tokens.
  assert context == [{
    "type": "context_usage",
    "provider": "claude",
    "input_tokens": 3_210,
    "context_window": None,
  }]


def test_dispatch_assistant_stop_reason():
  bus = _Bus()
  msg = AssistantMessage(
    content=[], model="claude-opus", stop_reason="end_turn",
  )
  dispatch_sdk_message(msg, bus, None)
  stops = [e for e in bus.events if e["type"] == "stop_reason"]
  assert stops == [{"type": "stop_reason", "reason": "end_turn"}]


def test_dispatch_user_tool_result():
  bus = _Bus()
  msg = UserMessage(
    content=[ToolResultBlock(tool_use_id="t1", content="output text")],
  )
  dispatch_sdk_message(msg, bus, None)
  types = [e["type"] for e in bus.events]
  assert "tool_output" in types
  assert "tool_end" in types


def test_dispatch_user_tool_result_threads_tool_use_id():
  # The ToolResultBlock's tool_use_id (contract rule 6) rides both the
  # tool_output and tool_end events so the sink can key a stash of a large
  # output and the block can fetch it by id.
  bus = _Bus()
  dispatch_sdk_message(
    UserMessage(
      content=[ToolResultBlock(tool_use_id="tu_abc", content="output text")],
    ),
    bus,
    None,
  )
  by_type = {e["type"]: e for e in bus.events}
  assert by_type["tool_output"]["tool_use_id"] == "tu_abc"
  assert by_type["tool_end"]["tool_use_id"] == "tu_abc"


def test_dispatch_server_web_search_result_emits_sources():
  bus = _Bus()
  msg = AssistantMessage(
    content=[
      ServerToolUseBlock(
        id="srv-1",
        name="web_search",
        input={"query": "mobius docs"},
      ),
      ServerToolResultBlock(
        tool_use_id="srv-1",
        content={
          "type": "web_search_tool_result",
          "content": [{
            "title": "Mobius",
            "url": "https://example.com/mobius",
            "snippet": "Project page",
          }],
        },
      ),
    ],
    model="claude-opus",
  )

  dispatch_sdk_message(msg, bus, None)

  assert bus.events == [
    {
      "type": "tool_start", "tool": "WebSearch", "input": "mobius docs",
      "tool_use_id": "srv-1",
    },
    {"type": "tool_sources", "sources": [{
      "title": "Mobius",
      "url": "https://example.com/mobius",
      "snippet": "Project page",
    }], "tool_use_id": "srv-1"},
    {"type": "tool_end", "tool_use_id": "srv-1"},
  ]


def test_dispatch_batched_server_web_search_results_keep_their_ids():
  bus = _Bus()
  msg = AssistantMessage(
    content=[
      ServerToolUseBlock(
        id="srv-a", name="web_search", input={"query": "query A"},
      ),
      ServerToolUseBlock(
        id="srv-b", name="web_search", input={"query": "query B"},
      ),
      ServerToolResultBlock(
        tool_use_id="srv-a",
        content={"type": "web_search_tool_result", "content": [{
          "title": "A", "url": "https://a.example/result",
        }]},
      ),
      ServerToolResultBlock(
        tool_use_id="srv-b",
        content={"type": "web_search_tool_result", "content": [{
          "title": "B", "url": "https://b.example/result",
        }]},
      ),
    ],
    model="claude-opus",
  )

  dispatch_sdk_message(msg, bus, None)

  source_events = [event for event in bus.events
                   if event["type"] == "tool_sources"]
  assert [(event["tool_use_id"], event["sources"][0]["title"])
          for event in source_events] == [("srv-a", "A"), ("srv-b", "B")]
  end_events = [event for event in bus.events if event["type"] == "tool_end"]
  assert [event["tool_use_id"] for event in end_events] == ["srv-a", "srv-b"]


def test_dispatch_client_web_search_tool_result_emits_sources():
  bus = _Bus()
  result_text = (
    "Web search results for query: \"mobius docs\"\n\n"
    "Links: [{\"title\":\"Mobius\",\"url\":\"https://example.com/mobius\","
    "\"snippet\":\"Project page\"},{\"title\":\"Docs\","
    "\"url\":\"https://example.com/docs\"}]\n\n"
    "Summary text continues after the links."
  )

  dispatch_sdk_message(
    AssistantMessage(
      content=[ToolUseBlock(id="t1", name="WebSearch", input={
        "query": "mobius docs",
      })],
      model="claude-opus",
    ),
    bus,
    None,
  )
  dispatch_sdk_message(
    UserMessage(
      content=[ToolResultBlock(tool_use_id="t1", content=result_text)],
    ),
    bus,
    None,
  )

  assert bus.events == [
    {"type": "tool_start", "tool": "WebSearch", "input": "", "tool_use_id": "t1"},
    {"type": "tool_input", "tool": "WebSearch", "input": "mobius docs",
     "tool_use_id": "t1"},
    {
      "type": "tool_output", "content": result_text, "tool_use_id": "t1",
      "output_complete": True,
    },
    # tool_use_id binds these sources to the search that produced them, so a
    # batch of parallel WebSearch calls does not collapse onto one block.
    {"type": "tool_sources", "tool_use_id": "t1", "sources": [
      {
        "title": "Mobius",
        "url": "https://example.com/mobius",
        "snippet": "Project page",
      },
      {"title": "Docs", "url": "https://example.com/docs"},
    ]},
    {"type": "tool_end", "tool_use_id": "t1"},
  ]


def test_dispatch_rate_limit_event():
  bus = _Bus()
  info = RateLimitInfo(status="allowed_warning", resets_at=12345)
  msg = RateLimitEvent(
    rate_limit_info=info, uuid="rl-1", session_id="sess-1",
  )
  dispatch_sdk_message(msg, bus, None)
  assert len(bus.events) == 1
  assert bus.events[0]["type"] == "rate_limit"
  assert bus.events[0]["status"] == "allowed_warning"
  assert bus.events[0]["resets_at"] == 12345


def test_dispatch_task_started():
  bus = _Bus()
  msg = TaskStartedMessage(
    subtype="task_started",
    data={},
    task_id="t-1",
    description="build app",
    uuid="u-1",
    session_id="sess-1",
    tool_use_id="tu_spawn",
    task_type="build",
  )
  dispatch_sdk_message(msg, bus, None)
  # tool_use_id rides task_start so a consumer can nest the sub-task under the
  # parent turn's tool call.
  assert bus.events == [{
    "type": "task_start",
    "task_id": "t-1",
    "description": "build app",
    "task_type": "build",
    "tool_use_id": "tu_spawn",
  }]
  assert bus.lifecycle_events[0]["provider_session_id"] == "sess-1"
  assert bus.lifecycle_events[0]["source_event_id"] == "u-1"


def test_dispatch_task_started_accepts_sdk_shape_without_identity_attrs(
  monkeypatch,
):
  class MinimalTaskStarted:
    subtype = "task_started"
    task_id = "t-minimal"
    description = "inspect"
    task_type = "explore"
    tool_use_id = None

  monkeypatch.setattr(claude_events, "SystemMessage", MinimalTaskStarted)
  monkeypatch.setattr(
    claude_events, "TaskStartedMessage", MinimalTaskStarted,
  )
  bus = _Bus()
  dispatch_sdk_message(MinimalTaskStarted(), bus, "known-session")
  assert "provider_session_id" not in bus.events[0]
  assert bus.lifecycle_events[0]["provider_session_id"] == "known-session"
  assert bus.lifecycle_events[0]["source_event_id"] is None


def test_dispatch_task_progress():
  bus = _Bus()
  msg = TaskProgressMessage(
    subtype="task_progress",
    data={},
    task_id="t-1",
    description="building",
    usage={"total_tokens": 500, "tool_uses": 2, "duration_ms": 1000},
    uuid="u-1",
    session_id="sess-1",
    tool_use_id="tu_spawn",
    last_tool_name="Bash",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events[0]["type"] == "task_progress"
  assert bus.events[0]["last_tool_name"] == "Bash"
  assert bus.events[0]["tool_use_id"] == "tu_spawn"


def test_dispatch_task_notification_done():
  bus = _Bus()
  msg = TaskNotificationMessage(
    subtype="task_notification",
    data={},
    task_id="t-1",
    status="completed",
    output_file="/tmp/out",
    summary="all good",
    uuid="u-1",
    session_id="sess-1",
    tool_use_id="tu_spawn",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{
    "type": "task_done",
    "task_id": "t-1",
    "status": "completed",
    "summary": "all good",
    "tool_use_id": "tu_spawn",
  }]
  assert bus.lifecycle_events[0]["provider_session_id"] == "sess-1"
  assert bus.lifecycle_events[0]["source_event_id"] == "u-1"


def test_dispatch_task_updated_terminal_emits_task_done():
  """A background task's terminal state can arrive ONLY as a task_updated
  patch (no task_notification) — e.g. a TaskStop reporting status "killed".
  It must surface as the same task_done shape so a consumer clears the task."""
  bus = _Bus()
  msg = TaskUpdatedMessage(
    subtype="task_updated",
    data={},
    task_id="t-1",
    patch={"status": "killed", "end_time": 123},
    status="killed",
    session_id="sess-1",
    uuid="u-1",
  )
  dispatch_sdk_message(msg, bus, None)
  # summary + tool_use_id are absent on this SDK class, so the uniform
  # task_done shape carries them as None.
  assert bus.events == [{
    "type": "task_done",
    "task_id": "t-1",
    "status": "killed",
    "summary": None,
    "tool_use_id": None,
  }]
  assert bus.lifecycle_events[0]["provider_session_id"] == "sess-1"
  assert bus.lifecycle_events[0]["source_event_id"] == "u-1"
  assert bus.lifecycle_events[0]["occurred_at"] == 123


def test_dispatch_task_updated_nonterminal_is_silent():
  """Non-terminal task_updated patches (pending/running/paused, or a patch
  carrying only end_time/result with no status) close nothing — they publish
  no event rather than surfacing as noise or an unknown fallthrough."""
  bus = _Bus()
  for status in ("running", "paused", None):
    msg = TaskUpdatedMessage(
      subtype="task_updated",
      data={},
      task_id="t-1",
      patch={"status": status} if status else {"end_time": 9},
      status=status,
      session_id="sess-1",
      uuid="u-1",
    )
    dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


def test_dispatch_result_message_returns_terminal():
  bus = _Bus()
  usage_state = {}
  dispatch_sdk_message(
    AssistantMessage(
      content=[],
      model="claude-sonnet",
      usage={
        "input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 40,
      },
    ),
    bus,
    None,
    usage_state=usage_state,
  )
  msg = ResultMessage(
    subtype="success",
    duration_ms=1000,
    duration_api_ms=900,
    is_error=False,
    num_turns=1,
    session_id="sess-1",
    stop_reason="end_turn",
    total_cost_usd=0.05,
    usage={"input_tokens": 100, "output_tokens": 200},
  )
  before_result = len(bus.events)
  new_sid, terminal = dispatch_sdk_message(
    msg, bus, None, usage_state=usage_state,
  )
  assert new_sid == "sess-1"
  assert terminal is not None
  assert terminal["cost_usd"] == 0.05
  assert terminal["session_id"] == "sess-1"
  assert terminal["usage"] == {"input_tokens": 100, "output_tokens": 200}
  assert terminal["usage_metrics"] == {
    "provider": "claude",
    "scope": "turn",
    "calculation": "result_aggregate",
    "input_tokens": 100,
    "uncached_input_tokens": 100,
    "output_tokens": 200,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 300,
    "model_context_window": None,
    "latest_model_input_tokens": 100,
    "provider_usage": {"input_tokens": 100, "output_tokens": 200},
    "provider_model_usage": None,
  }
  # The turn aggregate is not context occupancy, so the result publishes no
  # live context reading; stop_reason still fires.
  types = [e["type"] for e in bus.events[before_result:]]
  assert "context_usage" not in types
  assert "stop_reason" in types


def test_dispatch_init_system_message_is_silent():
  bus = _Bus()
  msg = SystemMessage(subtype="init", data={"hello": "world"})
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


# ---------------------------------------------------------------------------
# Read-based skill_loaded observability. The in-product agent loads
# skills by Reading /data/shared/skills/<name>.md (the Skill tool is
# never offered on the default skills-disabled posture), so the
# can_use_tool callback is where skill loads actually become visible.
# ---------------------------------------------------------------------------

def _skills_dir() -> str:
  from app.config import get_settings
  return os.path.join(get_settings().data_dir, "shared", "skills")


def test_skill_file_read_name_matches_absolute_skill_path():
  from app.claude_sdk_runner import _skill_file_read_name

  path = os.path.join(_skills_dir(), "memory.md")
  assert _skill_file_read_name("Read", {"file_path": path}, "/data") == "memory"


def test_skill_file_read_name_resolves_relative_against_cwd():
  from app.claude_sdk_runner import _skill_file_read_name
  from app.config import get_settings

  rel = os.path.join("shared", "skills", "building-apps.md")
  name = _skill_file_read_name(
    "Read", {"file_path": rel}, get_settings().data_dir,
  )
  assert name == "building-apps"


def test_skill_file_read_name_normalizes_dot_segments():
  from app.claude_sdk_runner import _skill_file_read_name

  path = os.path.join(_skills_dir(), "..", "skills", "reflection.md")
  assert (
    _skill_file_read_name("Read", {"file_path": path}, "/data")
    == "reflection"
  )


def test_skill_file_read_name_rejects_non_matches():
  from app.claude_sdk_runner import _skill_file_read_name

  skills = _skills_dir()
  cases = [
    # A non-Read tool never matches, even on a skill path.
    ("Bash", {"file_path": os.path.join(skills, "memory.md")}),
    # Only .md files in the skills dir are skills.
    ("Read", {"file_path": os.path.join(skills, "notes.txt")}),
    # Same-suffix path under a DIFFERENT root is not a skill load.
    ("Read", {"file_path": "/somewhere/else/shared/skills/memory.md"}),
    # Nested subdirectories are not skill files.
    ("Read", {"file_path": os.path.join(skills, "deeper", "memory.md")}),
    ("Read", {}),
    ("Read", {"file_path": "   "}),
    ("Read", "not a dict"),
  ]
  for tool, input_data in cases:
    assert _skill_file_read_name(tool, input_data, "/data") == ""


def test_skill_file_read_name_matches_dir_shaped_skill():
  """`<skills>/<name>/SKILL.md` (the installed-skill convention) is a load."""
  from app.claude_sdk_runner import _skill_file_read_name

  path = os.path.join(_skills_dir(), "pdf-tools", "SKILL.md")
  assert _skill_file_read_name("Read", {"file_path": path}, "/data") == "pdf-tools"
  # A resource file inside the skill dir is NOT a load — only the entry doc.
  res = os.path.join(_skills_dir(), "pdf-tools", "reference.md")
  assert _skill_file_read_name("Read", {"file_path": res}, "/data") == ""


def test_skill_file_read_name_ignores_generated_index():
  """Reading skills-index.md is browsing the index, not loading a skill."""
  from app.claude_sdk_runner import _skill_file_read_name

  path = os.path.join(_skills_dir(), "skills-index.md")
  assert _skill_file_read_name("Read", {"file_path": path}, "/data") == ""


def test_observe_skill_file_read_publishes_receipt_and_activity(monkeypatch):
  from app import activity
  from app.claude_sdk_runner import observe_skill_file_read

  logged: list[tuple] = []
  monkeypatch.setattr(
    activity, "log_skill_load",
    lambda chat_id, skill, ts=None: logged.append((chat_id, skill)),
  )
  bus = _Bus()
  path = os.path.join(_skills_dir(), "memory.md")
  observe_skill_file_read(
    "Read", {"file_path": path}, bc=bus, chat_id="chat-7", cwd="/data",
  )
  assert bus.events == [{"type": "skill_loaded", "skill": "memory"}]
  assert logged == [("chat-7", "memory")]


def test_observe_skill_file_read_never_raises(monkeypatch):
  """Fire-and-forget: a broken broadcast must not fail the tool call."""
  from app.claude_sdk_runner import observe_skill_file_read

  class _ExplodingBus:
    def publish(self, event):
      raise RuntimeError("wire down")

  path = os.path.join(_skills_dir(), "memory.md")
  observe_skill_file_read(
    "Read", {"file_path": path}, bc=_ExplodingBus(), chat_id="c",
    cwd="/data",
  )


@pytest.mark.asyncio
async def test_can_use_tool_read_of_skill_file_emits_skill_loaded(
  monkeypatch,
):
  """The canonical interception point: the runner's can_use_tool
  callback observes skill-file Reads — targeted receipt + activity record —
  and still allows the tool with its input unchanged."""
  from app import activity, claude_sdk_runner
  from claude_agent_sdk.types import PermissionResultAllow

  logged: list[tuple] = []
  monkeypatch.setattr(
    activity, "log_skill_load",
    lambda chat_id, skill, ts=None: logged.append((chat_id, skill)),
  )

  clients = _install_fake_client(monkeypatch)
  bus = _ChatBus()
  await _run_turn("chat-42", bc=bus, cwd="/data")

  can_use_tool = clients[0].options.can_use_tool
  path = os.path.join(_skills_dir(), "notifications.md")
  input_data = {"file_path": path}
  context = SimpleNamespace(tool_use_id="read-skill-1")
  result = await can_use_tool("Read", input_data, context)
  assert isinstance(result, PermissionResultAllow)
  assert result.updated_input == input_data
  assert {
    "type": "skill_loaded", "skill": "notifications",
    "tool_use_id": "read-skill-1",
  } in bus.events
  assert logged == [("chat-42", "notifications")]

  # A Read outside the skills dir passes through silently.
  before = list(bus.events)
  result = await can_use_tool(
    "Read", {"file_path": "/data/notes/today.md"}, None,
  )
  assert isinstance(result, PermissionResultAllow)
  assert bus.events == before
  assert logged == [("chat-42", "notifications")]


@pytest.mark.asyncio
async def test_delegated_claude_keeps_parent_tools_without_hidden_budget(
  monkeypatch,
):
  from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

  captured: dict = {}
  real_options = claude_sdk_runner.ClaudeAgentOptions

  def capture_options(**kwargs):
    captured["kwargs"] = dict(kwargs)
    options = real_options(**kwargs)
    captured["options"] = options
    return options

  monkeypatch.setattr(claude_sdk_runner, "ClaudeAgentOptions", capture_options)
  _install_fake_client(monkeypatch)

  policy = SimpleNamespace(scope="read")
  await _run_turn(
    "delegated-tools", bc=_Bus(), prompt="review", cwd="/data",
    run_policy=policy,
  )

  kwargs = captured["kwargs"]
  assert "max_budget_usd" not in kwargs
  assert "agents" not in kwargs
  disallowed = set(kwargs["disallowed_tools"])
  assert "AskUserQuestion" in disallowed
  assert "create_goal" in disallowed
  assert set(claude_sdk_runner._CLAUDE_NATIVE_SCHEDULING_TOOLS) <= disallowed
  finite_tools = {
    "Bash", "Task", "TaskStop", "Workflow", "Workflows",
    "Agent",
  }
  assert not disallowed.intersection(finite_tools)

  can_use_tool = captured["options"].can_use_tool
  for tool_name in finite_tools:
    result = await can_use_tool(tool_name, {}, None)
    assert isinstance(result, PermissionResultAllow)
  assert isinstance(
    await can_use_tool("AskUserQuestion", {"questions": []}, None),
    PermissionResultDeny,
  )
  for tool_name in claude_sdk_runner._CLAUDE_NATIVE_SCHEDULING_TOOLS:
    assert isinstance(
      await can_use_tool(tool_name, {}, None), PermissionResultDeny,
    )


@pytest.mark.asyncio
async def test_rate_limit_resets_at_rides_the_terminal_result(monkeypatch):
  """A RateLimitEvent's structured resets_at lands on the terminal dict so
  the limit park (design §2.4) can use the exact reset time; a turn with NO
  rate-limit event carries no such key and completes cleanly — the
  regression here was an unbound attempt-scope local that error'd every
  ordinary turn."""
  epoch = 1783813200  # any fixed unix-seconds reset time

  class _Client(_FakeClient):
    async def receive_response(self):
      yield _stream_delta("text_delta", text="working")
      yield RateLimitEvent(
        rate_limit_info=RateLimitInfo(
          status="rejected", resets_at=epoch, rate_limit_type="five_hour",
        ),
        uuid="rl-1",
        session_id="sess-1",
      )
      yield _success_result()

  _install_fake_client(monkeypatch, _Client)
  result = await _run_turn("chat-42")

  assert result["error"] is None
  assert result["rate_limit_resets_at"] == epoch


@pytest.mark.asyncio
async def test_turn_without_rate_limit_event_has_no_resets_key(monkeypatch):
  _install_fake_client(monkeypatch)
  result = await _run_turn("chat-42")

  assert result["error"] is None
  assert "rate_limit_resets_at" not in result


def test_clip_task_text_bounds_and_coerces():
  """task_* text is clipped at emission: None stays None, an oversized string is
  truncated, and a non-string (SDK shape drift) is coerced so it can't crash a
  downstream render."""
  from app.claude_sdk_runner import _clip_task_text
  assert _clip_task_text(None, 100) is None
  assert _clip_task_text("ok", 100) == "ok"
  big = "x" * 5000
  out = _clip_task_text(big, 2000)
  assert len(out) == 2000 and out.endswith("…")
  assert _clip_task_text({"a": 1}, 100) == "{'a': 1}"


def test_precompact_log_trigger_extracts_and_is_defensive():
  # The PreCompact observability hook must never raise into the SDK's own
  # compaction path, so the trigger extractor coerces anything unexpected to
  # None and only returns a real string trigger.
  from app.claude_sdk_runner import _precompact_log_trigger

  assert _precompact_log_trigger({"trigger": "auto"}) == "auto"
  assert _precompact_log_trigger({"trigger": "manual"}) == "manual"
  assert _precompact_log_trigger({}) is None
  assert _precompact_log_trigger({"trigger": 123}) is None
  assert _precompact_log_trigger(None) is None
  assert _precompact_log_trigger("not-a-dict") is None


def _stream_message_start(message_id: str) -> StreamEvent:
  return StreamEvent(
    uuid="evt-ms", session_id="sess-1",
    event={"type": "message_start", "message": {"id": message_id}},
  )


def _stream_text_delta_at(index: int, text: str) -> StreamEvent:
  return StreamEvent(
    uuid="evt-td", session_id="sess-1",
    event={
      "type": "content_block_delta", "index": index,
      "delta": {"type": "text_delta", "text": text},
    },
  )


def _stream_text_block_start(index: int) -> StreamEvent:
  return StreamEvent(
    uuid="evt-cbs", session_id="sess-1",
    event={
      "type": "content_block_start", "index": index,
      "content_block": {"type": "text"},
    },
  )


def test_claude_text_final_repairs_earlier_block_by_id():
  """A dropped leading delta on the FIRST of two text blocks in one message is
  repaired by the authoritative text_final, matched by (message id + index).

  Before the id was threaded, text_final for the first block landed on the
  trailing (second) block positionally, so the first block kept its truncated
  delta accumulation forever (the dropped-leading-token bug).
  """
  from app.events import process_event

  bus = _ChatBus()
  # One message, TWO text blocks (indices 0 and 1). Block 0's leading delta
  # ("Al") was dropped, so it accumulates truncated ("pha text here").
  dispatch_sdk_message(_stream_message_start("msg_abc"), bus, None)
  dispatch_sdk_message(_stream_text_delta_at(0, "pha text here"), bus, None)
  dispatch_sdk_message(_stream_text_block_start(1), bus, None)
  dispatch_sdk_message(_stream_text_delta_at(1, "Beta text"), bus, None)
  dispatch_sdk_message(
    AssistantMessage(
      content=[TextBlock(text="Alpha text here"), TextBlock(text="Beta text")],
      model="claude-opus",
      message_id="msg_abc",
    ),
    bus, None,
  )

  # Delta and final events carry matching, turn-unique ids.
  text_events = [e for e in bus.events if e["type"] == "text"]
  final_events = [e for e in bus.events if e["type"] == "text_final"]
  assert text_events[0]["text_item_id"] == "msg_abc:0"
  assert final_events[0]["text_item_id"] == "msg_abc:0"
  assert final_events[1]["text_item_id"] == "msg_abc:1"

  # Reduce the emitted events the way the sink does; the first block is repaired.
  blocks: list[dict] = []
  for event in bus.events:
    process_event(event, blocks)
  texts = [b["content"] for b in blocks if b.get("type") == "text"]
  assert texts == ["Alpha text here", "Beta text"]


def test_claude_text_events_have_no_id_without_message_id():
  """No message id (no message_start, or a message_id-less AssistantMessage)
  means no text_item_id — the reducer keeps its positional fallback, so nothing
  regresses for paths that do not supply the id."""
  bus = _ChatBus()
  dispatch_sdk_message(_stream_text_delta_at(0, "hello"), bus, None)
  dispatch_sdk_message(
    AssistantMessage(content=[TextBlock(text="hello")], model="claude-opus"),
    bus, None,
  )
  emitted = [e for e in bus.events if e["type"] in ("text", "text_final")]
  assert emitted and all("text_item_id" not in e for e in emitted)
