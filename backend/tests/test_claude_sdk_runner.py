"""Tests for the Claude SDK runner's event dispatch.

These tests exercise `dispatch_sdk_message` directly with hand-built
SDK message instances so the unit doesn't spin up the Claude
subprocess or the SDK transport. The dispatch is the load-bearing
behavior we care about: every SDK message type either translates
into a Möbius event or surfaces as `unknown_sdk_event`. Nothing
silently disappears.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from claude_agent_sdk import ProcessError
from claude_agent_sdk.types import (
  AssistantMessage,
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


@pytest.mark.asyncio
async def test_claude_connection_secret_is_retired_after_connect_without_fd_reuse(
  monkeypatch,
):
  observed = {}

  class _FakeClient:
    def __init__(self, options):
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

    async def receive_response(self):
      yield _success_result()

    async def disconnect(self):
      return None

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)
  plan = connector_core.ConnectorTurnPlan(claude_servers={
    "private": {
      "type": "http",
      "url": "https://mcp.example/mcp",
      "headers": {"Authorization": "Bearer private-key"},
    },
  })

  result = await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="claude-mcp-config",
    skill_text="system",
    bc=_ChatBus(),
    pending_questions={},
    db=None,
    connector_plan=plan,
  )

  assert "private-key" in observed["config"]
  assert not os.path.exists(observed["path"])
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
    "hello",
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
    "hello",
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
  from app import claude_sdk_runner

  class _FakeClient:
    def __init__(self, options):
      del options
      self.queries = []
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
      if len(self.queries) == 1:
        yield _stream_delta("text_delta", text="working")
        assert await steer_into_active_turn("loop-chat", "use blue") is True
        yield ResultMessage(
          subtype="error_during_execution",
          duration_ms=10,
          duration_api_ms=5,
          is_error=True,
          num_turns=1,
          session_id="sess-1",
          stop_reason="interrupt",
          total_cost_usd=0.01,
          usage={"input_tokens": 1, "output_tokens": 2},
        )
        return
      yield _stream_delta("text_delta", text="blue done")
      yield ResultMessage(
        subtype="success",
        duration_ms=20,
        duration_api_ms=15,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason="end_turn",
        total_cost_usd=0.02,
        usage={"input_tokens": 3, "output_tokens": 4},
      )

  clients = []

  def _client_factory(options):
    client = _FakeClient(options)
    clients.append(client)
    return client

  monkeypatch.setattr(
    claude_sdk_runner, "ClaudeSDKClient", _client_factory,
  )

  bus = _ChatBus()
  result = await run_claude_sdk_turn(
    "start task",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="loop-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

  client = clients[0]
  # The steer fired its interrupt immediately when requested; the terminal
  # ResultMessage then drove the requery.
  assert client.interrupts == 1
  assert client.disconnected is True
  assert client.queries[0] == "start task"
  assert client.queries[1].startswith(
    "The user added this while you were working."
  )
  assert "use blue" in client.queries[1]
  assert result["error"] is None
  assert result["cost_usd"] == 0.02
  assert [e for e in bus.events if e["type"] == "text"] == [
    {"type": "text", "content": "working"},
    {"type": "text", "content": "blue done"},
  ]


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
  """Run one fake response stream with an optional owner Stop in flight."""
  process_error = ProcessError(
    f"Command failed with exit code {'1' if mode == 'process_failure' else '-15'}",
    exit_code=1 if mode == "process_failure" else -15,
    stderr="Check stderr output for details",
  )

  class _Transport:
    _process = None
    _exit_error = process_error if mode in ("process_error", "process_failure") else None

  class _FakeClient:
    def __init__(self, options):
      del options
      self._transport = _Transport()

    async def connect(self):
      return None

    async def query(self, message):
      del message

    async def interrupt(self):
      return None

    async def disconnect(self):
      return None

    async def receive_response(self):
      handle = registry.get_handle("claude-stop-shape", RunnerKind.CLAUDE_SDK)
      assert handle is not None
      handle._interrupt_requested = owned
      if mode == "terminal":
        yield _interrupt_result()
        return
      if mode == "resultless":
        return
      if mode in ("process_error", "process_failure"):
        raise Exception(str(process_error))
      if mode == "other_error":
        raise ValueError("unexpected notification payload")
      raise AssertionError(mode)

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)
  return await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="claude-stop-shape",
    skill_text="system",
    bc=_ChatBus(),
    pending_questions={},
    db=None,
  )


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


def test_claude_force_stop_check_uses_the_sdk_type_not_its_name():
  impostor = type("ProcessError", (Exception,), {})

  class _Client:
    _transport = type("Transport", (), {"_exit_error": impostor("boom")})()

  assert claude_sdk_runner._claude_process_was_force_stopped(_Client()) is False


def test_claude_force_stop_check_rejects_other_typed_process_failures():
  class _Client:
    _transport = type("Transport", (), {
      "_exit_error": ProcessError("CLI failed", exit_code=1),
    })()

  assert claude_sdk_runner._claude_process_was_force_stopped(_Client()) is False


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
async def test_unrequested_claude_interrupt_result_stays_an_error(monkeypatch):
  result = await _run_claude_stop_outcome(monkeypatch, "terminal", owned=False)

  assert result["error"] == "Execution interrupted."
  assert result.get("terminal_status") is None


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
  from app import claude_sdk_runner

  # (message_label, interrupts_observed_when_this_message_was_yielded)
  interrupt_trace: list[tuple[str, int]] = []

  class _FakeClient:
    def __init__(self, options):
      del options
      self.queries: list[str] = []
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

  clients: list[_FakeClient] = []

  def _factory(options):
    c = _FakeClient(options)
    clients.append(c)
    return c

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _factory)

  bus = _ChatBus()
  result = await run_claude_sdk_turn(
    "start task",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="boundary-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

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
    "The user added this while you were working."
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
  from app import claude_sdk_runner

  class _FakeClient:
    def __init__(self, options):
      del options
      self.queries: list[str] = []
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
      if len(self.queries) == 1:
        # Two rapid steers: the first fires the interrupt, the second is
        # guarded by _interrupt_in_flight and only buffers its text.
        assert await steer_into_active_turn("multi-chat", "use blue") is True
        assert await steer_into_active_turn("multi-chat", "and bold") is True
        yield _assistant_text("first block")
        # The SDK may still emit trailing completed blocks in the drain
        # window before the interrupt's terminal lands. They must NOT cause
        # a second interrupt.
        yield _assistant_text("straggler block")
        yield _interrupt_result()
        return
      yield _stream_delta("text_delta", text="done")
      yield _success_result()

  clients: list[_FakeClient] = []

  def _factory(options):
    c = _FakeClient(options)
    clients.append(c)
    return c

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _factory)

  bus = _ChatBus()
  result = await run_claude_sdk_turn(
    "start task",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="multi-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

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
  from app import claude_sdk_runner

  class _FakeClient:
    def __init__(self, options):
      del options
      self.queries: list[str] = []
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

  clients: list[_FakeClient] = []

  def _factory(options):
    c = _FakeClient(options)
    clients.append(c)
    return c

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _factory)

  bus = _ChatBus()
  result = await run_claude_sdk_turn(
    "start task",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="late-chat",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

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
  from app import claude_sdk_runner

  class _FakeClient:
    def __init__(self, options):
      del options
      self.disconnected = False

    async def connect(self):
      return None

    async def query(self, message):
      del message

    async def disconnect(self):
      self.disconnected = True

    async def receive_response(self):
      yield StreamEvent(
        uuid="evt-session",
        session_id="sess-early",
        event={
          "type": "content_block_delta",
          "delta": {"type": "text_delta", "text": "still running"},
        },
      )
      yield ResultMessage(
        subtype="success",
        duration_ms=20,
        duration_api_ms=15,
        is_error=False,
        num_turns=1,
        session_id="sess-early",
        stop_reason="end_turn",
        total_cost_usd=0.02,
        usage={"input_tokens": 3, "output_tokens": 4},
      )

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

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

    result = asyncio.run(
      run_claude_sdk_turn(
        "hello",
        session_id=None,
        base_env={},
        cwd="/tmp",
        chat_id="claude-early",
        skill_text="system",
        bc=_ChatBus(),
        pending_questions={},
        db=db,
      )
    )

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


def test_claude_thinking_config_requests_summarized_adaptive_thinking():
  assert claude_sdk_runner._claude_thinking_config("claude-opus-4-8") == {
    "type": "adaptive",
    "display": "summarized",
  }
  assert claude_sdk_runner._claude_thinking_config(
    "claude-sonnet-4-7-20251215"
  ) == {
    "type": "adaptive",
    "display": "summarized",
  }
  assert claude_sdk_runner._claude_thinking_config(None) == {
    "type": "adaptive",
    "display": "summarized",
  }
  assert (
    claude_sdk_runner._claude_thinking_config("claude-opus-4-5-20251001")
    is None
  )


@pytest.mark.asyncio
async def test_run_claude_sdk_turn_requests_summarized_thinking(monkeypatch):
  captured: dict = {}

  class _FakeClient:
    def __init__(self, options):
      captured["options"] = options

    async def connect(self):
      return None

    async def query(self, message):
      del message

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="sess-thinking",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 1},
      )

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

  await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/data",
    chat_id="chat-thinking",
    skill_text="system",
    bc=_Bus(),
    pending_questions={},
    db=None,
    agent_settings={"model": "claude-opus-4-8", "effort": "high"},
  )

  assert captured["options"].model == "claude-opus-4-8"
  assert captured["options"].effort == "high"
  # The Claude runner appends its provider-authored concise register on top of
  # the shared base (documented amendment to system_prompts.py's contract): the
  # shared base is preserved verbatim, with the register appended after it.
  assert captured["options"].system_prompt == (
    claude_sdk_runner._system_prompt_with_register("system")
  )
  assert captured["options"].system_prompt.startswith("system")
  assert "# Concise register" in captured["options"].system_prompt
  assert captured["options"].max_buffer_size == 10 * 1024 * 1024
  assert captured["options"].thinking == {
    "type": "adaptive",
    "display": "summarized",
  }


@pytest.mark.asyncio
async def test_precompact_hook_publishes_context_compaction_marker(monkeypatch):
  captured: dict = {}

  class _FakeClient:
    def __init__(self, options):
      captured["options"] = options

    async def connect(self):
      return None

    async def query(self, message):
      del message

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="sess-compaction",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 1},
      )

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)
  bus = _Bus()
  await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/data",
    chat_id="chat-compaction",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

  matcher = captured["options"].hooks["PreCompact"][0]
  result = await matcher.hooks[0]({"trigger": "manual"}, None, {})

  assert result == {"continue_": True}
  assert {
    "type": "context_compacted",
    "provider": "claude",
    "trigger": "manual",
  } in bus.events


def test_dispatch_input_json_delta_emits_unknown(monkeypatch):
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "1")
  bus = _Bus()
  msg = _stream_delta("input_json_delta", partial_json="{\"a\":")
  dispatch_sdk_message(msg, bus, None)
  assert len(bus.events) == 1
  assert bus.events[0]["type"] == "unknown_sdk_event"
  assert bus.events[0]["kind"] == "stream:content_block_delta:input_json_delta"


def test_dispatch_unknown_delta_silent_when_disabled(monkeypatch):
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "0")
  bus = _Bus()
  msg = _stream_delta("signature_delta", signature="abc")
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


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


def test_dispatch_assistant_usage_emits_usage_event():
  bus = _Bus()
  msg = AssistantMessage(
    content=[],
    model="claude-opus",
    usage={"input_tokens": 10, "output_tokens": 5},
  )
  dispatch_sdk_message(msg, bus, None)
  usages = [e for e in bus.events if e["type"] == "usage"]
  assert len(usages) == 1
  assert usages[0]["input_tokens"] == 10
  assert usages[0]["output_tokens"] == 5


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
  new_sid, terminal = dispatch_sdk_message(msg, bus, None)
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
    "provider_usage": {"input_tokens": 100, "output_tokens": 200},
    "provider_model_usage": None,
  }
  # ResultMessage also fires usage + stop_reason side-channels.
  types = [e["type"] for e in bus.events]
  assert "usage" in types
  assert "stop_reason" in types


def test_dispatch_init_system_message_is_silent():
  bus = _Bus()
  msg = SystemMessage(subtype="init", data={"hello": "world"})
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


def test_dispatch_unknown_system_subtype_emits_unknown(monkeypatch):
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "1")
  bus = _Bus()
  msg = SystemMessage(subtype="brand_new_thing", data={"x": 1})
  dispatch_sdk_message(msg, bus, None)
  assert len(bus.events) == 1
  assert bus.events[0]["type"] == "unknown_sdk_event"
  assert bus.events[0]["kind"] == "system:brand_new_thing"


def test_dispatch_unknown_system_subtype_silent_when_disabled(monkeypatch):
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "0")
  bus = _Bus()
  msg = SystemMessage(subtype="brand_new_thing", data={"x": 1})
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == []


def test_dispatch_completely_unknown_sdk_class_emits_unknown(monkeypatch):
  """An SDK message class the dispatcher doesn't know about still
  surfaces — never silently dropped."""
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "1")

  class FreshSdkMessage:  # Stand-in for a hypothetical future SDK type.
    def __init__(self) -> None:
      self.field = "value"

  bus = _Bus()
  dispatch_sdk_message(FreshSdkMessage(), bus, None)
  assert len(bus.events) == 1
  assert bus.events[0]["type"] == "unknown_sdk_event"
  assert "FreshSdkMessage" in bus.events[0]["kind"]


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

  captured: dict = {}

  class _FakeClient:
    def __init__(self, options):
      captured["options"] = options

    async def connect(self):
      return None

    async def query(self, message):
      del message

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="sess-skill",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 1},
      )

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

  bus = _ChatBus()
  await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/data",
    chat_id="chat-42",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

  can_use_tool = captured["options"].can_use_tool
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
async def test_rate_limit_resets_at_rides_the_terminal_result(monkeypatch):
  """A RateLimitEvent's structured resets_at lands on the terminal dict so
  the limit park (design §2.4) can use the exact reset time; a turn with NO
  rate-limit event carries no such key and completes cleanly — the
  regression here was an unbound attempt-scope local that error'd every
  ordinary turn."""
  from app import claude_sdk_runner

  epoch = 1783813200  # any fixed unix-seconds reset time

  class _FakeClient:
    def __init__(self, options):
      del options
      self.queries = []

    async def connect(self):
      return None

    async def query(self, message):
      self.queries.append(message)

    async def interrupt(self):
      return None

    async def disconnect(self):
      return None

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

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

  bus = _ChatBus()
  result = await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-42",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

  assert result["error"] is None
  assert result["rate_limit_resets_at"] == epoch


@pytest.mark.asyncio
async def test_turn_without_rate_limit_event_has_no_resets_key(monkeypatch):
  from app import claude_sdk_runner

  class _FakeClient:
    def __init__(self, options):
      del options

    async def connect(self):
      return None

    async def query(self, message):
      return None

    async def interrupt(self):
      return None

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield _stream_delta("text_delta", text="fine")
      yield _success_result()

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

  bus = _ChatBus()
  result = await run_claude_sdk_turn(
    "hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-42",
    skill_text="system",
    bc=bus,
    pending_questions={},
    db=None,
  )

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
