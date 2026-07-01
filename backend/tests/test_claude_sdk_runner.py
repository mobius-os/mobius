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
from typing import Any

import pytest

from claude_agent_sdk.types import (
  AssistantMessage,
  RateLimitEvent,
  RateLimitInfo,
  ResultMessage,
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

from app import claude_sdk_runner, models
from app.claude_sdk_runner import (
  ActiveClaudeClient,
  dispatch_sdk_message,
  run_claude_sdk_turn,
  steer_into_active_turn,
)
from app.database import SessionLocal
from app.runner_registry import registry


class _Bus:
  """Minimal stand-in for ChatBroadcast used by the dispatch tests.

  Records every publish call in order so assertions can check both
  the event sequence and the event payloads.
  """

  def __init__(self) -> None:
    self.events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)


class _ChatBus(_Bus):
  chat_id = "chat-42"
  run_token = "run-1"


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
async def test_steer_into_active_turn_buffers_without_interrupting():
  """A registered Claude handle BUFFERS steer text + flags the request,
  but does NOT interrupt mid-token. The cut happens at the next
  content-block boundary in the runner loop (see the boundary tests
  below), so `steer()` itself never touches live IO."""
  calls = []

  class _Client:
    async def interrupt(self):
      calls.append("interrupt")

  handle = ActiveClaudeClient(_Client(), chat_id="claude-steer")
  registry.register(handle)
  try:
    assert await steer_into_active_turn("claude-steer", "use blue") is True
    assert handle.pending_steer == ["use blue"]
    assert handle._steer_requested is True
    # Buffering must NOT interrupt — the old behavior cut mid-token and
    # lost in-flight text; the boundary-fire design defers the cut.
    assert calls == []
    # A second rapid steer must QUEUE behind the first (FIFO), not overwrite it
    # — both texts are already persisted to the transcript, so both must reach
    # Claude when the runner drains the mailbox.
    assert await steer_into_active_turn("claude-steer", "and bold") is True
    assert handle.pending_steer == ["use blue", "and bold"]
    assert handle._steer_requested is True
    assert calls == []
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
async def test_resteer_requeries_when_terminal_precedes_boundary(monkeypatch):
  """A steer buffered after a StreamEvent (no AssistantMessage boundary)
  before a natural ResultMessage still re-queries on the SAME client.

  This is edge (a) of the boundary design: a very short turn whose
  ResultMessage arrives before any completed content block. The boundary
  cut never fires (interrupts == 0), but the existing pending_steer →
  requery path on the terminal result MUST still deliver the steer."""
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
  # No AssistantMessage boundary preceded the terminal ResultMessage, so
  # the boundary cut never fired — the steer rode the natural terminal.
  assert client.interrupts == 0
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


@pytest.mark.asyncio
async def test_steer_fires_at_assistant_boundary_not_on_deltas(monkeypatch):
  """THE core contract: a steer requested mid-turn does NOT interrupt
  while token deltas (StreamEvent) stream — it waits for the next
  COMPLETED content block (an AssistantMessage), interrupts exactly once
  THERE, then re-queries exactly once on the same client.

  The fake stream records the interrupt-call count at the moment each
  message is dispatched, so the test can assert interrupt fired AFTER the
  AssistantMessage and NOT on any preceding StreamEvent delta."""
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
        # First turn: deltas stream, the user steers mid-block, MORE
        # deltas stream (interrupt must NOT have fired yet), then a
        # COMPLETED text block arrives — that is where the cut fires.
        yield _stream_delta("text_delta", text="thinking ")
        interrupt_trace.append(("delta-1", self.interrupts))
        assert await steer_into_active_turn("boundary-chat", "use blue") \
          is True
        yield _stream_delta("text_delta", text="about it")
        interrupt_trace.append(("delta-2-after-steer", self.interrupts))
        # The completed block — boundary. The runner interrupts right
        # after dispatching THIS message.
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
  # No interrupt while deltas were streaming — the half-sentence was NOT
  # cut mid-token (this is the whole point of the change).
  assert trace["delta-1"] == 0
  assert trace["delta-2-after-steer"] == 0
  # The interrupt fired AT the AssistantMessage boundary, exactly once.
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
async def test_steer_interrupts_once_despite_multiple_boundaries(monkeypatch):
  """A second AssistantMessage arriving in the drain window (after the
  boundary interrupt, before its terminal ResultMessage) must NOT fire a
  SECOND interrupt. `_interrupt_in_flight` guards the single cut. Two
  buffered steers must both ride the single requery (FIFO, exactly once)."""
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
        # Two rapid steers buffer before any boundary.
        assert await steer_into_active_turn("multi-chat", "use blue") is True
        assert await steer_into_active_turn("multi-chat", "and bold") is True
        # First completed block — the single boundary cut fires here.
        yield _assistant_text("first block")
        # The SDK may still emit a trailing completed block in the drain
        # window before the interrupt's terminal lands. It must NOT cause
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
  # Exactly one interrupt despite two AssistantMessage boundaries in the
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
async def test_steer_after_assistant_boundary_already_passed(monkeypatch):
  """A steer buffered AFTER the only AssistantMessage already streamed,
  with a fresh boundary still to come, fires the cut on that next
  boundary — proving the flag (not a one-time event) drives the cut."""
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
        # Steer arrives only now — no boundary cut yet because the flag
        # is checked when a message is dispatched, and the next message
        # is the boundary we cut on.
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
async def test_stop_drops_buffered_steer_and_clears_flags(monkeypatch):
  """Stop is the hard immediate-cut path: it must drop a buffered steer
  (no boundary cut, no requery for abandoned work) and clear both steer
  flags. Distinct from steer's deferred boundary cut."""
  del monkeypatch  # this handle-level test needs no SDK patching

  calls: list[str] = []

  class _Client:
    async def interrupt(self):
      calls.append("interrupt")

  handle = ActiveClaudeClient(_Client(), chat_id="stop-chat")
  registry.register(handle)
  try:
    # Buffer a steer, then Stop.
    assert await steer_into_active_turn("stop-chat", "use blue") is True
    assert handle.pending_steer == ["use blue"]
    assert handle._steer_requested is True

    # mark_finished so interrupt()'s _finished wait returns immediately.
    handle.mark_finished()
    await handle.interrupt()

    assert calls == ["interrupt"]
    assert handle.pending_steer == []
    assert handle._steer_requested is False
  finally:
    registry.unregister("stop-chat", handle.kind)


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
  monkeypatch.setattr(claude_sdk_runner.time, "time", lambda: 1.234)
  bus = _Bus()
  msg = _stream_delta("thinking_delta", thinking="planning...")
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{
    "type": "thinking",
    "content": "planning...",
    "ts": 1234,
  }]


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


def test_dispatch_assistant_thinking_block_emits_thinking(monkeypatch):
  monkeypatch.setattr(claude_sdk_runner.time, "time", lambda: 2.5)
  bus = _Bus()
  msg = AssistantMessage(
    content=[ThinkingBlock(thinking="reflecting", signature="sig")],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert {
    "type": "thinking",
    "content": "reflecting",
    "ts": 2500,
  } in bus.events


def test_dispatch_assistant_tool_use_emits_tool_start():
  bus = _Bus()
  msg = AssistantMessage(
    content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  types = [e["type"] for e in bus.events]
  assert "tool_start" in types


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
  # tool_start fires first, then the skill_loaded chip event.
  assert types == ["tool_start", "tool_input", "skill_loaded"]
  loaded = [e for e in bus.events if e["type"] == "skill_loaded"]
  assert loaded == [{"type": "skill_loaded", "skill": "humanizer"}]
  assert logged == [("chat-42", "humanizer")]


def test_dispatch_skill_tool_without_name_does_not_emit(monkeypatch):
  """A Skill tool_use with no resolvable skill name emits no chip and
  logs nothing — an empty chip carries no signal."""
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


def test_dispatch_assistant_text_block_is_silent():
  """TextBlock is a snapshot duplicate of streamed text_delta — must
  not re-emit as text to avoid doubling the content."""
  bus = _Bus()
  msg = AssistantMessage(
    content=[TextBlock(text="hello")],
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
    task_type="build",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{
    "type": "task_start",
    "task_id": "t-1",
    "description": "build app",
    "task_type": "build",
  }]


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
    last_tool_name="Bash",
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events[0]["type"] == "task_progress"
  assert bus.events[0]["last_tool_name"] == "Bash"


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
  )
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{
    "type": "task_done",
    "task_id": "t-1",
    "status": "completed",
    "summary": "all good",
  }]


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


def test_observe_skill_file_read_publishes_chip_and_activity(monkeypatch):
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
  callback observes skill-file Reads — chip event + activity record —
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
  result = await can_use_tool("Read", input_data, None)
  assert isinstance(result, PermissionResultAllow)
  assert result.updated_input == input_data
  assert {"type": "skill_loaded", "skill": "notifications"} in bus.events
  assert logged == [("chat-42", "notifications")]

  # A Read outside the skills dir passes through silently.
  before = list(bus.events)
  result = await can_use_tool(
    "Read", {"file_path": "/data/notes/today.md"}, None,
  )
  assert isinstance(result, PermissionResultAllow)
  assert bus.events == before
  assert logged == [("chat-42", "notifications")]
