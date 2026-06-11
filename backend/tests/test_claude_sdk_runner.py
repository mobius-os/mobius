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

from app import models
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
async def test_steer_into_active_turn_sets_mailbox_and_interrupts():
  """A registered Claude handle records steer text and interrupts live IO."""
  calls = []

  class _Client:
    async def interrupt(self):
      calls.append("interrupt")

  handle = ActiveClaudeClient(_Client(), chat_id="claude-steer")
  registry.register(handle)
  try:
    assert await steer_into_active_turn("claude-steer", "use blue") is True
    assert handle.pending_steer == ["use blue"]
    assert calls == ["interrupt"]
    # A second rapid steer must QUEUE behind the first (FIFO), not overwrite it
    # — both texts are already persisted to the transcript, so both must reach
    # Claude when the runner drains the mailbox.
    assert await steer_into_active_turn("claude-steer", "and bold") is True
    assert handle.pending_steer == ["use blue", "and bold"]
    assert calls == ["interrupt", "interrupt"]
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
async def test_interrupt_and_resteer_loop_requeries_same_client(monkeypatch):
  """A steer-triggered interrupt result is drained, then re-queried."""
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


def test_dispatch_thinking_delta_emits_thinking():
  bus = _Bus()
  msg = _stream_delta("thinking_delta", thinking="planning...")
  dispatch_sdk_message(msg, bus, None)
  assert bus.events == [{"type": "thinking", "content": "planning..."}]


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


def test_dispatch_assistant_thinking_block_emits_thinking():
  bus = _Bus()
  msg = AssistantMessage(
    content=[ThinkingBlock(thinking="reflecting", signature="sig")],
    model="claude-opus",
  )
  dispatch_sdk_message(msg, bus, None)
  assert {"type": "thinking", "content": "reflecting"} in bus.events


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

  path = os.path.join(_skills_dir(), "mind.md")
  assert _skill_file_read_name("Read", {"file_path": path}, "/data") == "mind"


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

  path = os.path.join(_skills_dir(), "..", "skills", "dreaming.md")
  assert (
    _skill_file_read_name("Read", {"file_path": path}, "/data")
    == "dreaming"
  )


def test_skill_file_read_name_rejects_non_matches():
  from app.claude_sdk_runner import _skill_file_read_name

  skills = _skills_dir()
  cases = [
    # A non-Read tool never matches, even on a skill path.
    ("Bash", {"file_path": os.path.join(skills, "mind.md")}),
    # Only .md files in the skills dir are skills.
    ("Read", {"file_path": os.path.join(skills, "notes.txt")}),
    # Same-suffix path under a DIFFERENT root is not a skill load.
    ("Read", {"file_path": "/somewhere/else/shared/skills/mind.md"}),
    # Nested subdirectories are not skill files.
    ("Read", {"file_path": os.path.join(skills, "deeper", "mind.md")}),
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
  path = os.path.join(_skills_dir(), "mind.md")
  observe_skill_file_read(
    "Read", {"file_path": path}, bc=bus, chat_id="chat-7", cwd="/data",
  )
  assert bus.events == [{"type": "skill_loaded", "skill": "mind"}]
  assert logged == [("chat-7", "mind")]


def test_observe_skill_file_read_never_raises(monkeypatch):
  """Fire-and-forget: a broken broadcast must not fail the tool call."""
  from app.claude_sdk_runner import observe_skill_file_read

  class _ExplodingBus:
    def publish(self, event):
      raise RuntimeError("wire down")

  path = os.path.join(_skills_dir(), "mind.md")
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
