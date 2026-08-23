import asyncio
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from types import SimpleNamespace

import pytest

from app import codex_sdk_runner, connectors as connector_core, models
from app.agent_lifecycle import normalize_chat_event
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry


# Mirrors the installed SDK:
# - ErrorNotification: /usr/local/lib/python3.12/site-packages/openai_codex/generated/v2_all.py:6958
# - CodexRpcError: /usr/local/lib/python3.12/site-packages/openai_codex/errors.py:24
# - InvalidParamsError: /usr/local/lib/python3.12/site-packages/openai_codex/errors.py:40

try:
  from openai_codex.errors import (
    TransportClosedError as _SdkTransportClosedError,
  )
except ImportError:  # pragma: no cover - SDK is an optional install
  # openai-codex is installed in its own Docker step, not via
  # requirements.txt, so this module must still import without it. The real
  # symbol's continued existence is pinned by test_codex_sdk_contract, which
  # is where its disappearance should be reported.
  class _SdkTransportClosedError(Exception):
    pass


class _FakeBroadcast:
  def __init__(self):
    self.events: list[dict] = []
    self.lifecycle_events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)

  def record_lifecycle(self, event: dict) -> None:
    self.lifecycle_events.append(event)


class _FakeSteerSink:
  def __init__(self):
    self.bc = _FakeBroadcast()
    self.cuts: list[tuple[list[dict], list[str]]] = []

  async def commit_steer_cut(self, user_msgs, consume_pending_cids):
    self.cuts.append((list(user_msgs), list(consume_pending_cids)))
    return {
      "stored_messages": list(user_msgs),
      "pending": [],
    }


class _FakeCodexConfig:
  def __init__(self, **kwargs):
    self.kwargs = kwargs


class _FakeApprovalMode:
  auto_review = "auto_review"


class _FakeSandbox:
  read_only = "read-only"
  workspace_write = "workspace-write"
  full_access = "full-access"


class _FakeReasoningEffort:
  """Callable enum stand-in so `ReasoningEffort(str)` works in tests."""
  def __call__(self, value):
    return value


class _FakeReasoningSummary:
  """Callable enum/model stand-in so `ReasoningSummary(str)` works in tests."""
  def __call__(self, value):
    return value


class _FakeMessagePhase(Enum):
  commentary = "commentary"
  final_answer = "final_answer"


class _FakeTurnStatus(Enum):
  completed = "completed"
  interrupted = "interrupted"
  failed = "failed"
  in_progress = "inProgress"


class _FakeThreadGoalStatus(Enum):
  active = "active"
  paused = "paused"
  blocked = "blocked"
  usage_limited = "usageLimited"
  budget_limited = "budgetLimited"
  complete = "complete"


class _FakeGoalState:
  def __init__(self, thread_id: str, notifications=None, *, started=False):
    self.thread_id = thread_id
    self.logical_turn_id = "goal-turn" if started else None
    self._current_turn_id = "physical-turn" if started else None
    self.notifications = list(notifications or [])
    self.finished = False
    self.woken = False
    self.status = None

  def start(self):
    self.logical_turn_id = "goal-turn"
    self._current_turn_id = "physical-turn"

  def wait_for_start(self, _timeout: float):
    return self.logical_turn_id

  def current_turn(self):
    return self._current_turn_id

  def active_turn(self, *, after=None):
    if self.finished:
      return None
    if self._current_turn_id != after:
      return self._current_turn_id
    return None

  def finish(self):
    self.finished = True
    self._current_turn_id = None

  def wake_notification_reader(self):
    self.woken = True


class _FakeAsyncGoalNotificationStream:
  def __init__(
    self, state, _next_notification, unregister, _cancel_goal,
  ):
    self.state = state
    self.unregister = unregister

  async def __aiter__(self):
    for notification in self.state.notifications:
      yield notification
    self.unregister()


class _FakeGoalClient:
  def __init__(self, goal=None):
    self.goal = goal
    self.state: _FakeGoalState | None = None
    self.register_calls: list[str] = []
    self.start_calls: list[tuple[str, str]] = []
    self.set_calls: list[tuple[str, _FakeThreadGoalStatus]] = []
    self.clear_calls: list[str] = []
    self.steer_calls: list[tuple[str, str, str]] = []
    self.cancel_calls: list[_FakeGoalState] = []
    self.unregister_calls: list[_FakeGoalState] = []

  async def request(self, method, params, *, response_model):
    assert method == "thread/goal/get"
    assert params == {"threadId": "thread-1"}
    assert response_model is not None
    return SimpleNamespace(goal=self.goal)

  def register_goal_operation(self, thread_id):
    self.register_calls.append(thread_id)
    self.state = _FakeGoalState(thread_id)
    self.state.status = self.goal.status
    return self.state

  async def start_goal_operation(self, thread_id, objective):
    self.start_calls.append((thread_id, objective))
    self.state = _FakeGoalState(
      thread_id, _goal_completion_notifications(), started=True,
    )
    return self.state, self.state.logical_turn_id

  async def thread_goal_set(self, thread_id, *, status=None, objective=None):
    assert objective is None
    self.set_calls.append((thread_id, status))
    assert self.state is not None
    self.state.start()
    self.state.notifications = _goal_completion_notifications()
    return SimpleNamespace()

  async def thread_goal_clear(self, thread_id):
    self.clear_calls.append(thread_id)
    self.goal = None
    return SimpleNamespace(cleared=True)

  async def turn_steer(self, thread_id, expected_turn_id, message):
    self.steer_calls.append((thread_id, expected_turn_id, message))

  async def next_goal_notification(self, _state):
    raise AssertionError("fake goal stream reads its state directly")

  async def cancel_goal_operation(self, state):
    self.cancel_calls.append(state)

  def unregister_goal_operation(self, state):
    self.unregister_calls.append(state)


def _goal_completion_notifications():
  completed = SimpleNamespace(id="goal-turn", usage=None, error=None)
  return [SimpleNamespace(
    method="turn/completed",
    payload=_FakeTurnCompletedNotification(completed),
  )]


class _FakeTurnCompletedNotification:
  def __init__(self, turn):
    self.turn = turn


class _FakeTurnHandle:
  def __init__(
    self,
    notifications=None,
    steer_exc: Exception | None = None,
    interrupt_exc: Exception | None = None,
    stream_exc: Exception | None = None,
  ):
    self._notifications = notifications or []
    self._steer_exc = steer_exc
    self._interrupt_exc = interrupt_exc
    self._stream_exc = stream_exc
    self.steered: list[str] = []
    self.interrupt_calls = 0

  async def stream(self):
    for notification in self._notifications:
      yield notification
    if self._stream_exc is not None:
      raise self._stream_exc

  async def steer(self, message: str):
    if self._steer_exc is not None:
      raise self._steer_exc
    self.steered.append(message)

  async def interrupt(self):
    self.interrupt_calls += 1
    if self._interrupt_exc is not None:
      raise self._interrupt_exc


class _FakeThread:
  def __init__(self, thread_id: str, turn_handle: _FakeTurnHandle):
    self.id = thread_id
    self._turn_handle = turn_handle
    self.turn_args = None
    self.turn_kwargs = None

  async def turn(self, *args, **kwargs):
    self.turn_args = args
    self.turn_kwargs = kwargs
    return self._turn_handle


def _fake_sdk(async_codex_cls):
  class _Dummy:  # pragma: no cover - identity only
    pass

  class _FakeErrorNotification:
    def __init__(
      self,
      error,
      thread_id: str,
      turn_id: str,
      will_retry: bool,
    ):
      self.error = error
      self.thread_id = thread_id
      self.turn_id = turn_id
      self.will_retry = will_retry

  class _FakeInvalidParamsError(RuntimeError):
    def __init__(self, code: int, message: str, data=None):
      super().__init__(f"JSON-RPC error {code}: {message}")
      self.code = code
      self.message = message
      self.data = data

  class _FakeInvalidRequestError(RuntimeError):
    def __init__(self, code: int, message: str, data=None):
      super().__init__(f"JSON-RPC error {code}: {message}")
      self.code = code
      self.message = message
      self.data = data

  class _FakeCodexRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data=None):
      super().__init__(f"JSON-RPC error {code}: {message}")
      self.code = code
      self.message = message
      self.data = data

  class _FakePersonality:
    none = "none"

  return {
    "AgentMessageDeltaNotification": _Dummy,
    "AgentMessageThreadItem": _Dummy,
    "ApprovalMode": _FakeApprovalMode,
    "AsyncCodex": async_codex_cls,
    "AsyncGoalNotificationStream": _FakeAsyncGoalNotificationStream,
    "CodexConfig": _FakeCodexConfig,
    "CodexRpcError": _FakeCodexRpcError,
    "CommandExecutionOutputDeltaNotification": _Dummy,
    "CommandExecutionThreadItem": _Dummy,
    "ContextCompactedNotification": _Dummy,
    "ContextCompactionThreadItem": _Dummy,
    "DynamicToolCallThreadItem": _Dummy,
    "ErrorNotification": _FakeErrorNotification,
    "FileChangePatchUpdatedNotification": _Dummy,
    "FileChangeThreadItem": _Dummy,
    "ImageViewThreadItem": type("_FakeImageViewThreadItem", (), {}),
    "InvalidParamsError": _FakeInvalidParamsError,
    "InvalidRequestError": _FakeInvalidRequestError,
    "Personality": _FakePersonality,
    "ReasoningEffort": _FakeReasoningEffort(),
    "ReasoningSummary": _FakeReasoningSummary(),
    "Sandbox": _FakeSandbox,
    "ItemCompletedNotification": _Dummy,
    "ItemGuardianApprovalReviewCompletedNotification": _Dummy,
    "ItemGuardianApprovalReviewStartedNotification": _Dummy,
    "ItemStartedNotification": _Dummy,
    "MessagePhase": _FakeMessagePhase,
    "McpToolCallThreadItem": _Dummy,
    "ReasoningSummaryTextDeltaNotification": _Dummy,
    "ReasoningTextDeltaNotification": _Dummy,
    "ThreadTokenUsageUpdatedNotification": _Dummy,
    "ThreadGoalGetResponse": type("ThreadGoalGetResponse", (), {}),
    "ThreadGoalStatus": _FakeThreadGoalStatus,
    "TransportClosedError": _SdkTransportClosedError,
    "TurnCompletedNotification": _FakeTurnCompletedNotification,
    "TurnStatus": _FakeTurnStatus,
    "WebSearchThreadItem": _Dummy,
  }


def test_stamp_tool_use_id_uses_stable_item_id():
  # Every Codex ThreadItem carries a stable `id` (same id on ItemStarted /
  # ItemCompleted for one tool call); _stamp_tool_use_id threads it onto the
  # tool event so a large output can be reduced + fetched by id (contract rule
  # 6). A fake without an id is left unstamped, so event shape is unchanged.
  from types import SimpleNamespace

  event = {"type": "tool_output", "content": "x"}
  codex_sdk_runner._stamp_tool_use_id(event, SimpleNamespace(id="item_42"))
  assert event["tool_use_id"] == "item_42"

  untagged = {"type": "tool_output", "content": "x"}
  codex_sdk_runner._stamp_tool_use_id(untagged, SimpleNamespace())
  assert "tool_use_id" not in untagged

  null_id = {"type": "tool_output", "content": "x"}
  codex_sdk_runner._stamp_tool_use_id(null_id, SimpleNamespace(id=None))
  assert "tool_use_id" not in null_id


def test_tool_completed_events_emit_output_before_end():
  class CommandExecutionThreadItem:
    def __init__(self, output: str, exit_code: int = 0):
      self.aggregated_output = output
      self.exit_code = exit_code

  sdk = {"CommandExecutionThreadItem": CommandExecutionThreadItem}
  sdk.update({
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": type("WebSearchThreadItem", (), {}),
  })

  events = codex_sdk_runner._tool_completed_events(
    CommandExecutionThreadItem("hello\n"),
    sdk,
  )

  assert events == [
    {
      "type": "tool_output", "content": "hello",
      "output_complete": True, "output_exit_code": 0,
    },
    {"type": "tool_end"},
  ]

  assert codex_sdk_runner._tool_completed_events(
    CommandExecutionThreadItem("", exit_code=7), sdk,
  ) == [
    {
      "type": "tool_output", "content": "",
      "output_complete": True, "output_exit_code": 7,
    },
    {"type": "tool_end"},
  ]


def test_native_image_view_item_uses_shared_view_image_activity():
  class ImageViewThreadItem:
    id = "image-1"
    path = "/data/chats/chat-1/media/diagram.png"

  sdk = {
    "ImageViewThreadItem": ImageViewThreadItem,
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": type("WebSearchThreadItem", (), {}),
  }

  item = ImageViewThreadItem()
  assert codex_sdk_runner._tool_start_event(item, sdk) == {
    "type": "tool_start",
    "tool": "ViewImage",
    "input": "/data/chats/chat-1/media/diagram.png",
  }
  assert codex_sdk_runner._tool_completed_events(item, sdk) == [
    {"type": "tool_end"},
  ]


def test_websearch_completed_events_emit_sources_when_exposed():
  class WebSearchThreadItem:
    def __init__(self):
      self.results = [{
        "title": "Docs",
        "url": "https://example.com/docs",
        "snippet": "Search hit",
      }]

  sdk = {
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": WebSearchThreadItem,
  }

  events = codex_sdk_runner._tool_completed_events(
    WebSearchThreadItem(), sdk,
  )

  assert events == [
    {"type": "tool_sources", "sources": [{
      "title": "Docs",
      "url": "https://example.com/docs",
      "snippet": "Search hit",
    }]},
    {"type": "tool_end"},
  ]


def test_websearch_completed_events_noop_when_sdk_exposes_no_sources():
  class WebSearchThreadItem:
    query = "latest news"
    action = None

  sdk = {
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": WebSearchThreadItem,
  }

  events = codex_sdk_runner._tool_completed_events(
    WebSearchThreadItem(), sdk,
  )

  # The real query lands on completion, so it is backfilled as tool_input even
  # when the SDK exposed no result sources.
  assert events == [
    {"type": "tool_input", "input": "latest news"},
    {"type": "tool_end"},
  ]


def test_websearch_completed_events_backfill_query_and_sources():
  class WebSearchThreadItem:
    query = "site:nodejs.org Node.js 24 LTS"
    results = [{"title": "Node", "url": "https://nodejs.org/x"}]

  sdk = {
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": WebSearchThreadItem,
  }

  events = codex_sdk_runner._tool_completed_events(WebSearchThreadItem(), sdk)

  # Query backfill comes first, then sources, then end.
  assert events[0] == {
    "type": "tool_input", "input": "site:nodejs.org Node.js 24 LTS"}
  assert events[-1] == {"type": "tool_end"}
  assert {"type": "tool_sources", "sources": [
    {"title": "Node", "url": "https://nodejs.org/x"}]} in events


@pytest.mark.parametrize("action_type", ["openPage", "findInPage"])
def test_websearch_completed_events_extract_current_sdk_action_url(action_type):
  """The pinned SDK exposes visited source URLs on action.root, not results."""
  class WebSearchThreadItem:
    query = "Node.js releases"
    action = SimpleNamespace(root=SimpleNamespace(
      type=action_type,
      url="https://nodejs.org/en/blog/release/v24.0.0",
    ))

  sdk = {
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": WebSearchThreadItem,
  }

  events = codex_sdk_runner._tool_completed_events(WebSearchThreadItem(), sdk)

  assert {"type": "tool_sources", "sources": [{
    "title": "https://nodejs.org/en/blog/release/v24.0.0",
    "url": "https://nodejs.org/en/blog/release/v24.0.0",
  }]} in events


def test_steer_closed_turn_releases_reserved_row_without_dropping_handle(
  monkeypatch,
):
  sdk = _fake_sdk(async_codex_cls=object)

  async def _scenario():
    sink = _FakeSteerSink()
    active_turn = codex_sdk_runner.ActiveCodexTurn(
      object(),
      _FakeTurnHandle(
        steer_exc=sdk["InvalidParamsError"](-32602, "turn is not running"),
      ),
      chat_id="chat-1",
      sink=sink,
    )
    registry.register(active_turn)
    accepted = await active_turn.steer(
      "ping",
      [{"role": "user", "content": "ping", "cid": "ping-cid"}],
      ["ping-cid"],
    )
    while active_turn.steer_in_flight:
      await asyncio.sleep(0)
    return accepted, sink, active_turn

  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)
  accepted, sink, active = asyncio.run(_scenario())
  assert accepted is True
  assert sink.cuts == []
  assert sink.bc.events == [{
    "type": "steer_delivery_failed",
    "consume_pending_cids": ["ping-cid"],
  }]
  # The runner lifecycle still owns unregistering the live handle. A failed
  # side-channel steer must not make Stop lose reachability to the main turn.
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is active
  registry.unregister("chat-1", RunnerKind.CODEX_SDK)


def test_steer_provider_error_is_reported_as_queued_delivery_failure():
  async def _scenario():
    sink = _FakeSteerSink()
    active = codex_sdk_runner.ActiveCodexTurn(
      object(),
      _FakeTurnHandle(steer_exc=RuntimeError("real failure")),
      chat_id="chat-1",
      sink=sink,
    )
    registry.register(active)
    accepted = await active.steer(
      "ping",
      [{"role": "user", "content": "ping", "cid": "ping-cid"}],
      ["ping-cid"],
    )
    while active.steer_in_flight:
      await asyncio.sleep(0)
    registry.unregister("chat-1", RunnerKind.CODEX_SDK)
    return accepted, sink

  accepted, sink = asyncio.run(_scenario())
  assert accepted is True
  assert sink.cuts == []
  assert sink.bc.events[-1]["type"] == "steer_delivery_failed"


def test_steer_cut_failure_releases_reserved_row():
  class _FailingSink(_FakeSteerSink):
    async def commit_steer_cut(self, user_msgs, consume_pending_cids):
      del user_msgs, consume_pending_cids
      raise RuntimeError("writer unavailable")

  async def _scenario():
    sink = _FailingSink()
    active = codex_sdk_runner.ActiveCodexTurn(
      object(), _FakeTurnHandle(), chat_id="cut-failure", sink=sink,
    )
    registry.register(active)
    try:
      assert await active.steer(
        "ping",
        [{"role": "user", "content": "ping", "cid": "ping-cid"}],
        ["ping-cid"],
      ) is True
      while active.steer_in_flight:
        await asyncio.sleep(0)
      assert sink.bc.events == [{
        "type": "steer_delivery_failed",
        "consume_pending_cids": ["ping-cid"],
      }]
    finally:
      registry.unregister("cut-failure", RunnerKind.CODEX_SDK)

  asyncio.run(_scenario())


def test_concurrent_steer_is_refused_while_ack_is_pending():
  """Only the steer that atomically won admission reaches the provider."""
  async def _scenario() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingTurn(_FakeTurnHandle):
      async def steer(self, message: str):
        self.steered.append(message)
        entered.set()
        await release.wait()

    turn = _BlockingTurn()
    sink = _FakeSteerSink()
    active = codex_sdk_runner.ActiveCodexTurn(
      object(), turn, chat_id="atomic-steer", sink=sink,
    )
    registry.register(active)
    try:
      assert await active.steer(
        "first",
        [{"role": "user", "content": "first", "cid": "first-cid"}],
        ["first-cid"],
      ) is True
      await asyncio.wait_for(entered.wait(), timeout=1)
      assert active.steer_in_flight
      settlement = active._steer_attempt.task
      assert await active.steer(
        "second",
        [{"role": "user", "content": "second", "cid": "second-cid"}],
        ["second-cid"],
      ) is False
      assert turn.steered == ["first"]

      release.set()
      await asyncio.wait_for(settlement, timeout=1)
      assert not active.steer_in_flight
      assert sink.cuts == [(
        [{"role": "user", "content": "first", "cid": "first-cid"}],
        ["first-cid"],
      )]
    finally:
      registry.unregister("atomic-steer", RunnerKind.CODEX_SDK)

  asyncio.run(_scenario())


def test_turn_teardown_cancels_a_never_acknowledged_steer():
  async def _scenario():
    entered = asyncio.Event()

    class _NeverAckTurn:
      async def steer(self, _message):
        entered.set()
        await asyncio.Event().wait()

    sink = _FakeSteerSink()
    active = codex_sdk_runner.ActiveCodexTurn(
      object(), _NeverAckTurn(), chat_id="teardown-steer", sink=sink,
    )
    registry.register(active)
    try:
      assert await active.steer(
        "first",
        [{"role": "user", "content": "first", "cid": "first-cid"}],
        ["first-cid"],
      ) is True
      await asyncio.wait_for(entered.wait(), timeout=1)
      task = active._steer_attempt.task

      await active.finish_steer_before_turn_end()
      await asyncio.sleep(0)

      assert task.cancelled()
      assert sink.cuts == []
      assert sink.bc.events == [{
        "type": "steer_delivery_failed",
        "consume_pending_cids": ["first-cid"],
      }]
    finally:
      registry.unregister("teardown-steer", RunnerKind.CODEX_SDK)

  asyncio.run(_scenario())


def test_question_losing_steer_admission_race_never_parks():
  """The sole SDK reader must not park before routing a steer response."""
  class _Bc:
    run_token = None

    async def publish_question(self, _event):
      raise AssertionError("losing question must not persist or broadcast")

  class _SyncClient:
    _approval_handler = None

  class _Inner:
    _sync = _SyncClient()

  class _FakeCodex:
    _client = _Inner()

  async def _scenario() -> None:
    active = codex_sdk_runner.ActiveCodexTurn(
      object(), _FakeTurnHandle(), chat_id="steer-question-race",
    )
    active._steer_in_flight = True
    registry.register(active)
    pending: dict = {}
    try:
      codex_sdk_runner._install_request_user_input_handler(
        _FakeCodex(),
        loop=asyncio.get_running_loop(),
        chat_id="steer-question-race",
        bc=_Bc(),
        pending_questions=pending,
        db=None,
      )
      result = await asyncio.to_thread(
        _FakeCodex._client._sync._approval_handler,
        "item/tool/requestUserInput",
        {"questions": [{"id": "q1", "question": "Proceed?"}]},
      )
      assert result == {"error": {"message": (
        "Question superseded by steering input; continue with the new input."
      )}}
      assert pending == {}
    finally:
      active._steer_in_flight = False
      registry.unregister("steer-question-race", RunnerKind.CODEX_SDK)

  asyncio.run(_scenario())


def test_active_codex_turn_interrupt_waits_for_runner_finish():
  async def _scenario() -> None:
    turn = _FakeTurnHandle()
    active_turn = codex_sdk_runner.ActiveCodexTurn(
      object(), turn, chat_id="chat-1"
    )
    assert active_turn.interrupt_requested is False
    task = asyncio.create_task(active_turn.interrupt())
    await asyncio.sleep(0)
    assert turn.interrupt_calls == 1
    assert active_turn.interrupt_requested is True
    assert task.done() is False
    active_turn.mark_finished()
    await asyncio.wait_for(task, timeout=1)

  asyncio.run(_scenario())


def test_active_codex_turn_interrupt_logs_and_still_waits(caplog):
  async def _scenario() -> None:
    turn = _FakeTurnHandle(interrupt_exc=RuntimeError("interrupt failed"))
    active_turn = codex_sdk_runner.ActiveCodexTurn(
      object(), turn, chat_id="chat-1"
    )
    task = asyncio.create_task(active_turn.interrupt())
    await asyncio.sleep(0)
    assert turn.interrupt_calls == 1
    assert task.done() is False
    active_turn.mark_finished()
    await asyncio.wait_for(task, timeout=1)

  with caplog.at_level("WARNING", logger="moebius.chat"):
    asyncio.run(_scenario())

  assert "codex interrupt() raised: interrupt failed" in caplog.text


def test_active_codex_turn_interrupt_times_out_if_runner_never_finishes(caplog):
  turn = _FakeTurnHandle()

  async def _scenario() -> float:
    active_turn = codex_sdk_runner.ActiveCodexTurn(
      object(), turn, chat_id="chat-1"
    )
    start = asyncio.get_running_loop().time()
    await asyncio.wait_for(active_turn.interrupt(), timeout=5.5)
    return asyncio.get_running_loop().time() - start

  with caplog.at_level("WARNING", logger="moebius.chat"):
    elapsed = asyncio.run(_scenario())

  assert 4.8 <= elapsed < 5.5
  assert turn.interrupt_calls == 1
  assert (
    "codex active_turn._finished never resolved within 5s; runner is wedged"
    in caplog.text
  )


def test_active_codex_stop_timeout_preserves_runner_completion_future():
  async def _scenario() -> None:
    active = codex_sdk_runner.ActiveCodexTurn(
      object(), _FakeTurnHandle(), chat_id="timeout-identity",
    )

    assert await active.stop(timeout=0.01) is False
    assert active._finished.done() is False
    active.mark_finished()
    assert active._finished.done() is True

  asyncio.run(_scenario())


def test_active_codex_force_stop_signals_group_only_once(monkeypatch):
  calls: list[int] = []
  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    lambda pgid: calls.append(pgid) or True,
  )

  async def _scenario() -> None:
    active = codex_sdk_runner.ActiveCodexTurn(
      object(),
      _FakeTurnHandle(),
      chat_id="hard-stop",
      process_group_id=4321,
    )
    first = asyncio.create_task(active.force_stop(timeout=1))
    while not calls:
      await asyncio.sleep(0)
    active.mark_finished()
    assert await first is True
    assert await active.force_stop(timeout=1) is True

  asyncio.run(_scenario())
  assert calls == [4321]


def test_is_closed_turn_error_matches_sdk_rpc_errors(monkeypatch):
  sdk = _fake_sdk(async_codex_cls=object)
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  invalid_params = sdk["InvalidParamsError"](-32602, "turn is not running")
  rpc_error = sdk["CodexRpcError"](-32000, "turn closed")

  assert codex_sdk_runner._is_closed_turn_error(invalid_params) is True
  assert codex_sdk_runner._is_closed_turn_error(rpc_error) is True


def test_is_closed_turn_error_does_not_treat_arbitrary_oserror_as_closed():
  assert codex_sdk_runner._is_closed_turn_error(OSError("disk full")) is False


def test_is_transport_death_rejects_provider_runtime_error_about_not_running(
  monkeypatch,
):
  """The narrow predicate is the whole reason the two exist separately.

  The runner reraises every non-retryable provider ErrorNotification as a
  plain `RuntimeError(message)`, and those messages routinely say "is not
  running". _is_closed_turn_error accepts that text because the only cost
  of a false positive there is a refused steer; the except-path
  reclassification must not, or a stop racing a genuine MCP failure would
  swallow the only report the owner ever gets. Widening the guard back to
  _is_closed_turn_error fails here.
  """
  sdk = _fake_sdk(async_codex_cls=object)
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  provider_fault = RuntimeError("MCP server 'x' is not running")

  assert codex_sdk_runner._is_transport_death(provider_fault) is False
  assert codex_sdk_runner._is_closed_turn_error(provider_fault) is True


def test_is_transport_death_binds_to_the_sdk_class_not_to_its_name():
  """Uses the installed SDK, because the binding is what is under test.

  isinstance against the symbol `_sdk_imports()` exposes means a genuine
  subclass matches and a same-named impostor does not — neither of which a
  class-name comparison could get right.
  """
  pytest.importorskip("openai_codex")

  class _MoreSpecificTransportError(_SdkTransportClosedError):
    pass

  impostor = type("TransportClosedError", (Exception,), {})

  assert codex_sdk_runner._is_transport_death(
    _MoreSpecificTransportError("closed stdout")
  ) is True
  assert codex_sdk_runner._is_transport_death(
    impostor("closed stdout")
  ) is False


def test_run_codex_sdk_turn_resume_mismatch_reseeds_without_error(monkeypatch):
  # A resume that returns a different thread id (a lost session) now reseeds and
  # continues instead of erroring. Even with NO prior-history context to inject,
  # it must not dead-end: it rides the fresh thread and publishes session_init,
  # never an error. (Companion to the reseed+record test, which covers the
  # history-injection and activity-event side.)
  completed_turn = SimpleNamespace(id="turn-m", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  mismatched_thread = _FakeThread("actual-thread", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, *_args, **_kwargs):
      return mismatched_thread

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id="requested-thread",
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
    )
  )

  assert result["session_id"] == "actual-thread"
  assert result["error"] is None
  assert {"type": "session_init", "session_id": "actual-thread"} in bc.events
  assert not any(e.get("type") == "error" for e in bc.events)


def test_run_codex_sdk_turn_resume_skips_skill_lookup(monkeypatch):
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    )
  ]
  resumed_thread = _FakeThread("requested-thread", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, *_args, **_kwargs):
      return resumed_thread

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(
    codex_sdk_runner,
    "get_skill_path",
    lambda: pytest.fail("get_skill_path() should not run on resume"),
  )

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id="requested-thread",
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
    )
  )

  assert result == {
    "session_id": "requested-thread",
    "cost_usd": None,
    "error": None,
  }
  assert bc.events == [{
    "type": "session_init",
    "session_id": "requested-thread",
  }]
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None


def test_new_goal_uses_sdk_logical_operation_not_ordinary_turn(monkeypatch):
  ordinary_turn = _FakeTurnHandle(_goal_completion_notifications())
  thread = _FakeThread("thread-1", ordinary_turn)

  class FakeAsyncCodex:
    last = None

    def __init__(self, config=None):
      self.config = config
      self._client = _FakeGoalClient()
      type(self).last = self

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, **_kwargs):
      return thread

  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="/goal Repair every failing test",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-goal",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
    goal_objective="Repair every failing test",
    goal_mode=True,
  ))

  assert result["error"] is None
  assert FakeAsyncCodex.last._client.start_calls == [
    ("thread-1", "Repair every failing test"),
  ]
  assert thread.turn_args is None


def test_active_goal_route_exists_before_resume_and_auto_continues(monkeypatch):
  goal = SimpleNamespace(status=_FakeThreadGoalStatus.active)
  ordinary_turn = _FakeTurnHandle(_goal_completion_notifications())
  thread = _FakeThread("thread-1", ordinary_turn)

  class FakeAsyncCodex:
    last = None

    def __init__(self, config=None):
      self.config = config
      self._client = _FakeGoalClient(goal)
      type(self).last = self

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, thread_id, **_kwargs):
      assert self._client.register_calls == [thread_id]
      assert self._client.state is not None
      self._client.state.start()
      self._client.state.notifications = _goal_completion_notifications()
      return thread

  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="continue",
    session_id="thread-1",
    base_env={},
    cwd="/tmp",
    chat_id="chat-goal",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
    goal_mode=True,
    goal_continue=True,
  ))

  client = FakeAsyncCodex.last._client
  assert result["error"] is None
  assert client.register_calls == ["thread-1"]
  assert client.set_calls == []
  assert client.steer_calls == []
  assert thread.turn_args is None


def test_missing_goal_thread_keeps_existing_fresh_thread_recovery(monkeypatch):
  ordinary_turn = _FakeTurnHandle(_goal_completion_notifications())
  replacement_thread = _FakeThread("thread-2", ordinary_turn)
  sdk_holder = {}

  class MissingGoalThreadClient(_FakeGoalClient):
    async def request(self, method, params, *, response_model):
      assert method == "thread/goal/get"
      raise sdk_holder["sdk"]["InvalidRequestError"](
        -32600, "thread not found: thread-1",
      )

  class FakeAsyncCodex:
    last = None

    def __init__(self, config=None):
      self.config = config
      self._client = MissingGoalThreadClient()
      type(self).last = self

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, _thread_id, **_kwargs):
      return replacement_thread

  sdk_holder["sdk"] = _fake_sdk(FakeAsyncCodex)
  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: sdk_holder["sdk"],
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="continue",
    session_id="thread-1",
    base_env={},
    cwd="/tmp",
    chat_id="chat-goal",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
    goal_mode=True,
    goal_continue=True,
    fallback_goal_objective="legacy objective",
  ))

  client = FakeAsyncCodex.last._client
  assert result["error"] is None
  assert result["session_id"] == "thread-2"
  assert client.start_calls == []
  assert replacement_thread.turn_args is not None


def test_paused_goal_reactivates_and_steers_real_owner_message(monkeypatch):
  goal = SimpleNamespace(status=_FakeThreadGoalStatus.paused)
  ordinary_turn = _FakeTurnHandle(_goal_completion_notifications())
  thread = _FakeThread("thread-1", ordinary_turn)

  class FakeAsyncCodex:
    last = None

    def __init__(self, config=None):
      self.config = config
      self._client = _FakeGoalClient(goal)
      type(self).last = self

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, thread_id, **_kwargs):
      assert self._client.register_calls == [thread_id]
      return thread

  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="Use the smaller durable design",
    session_id="thread-1",
    base_env={},
    cwd="/tmp",
    chat_id="chat-goal",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
    goal_mode=True,
  ))

  client = FakeAsyncCodex.last._client
  assert result["error"] is None
  assert client.set_calls == [
    ("thread-1", _FakeThreadGoalStatus.active),
  ]
  assert client.steer_calls == [
    ("thread-1", "physical-turn", "Use the smaller durable design"),
  ]
  assert thread.turn_args is None


def test_goal_turn_interrupt_uses_sdk_pause_and_interrupt_operation():
  async def scenario():
    client = _FakeGoalClient()
    state = _FakeGoalState("thread-1", started=True)
    turn = codex_sdk_runner._CodexGoalTurn(
      client,
      state,
      _FakeAsyncGoalNotificationStream,
      RuntimeError,
    )
    await turn.interrupt()
    assert client.cancel_calls == [state]

  asyncio.run(scenario())


def test_goal_turn_steer_follows_physical_turn_rollover():
  async def scenario():
    sdk = _fake_sdk(object)
    state = _FakeGoalState("thread-1", started=True)

    class RolloverClient(_FakeGoalClient):
      async def turn_steer(self, thread_id, expected_turn_id, message):
        self.steer_calls.append((thread_id, expected_turn_id, message))
        if expected_turn_id == "physical-turn":
          state._current_turn_id = "physical-turn-2"
          raise sdk["InvalidRequestError"](
            -32600,
            "expected active turn id `physical-turn` but found "
            "`physical-turn-2`",
          )

    client = RolloverClient()
    turn = codex_sdk_runner._CodexGoalTurn(
      client,
      state,
      _FakeAsyncGoalNotificationStream,
      sdk["InvalidRequestError"],
    )
    await turn.steer("keep the owner message")
    assert client.steer_calls == [
      ("thread-1", "physical-turn", "keep the owner message"),
      ("thread-1", "physical-turn-2", "keep the owner message"),
    ]

  asyncio.run(scenario())


def test_goal_clear_uses_sdk_without_starting_an_ordinary_turn(monkeypatch):
  goal = SimpleNamespace(status=_FakeThreadGoalStatus.active)
  ordinary_turn = _FakeTurnHandle(_goal_completion_notifications())
  thread = _FakeThread("thread-1", ordinary_turn)

  class FakeAsyncCodex:
    last = None

    def __init__(self, config=None):
      self.config = config
      self._client = _FakeGoalClient(goal)
      type(self).last = self

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, _thread_id, **_kwargs):
      return thread

  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )

  bc = _FakeBroadcast()
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="/goal clear",
    session_id="thread-1",
    base_env={},
    cwd="/tmp",
    chat_id="chat-goal",
    bc=bc,
    pending_questions={},
    db=None,
    goal_clear=True,
    goal_mode=True,
  ))

  assert result["error"] is None
  assert FakeAsyncCodex.last._client.clear_calls == ["thread-1"]
  assert thread.turn_args is None
  assert {"type": "text", "content": "Goal cleared."} in bc.events


def test_run_codex_sdk_turn_reports_all_model_calls_in_turn(monkeypatch):
  class TokenUsageUpdated:
    def __init__(self, token_usage):
      self.token_usage = token_usage

  class Usage:
    def __init__(self, last, total):
      self.last = SimpleNamespace(**last)
      self.total = SimpleNamespace(**total)
      self.model_context_window = 200_000

    def model_dump(self, **_kwargs):
      return {
        "last": vars(self.last),
        "total": vars(self.total),
        "modelContextWindow": self.model_context_window,
      }

  first = Usage(
    last={
      "input_tokens": 200, "cached_input_tokens": 100,
      "output_tokens": 100, "reasoning_output_tokens": 50,
      "total_tokens": 300,
    },
    total={
      "input_tokens": 1_000, "cached_input_tokens": 400,
      "output_tokens": 100, "reasoning_output_tokens": 50,
      "total_tokens": 1_100,
    },
  )
  final = Usage(
    last={
      "input_tokens": 300, "cached_input_tokens": 150,
      "output_tokens": 100, "reasoning_output_tokens": 50,
      "total_tokens": 400,
    },
    total={
      "input_tokens": 1_700, "cached_input_tokens": 800,
      "output_tokens": 200, "reasoning_output_tokens": 100,
      "total_tokens": 1_900,
    },
  )
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="thread/tokenUsage/updated",
      payload=TokenUsageUpdated(first),
    ),
    SimpleNamespace(
      method="thread/tokenUsage/updated",
      payload=TokenUsageUpdated(final),
    ),
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  thread = _FakeThread("thread-usage", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk["ThreadTokenUsageUpdatedNotification"] = TokenUsageUpdated
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="measure this turn",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-usage",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
  ))

  assert result["usage"]["total"]["total_tokens"] == 1_900
  assert result["usage_metrics"]["calculation"] == "thread_delta"
  assert result["usage_metrics"]["input_tokens"] == 900
  assert result["usage_metrics"]["total_tokens"] == 1_100


def test_run_codex_sdk_turn_resume_validation_error_now_propagates(monkeypatch, caplog):
  # Inverse of the old subAgentActivity resume test. The SDK now models
  # subAgentActivity natively, so thread_resume no longer raises on that
  # history and the compatibility fallback is gone. As a result, any resume
  # validation error is a REAL failure again: it must surface as an error
  # result, never be swallowed into a fake success + session_init.
  class ResumeValidationError(Exception):
    pass

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, *_args, **_kwargs):
      raise ResumeValidationError("264 validation errors for ThreadResumeResponse")

  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )

  bc = _FakeBroadcast()
  with caplog.at_level("WARNING", logger="moebius.chat"):
    result = asyncio.run(
      codex_sdk_runner.run_codex_sdk_turn(
        user_message="continue",
        session_id="requested-thread",
        base_env={},
        cwd="/tmp",
        chat_id="chat-1",
        bc=bc,
        pending_questions={},
        db=None,
      )
    )

  assert result["session_id"] == "requested-thread"
  assert result["error"] is not None
  assert "ThreadResumeResponse" in result["error"]
  # No session_init: the turn never reached a successful resume.
  assert bc.events == []
  # The removed fallback's warning must not reappear.
  assert "rejected subAgentActivity history" not in caplog.text


def test_subagent_activity_item_dispatch_stays_out_of_tool_stream():
  # The native subAgentActivity marker is classified explicitly at both
  # dispatch sites as a no-op: it opens and closes no Möbius tool block (the
  # live delegation rides CollabAgentToolCallThreadItem's Task events instead).
  class SubAgentActivityThreadItem:
    def __init__(self):
      self.kind = "started"
      self.agent_path = "/root/scout"
      self.agent_thread_id = "thread-1"

  sdk = {"SubAgentActivityThreadItem": SubAgentActivityThreadItem}
  item = SubAgentActivityThreadItem()

  assert codex_sdk_runner._tool_start_event(item, sdk) is None
  assert codex_sdk_runner._tool_completed_events(item, sdk) == []

  started = codex_sdk_runner._subagent_lifecycle_event(
    item, sdk, provider_session_id="root-thread", occurred_at=123_000,
    provider_activation_id="activation-1",
  )
  assert started["type"] == "agent_lifecycle"
  assert started["provider"] == "codex"
  assert started["provider_session_id"] == "root-thread"
  assert started["provider_agent_id"] == "thread-1"
  assert started["event_type"] == "agent_started"
  assert started["state"] == "running"
  assert started["agent_type"] == "/root/scout"
  assert "summary" not in started
  assert started["occurred_at"] == 123_000
  assert started["provider_activation_id"] == "activation-1"

  item.kind = "interrupted"
  assert codex_sdk_runner._subagent_lifecycle_event(
    item, sdk, provider_session_id="root-thread",
  )["event_type"] == "agent_terminal"


def test_codex_lifecycle_reactivation_and_terminal_state_mapping():
  class CollabItem:
    pass

  class AgentState:
    def __init__(self, status, message=None):
      self.status = status
      self.message = message

  sdk = {"CollabAgentToolCallThreadItem": CollabItem}
  item = CollabItem()
  item.id = "call-resume"
  item.tool = "resumeAgent"
  item.sender_thread_id = "root-thread"
  item.receiver_thread_ids = ["child-thread"]
  item.prompt = "Read the confidential acquisition plan"
  item.agents_states = {}
  active = {}
  known = set()
  by_call = {}
  last = {}

  starts = codex_sdk_runner._collab_reactivation_events(
    item, sdk, root_thread_id="root-thread", occurred_at=123_000,
    active=active, known=known,
    activation_by_call_child=by_call, last_activation_by_child=last,
  )
  assert len(starts) == 1
  assert starts[0]["provider_activation_id"] == "call-resume:child-thread"
  assert starts[0]["parent_kind"] == "main"
  assert starts[0]["occurred_at"] == 123_000
  assert "summary" not in starts[0]
  normalized = normalize_chat_event(
    chat_id="chat", chat_run_id="run", event=starts[0],
  )
  assert normalized["summary"] is None
  assert "confidential acquisition" not in repr(normalized)
  assert active == {"child-thread": "call-resume:child-thread"}

  item.agents_states = {
    "child-thread": AgentState("completed", "Review complete"),
  }
  terminals = codex_sdk_runner._collab_completion_events(
    item, sdk, root_thread_id="root-thread", occurred_at=125_000,
    active=active, known=known,
    activation_by_call_child=by_call, last_activation_by_child=last,
  )
  assert len(terminals) == 1
  assert terminals[0]["state"] == "done"
  assert terminals[0]["provider_activation_id"] == "call-resume:child-thread"
  assert terminals[0]["occurred_at"] == 125_000
  assert active == {}


def test_codex_thread_status_closes_known_child_and_ignores_root():
  class StatusPayload:
    def __init__(self, thread_id, status):
      self.thread_id = thread_id
      self.status = SimpleNamespace(root=SimpleNamespace(type=status))

  active = {"child": "activation-1"}
  known = {"child"}
  counts = {}
  last = {}
  done = codex_sdk_runner._thread_status_lifecycle_event(
    StatusPayload("child", "idle"), root_thread_id="root",
    active=active, known=known, activation_counts=counts,
    last_activation_by_child=last,
  )
  assert done["event_type"] == "agent_terminal"
  assert done["state"] == "done"
  assert done["provider_activation_id"] == "activation-1"
  assert active == {}
  assert codex_sdk_runner._thread_status_lifecycle_event(
    StatusPayload("root", "idle"), root_thread_id="root",
    active={"root": "bad"}, known={"root"}, activation_counts={},
    last_activation_by_child={},
  ) is None


def test_codex_late_completion_targets_its_call_without_closing_new_activation():
  class CollabItem:
    pass

  class AgentState:
    def __init__(self, status):
      self.status = status
      self.message = None

  class StatusPayload:
    def __init__(self, status):
      self.thread_id = "child"
      self.status = SimpleNamespace(root=SimpleNamespace(type=status))

  sdk = {"CollabAgentToolCallThreadItem": CollabItem}
  active, by_call, last, counts = {}, {}, {}, {}
  known = set()

  def call(call_id, operation="resumeAgent"):
    item = CollabItem()
    item.id = call_id
    item.tool = operation
    item.sender_thread_id = "root"
    item.receiver_thread_ids = ["child"]
    item.prompt = "Continue"
    item.agents_states = {}
    return item

  call_a = call("call-a")
  start_a = codex_sdk_runner._collab_reactivation_events(
    call_a, sdk, root_thread_id="root", occurred_at=100,
    active=active, known=known, activation_by_call_child=by_call,
    last_activation_by_child=last,
  )
  assert start_a[0]["provider_activation_id"] == "call-a:child"

  # Status is a useful observed terminal, but the call association survives so
  # a later exact completion can still refine this same activation.
  observed_done = codex_sdk_runner._thread_status_lifecycle_event(
    StatusPayload("idle"), root_thread_id="root", active=active, known=known,
    activation_counts=counts, last_activation_by_child=last,
  )
  assert observed_done["provider_activation_id"] == "call-a:child"
  assert active == {}

  call_b = call("call-b")
  start_b = codex_sdk_runner._collab_reactivation_events(
    call_b, sdk, root_thread_id="root", occurred_at=200,
    active=active, known=known, activation_by_call_child=by_call,
    last_activation_by_child=last,
  )
  assert start_b[0]["provider_activation_id"] == "call-b:child"
  call_a.agents_states = {"child": AgentState("errored")}
  late_a = codex_sdk_runner._collab_completion_events(
    call_a, sdk, root_thread_id="root", occurred_at=250,
    active=active, known=known, activation_by_call_child=by_call,
    last_activation_by_child=last,
  )
  assert late_a[0]["provider_activation_id"] == "call-a:child"
  assert late_a[0]["state"] == "failed"
  assert active == {"child": "call-b:child"}


def test_codex_input_to_running_child_is_progress_and_nested_spawn_uses_current_parent():
  class CollabItem:
    pass

  sdk = {"CollabAgentToolCallThreadItem": CollabItem}
  active = {"child": "resume-b:child"}
  known = {"child"}
  by_call, last = {}, {"child": "resume-b:child"}
  item = CollabItem()
  item.id = "send-input"
  item.tool = "sendInput"
  item.sender_thread_id = "root"
  item.receiver_thread_ids = ["child"]
  item.prompt = "One more check"
  assert codex_sdk_runner._collab_reactivation_events(
    item, sdk, root_thread_id="root", occurred_at=300,
    active=active, known=known, activation_by_call_child=by_call,
    last_activation_by_child=last,
  ) == []
  assert active == {"child": "resume-b:child"}
  assert by_call[("send-input", "child")] == "resume-b:child"

  payload = SimpleNamespace(thread=SimpleNamespace(
    id="grandchild", parent_thread_id="child", agent_role="reviewer",
    agent_nickname=None, preview="Confidential customer list", created_at=301,
  ))
  nested = codex_sdk_runner._thread_started_lifecycle_event(
    payload, root_thread_id="root",
    parent_provider_activation_id=active["child"],
  )
  assert nested["parent_kind"] == "agent"
  assert nested["parent_provider_activation_id"] == "resume-b:child"
  assert "summary" not in nested
  normalized = normalize_chat_event(
    chat_id="chat", chat_run_id="run", event=nested,
  )
  assert normalized["summary"] is None
  assert "Confidential customer list" not in repr(normalized)


def test_codex_lifecycle_maps_to_shared_task_chip_contract():
  started = {
    "event_type": "agent_spawned",
    "provider_agent_id": "child",
    "provider_activation_id": "thread-started:child",
    "agent_type": "researcher",
    "state": "running",
  }
  done = {
    **started,
    "event_type": "agent_terminal",
    "state": "done",
    "summary": "Found the cause",
  }
  assert codex_sdk_runner._public_task_event(
    started, tool_use_id="host-1",
  ) == {
    "type": "task_start",
    "task_id": "thread-started:child",
    "description": "researcher",
    "task_type": "researcher",
    "tool_use_id": "host-1",
  }
  assert codex_sdk_runner._public_task_event(
    done, tool_use_id="host-1",
  ) == {
    "type": "task_done",
    "task_id": "thread-started:child",
    "status": "done",
    "summary": "Found the cause",
    "tool_use_id": "host-1",
  }


def test_run_codex_sdk_turn_dispatches_lifecycle_sequence_with_late_exact_fact(
  monkeypatch,
):
  class CollabItem:
    def __init__(self, item_id, tool, receivers, states=None):
      self.id = item_id
      self.tool = tool
      self.sender_thread_id = "root"
      self.receiver_thread_ids = receivers
      self.agents_states = states or {}
      self.prompt = "Delegate"

  class AgentState:
    def __init__(self, status):
      self.status = status
      self.message = status

  class ItemStarted:
    def __init__(self, item, at):
      self.item = item
      self.started_at_ms = at

  class ItemCompleted:
    def __init__(self, item, at):
      self.item = item
      self.completed_at_ms = at

  class ThreadStarted:
    def __init__(self, thread):
      self.thread = thread

  class ThreadStatus:
    def __init__(self, thread_id, status):
      self.thread_id = thread_id
      self.status = SimpleNamespace(root=SimpleNamespace(type=status))

  class SubActivity:
    def __init__(self):
      self.id = "late-interrupt"
      self.kind = "interrupted"
      self.agent_path = "/root/child"
      self.agent_thread_id = "child"

  spawn = CollabItem("call-a", "spawnAgent", ["child"])
  resume = CollabItem("call-b", "resumeAgent", ["child"])
  late_spawn = CollabItem(
    "call-a", "spawnAgent", ["child"], {"child": AgentState("errored")},
  )
  done_resume = CollabItem(
    "call-b", "resumeAgent", ["child"], {"child": AgentState("completed")},
  )
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(method="item/started", payload=ItemStarted(spawn, 100)),
    SimpleNamespace(method="thread/started", payload=ThreadStarted(SimpleNamespace(
      id="child", parent_thread_id="root", agent_role="researcher",
      agent_nickname=None, preview="Research", created_at=101,
    ))),
    SimpleNamespace(method="thread/status/changed", payload=ThreadStatus("child", "idle")),
    SimpleNamespace(method="item/started", payload=ItemStarted(resume, 200)),
    SimpleNamespace(method="item/completed", payload=ItemCompleted(done_resume, 260)),
    SimpleNamespace(method="thread/started", payload=ThreadStarted(SimpleNamespace(
      id="grandchild", parent_thread_id="child", agent_role="reviewer",
      agent_nickname=None, preview="Review", created_at=270,
    ))),
    SimpleNamespace(method="item/started", payload=ItemStarted(SubActivity(), 275)),
    SimpleNamespace(method="item/completed", payload=ItemCompleted(late_spawn, 250)),
    SimpleNamespace(
      method="turn/completed", payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  thread = _FakeThread("root", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk.update({
    "CollabAgentToolCallThreadItem": CollabItem,
    "ItemStartedNotification": ItemStarted,
    "ItemCompletedNotification": ItemCompleted,
    "ThreadStartedNotification": ThreadStarted,
    "ThreadStatusChangedNotification": ThreadStatus,
    "SubAgentActivityThreadItem": SubActivity,
  })
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  async def no_links(*_args, **_kwargs):
    return None

  monkeypatch.setattr(codex_sdk_runner, "_record_collab_child_links", no_links)
  bus = _FakeBroadcast()
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="delegate", session_id=None, base_env={}, cwd="/tmp",
    chat_id="chat-sequence", bc=bus, pending_questions={}, db=None,
  ))

  assert result["error"] is None
  late = next(event for event in bus.lifecycle_events
              if event.get("source_event_id") == "call-a:child:errored")
  resumed = next(event for event in bus.lifecycle_events
                 if event.get("source_event_id") == "call-b:child:started")
  nested = next(event for event in bus.lifecycle_events
                if event.get("provider_agent_id") == "grandchild")
  completed = next(event for event in bus.lifecycle_events
                   if event.get("source_event_id") == "call-b:child:completed")
  interrupted = next(event for event in bus.lifecycle_events
                     if event.get("source_event_id") == "late-interrupt")
  assert late["provider_activation_id"] == "thread-started:child"
  assert resumed["provider_activation_id"] == "call-b:child"
  assert nested["parent_provider_activation_id"] == "call-b:child"
  assert completed["provider_activation_id"] == "call-b:child"
  assert interrupted["provider_activation_id"] == "call-b:child"
  assert all(event.get("type") != "agent_lifecycle" for event in bus.events)
  hosts = [
    event for event in bus.events
    if event.get("type") == "tool_start" and event.get("tool") == "Task"
  ]
  assert len(hosts) == 1
  host_id = hosts[0]["tool_use_id"]
  starts = {
    event["task_id"]
    for event in bus.events if event.get("type") == "task_start"
  }
  assert starts == {
    "thread-started:child",
    "call-b:child",
    "thread-started:grandchild",
  }
  assert any(
    event.get("type") == "task_done"
    and event.get("task_id") == "thread-started:child"
    and event.get("status") == "done"
    for event in bus.events
  )
  assert any(
    event.get("type") == "task_done"
    and event.get("task_id") == "thread-started:grandchild"
    and event.get("status") == "stopped"
    for event in bus.events
  )
  assert all(
    event.get("tool_use_id") == host_id
    for event in bus.events
    if event.get("type") in ("task_start", "task_done")
  )
  assert bus.events[-1] == {"type": "tool_end", "tool_use_id": host_id}


def test_run_codex_sdk_turn_aborts_after_turn_before_stream_registration(monkeypatch):
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  turn_handle = _FakeTurnHandle([
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ])
  thread = _FakeThread("thread-1", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
      should_abort=lambda: thread.turn_args is not None,
    )
  )

  assert result == {
    "session_id": "thread-1",
    "cost_usd": None,
    "error": None,
  }
  assert turn_handle.interrupt_calls == 1
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None
  assert bc.events == [{
    "type": "session_init",
    "session_id": "thread-1",
  }]


def test_run_codex_sdk_turn_cleans_up_active_session_on_stream_exception(
  monkeypatch,
):
  class AgentMessageDeltaNotification:
    def __init__(self, delta: str):
      self.delta = delta

  turn_handle = _FakeTurnHandle(
    notifications=[
      SimpleNamespace(
        method="agent_message/delta",
        payload=AgentMessageDeltaNotification("partial"),
      )
    ],
    stream_exc=RuntimeError("stream blew up"),
  )
  thread = _FakeThread("thread-1", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk["AgentMessageDeltaNotification"] = AgentMessageDeltaNotification
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)
  mark_finished_calls: list[bool] = []
  original_mark_finished = codex_sdk_runner.ActiveCodexTurn.mark_finished

  def _mark_finished(self):
    mark_finished_calls.append(True)
    original_mark_finished(self)

  monkeypatch.setattr(
    codex_sdk_runner.ActiveCodexTurn,
    "mark_finished",
    _mark_finished,
  )

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
    )
  )

  assert result["error"] == "stream blew up"
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None
  assert mark_finished_calls == [True]


@pytest.mark.parametrize(
  "event_kind",
  ["context_compaction_item", "legacy_notification"],
)
def test_run_codex_sdk_turn_publishes_marker_from_current_and_legacy_events(
  monkeypatch, event_kind,
):
  class ContextCompactionThreadItem:
    pass

  class ContextCompactedNotification:
    pass

  class ItemCompletedNotification:
    def __init__(self, item):
      self.item = SimpleNamespace(root=item)

  if event_kind == "context_compaction_item":
    event = SimpleNamespace(
      method="item/completed",
      payload=ItemCompletedNotification(ContextCompactionThreadItem()),
    )
  else:
    event = SimpleNamespace(
      method="thread/compacted",
      payload=ContextCompactedNotification(),
    )

  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  turn_handle = _FakeTurnHandle([
    event,
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ])
  thread = _FakeThread("thread-1", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk["ContextCompactedNotification"] = ContextCompactedNotification
  sdk["ContextCompactionThreadItem"] = ContextCompactionThreadItem
  sdk["ItemCompletedNotification"] = ItemCompletedNotification
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  bc = _FakeBroadcast()
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-compaction",
    bc=bc,
    pending_questions={},
    db=None,
  ))

  assert result["error"] is None
  markers = [
    event for event in bc.events
    if event.get("type") == "context_compacted"
  ]
  assert markers == [{
    "type": "context_compacted",
    "provider": "codex",
  }]


class _KilledTransportError(_SdkTransportClosedError):
  """A subclass of the SDK's own TransportClosedError.

  Subclassing the real symbol rather than redeclaring its name is the point:
  the runner reclassifies on isinstance, so a look-alike would prove nothing
  and a genuine subclass would not match a name check. It is also not a
  RuntimeError, which keeps the narrow transport branch distinguishable from
  the looser bare-RuntimeError-text branch that belongs to
  _is_closed_turn_error alone.
  """


def _run_turn_whose_stream_dies(
  monkeypatch,
  exc: Exception,
  *,
  on_register=None,
  should_abort=None,
  notifications=None,
  sdk_patch=None,
):
  """Runs one turn whose stream raises `exc`, optionally mid-teardown.

  `on_register` fires against the handle the runner has just registered —
  the same window in which a real Stop reaches a live turn — so a test can
  mark the teardown as ours before the stream dies.
  `notifications` are delivered before the death, so a test can give the
  turn something to have spent; `sdk_patch` supplies the payload classes
  those notifications need to be recognized as.
  """
  turn_handle = _FakeTurnHandle(notifications, stream_exc=exc)
  thread = _FakeThread("thread-1", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk.update(sdk_patch or {})
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  if on_register is not None:
    original_register = registry.register

    def _register(handle):
      on_register(handle)
      return original_register(handle)

    monkeypatch.setattr(registry, "register", _register)

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
      **({"should_abort": should_abort} if should_abort else {}),
    )
  )
  return result, bc


def _mark_interrupted(handle):
  handle._interrupt_requested = True


def test_run_codex_sdk_turn_reports_self_requested_kill_as_interrupted(
  monkeypatch, caplog,
):
  """A stop we asked for must not read as a provider failure.

  A forced stop closes the transport before turn/completed. Reporting the
  resulting "closed stdout" as an error would blame Codex for our teardown.
  """
  result, bc = _run_turn_whose_stream_dies(
    monkeypatch,
    _KilledTransportError("Codex process closed stdout. stderr_tail="),
    on_register=_mark_interrupted,
  )

  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"
  assert [e for e in bc.events if e.get("type") == "error"] == []
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None
  assert any(
    record.levelname == "WARNING"
    and "Codex transport closed by our own stop" in record.message
    and "closed stdout" in record.message
    for record in caplog.records
  )


def test_run_codex_sdk_turn_self_requested_kill_still_reports_usage(
  monkeypatch,
):
  """A stop we asked for is a clean interrupt, but the tokens still count.

  Usage delivered before the self-kill must survive the transport-death
  reclassification: the interrupted (error=None) exit routes through
  with_usage just like every other exit, so a stopped turn is not free in
  the budget ledger.
  """
  class TokenUsageUpdated:
    def __init__(self, token_usage):
      self.token_usage = token_usage

  class Usage:
    def __init__(self, total_tokens):
      self.last = SimpleNamespace(
        input_tokens=200, cached_input_tokens=100, output_tokens=100,
        reasoning_output_tokens=50, total_tokens=300,
      )
      self.total = SimpleNamespace(
        input_tokens=1_000, cached_input_tokens=400, output_tokens=100,
        reasoning_output_tokens=50, total_tokens=total_tokens,
      )
      self.model_context_window = 200_000

    def model_dump(self, **_kwargs):
      return {
        "last": vars(self.last),
        "total": vars(self.total),
        "modelContextWindow": self.model_context_window,
      }

  result, _bc = _run_turn_whose_stream_dies(
    monkeypatch,
    _KilledTransportError("Codex process closed stdout. stderr_tail="),
    on_register=_mark_interrupted,
    notifications=[
      SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=TokenUsageUpdated(Usage(1_100)),
      ),
    ],
    sdk_patch={"ThreadTokenUsageUpdatedNotification": TokenUsageUpdated},
  )

  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"
  assert result["usage"]["total"]["total_tokens"] == 1_100
  assert "usage_metrics" in result
  assert len(result["usage_metrics"]["model_calls"]) == 1


def test_run_codex_sdk_turn_unrequested_transport_death_stays_an_error(
  monkeypatch,
):
  """The same transport death with no stop pending is still a real failure.

  Only a teardown we asked for may be reclassified, so a provider-side
  crash keeps its error.
  """
  result, _bc = _run_turn_whose_stream_dies(
    monkeypatch,
    _KilledTransportError("Codex process closed stdout. stderr_tail="),
  )

  assert result["error"] == "Codex process closed stdout. stderr_tail="
  assert result.get("terminal_status") is None
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None


def test_run_codex_sdk_turn_non_transport_failure_during_stop_stays_an_error(
  monkeypatch,
):
  """A pending stop must not launder an unrelated bug into a clean stop.

  Without this, any defect that happens to fire inside a stop window
  returns "interrupted" with no error and no log line — invisible in both
  the transcript and chat.log.
  """
  result, _bc = _run_turn_whose_stream_dies(
    monkeypatch,
    ValueError("unexpected notification payload"),
    on_register=_mark_interrupted,
  )

  assert result["error"] == "unexpected notification payload"
  assert result.get("terminal_status") is None


def test_run_codex_sdk_turn_provider_fault_during_stop_keeps_its_error(
  monkeypatch,
):
  """A provider fault that merely looks closed-ish must still be reported.

  Non-retryable provider errors reach this except path as plain
  RuntimeErrors carrying the app-server's own words, and "is not running"
  is ordinary phrasing for a broken MCP server. Only the transport itself
  dying may be reclassified: if the guard is widened to
  _is_closed_turn_error, a stop in flight turns a real failure into a
  silent clean interrupt.
  """
  result, _bc = _run_turn_whose_stream_dies(
    monkeypatch,
    RuntimeError("MCP server 'x' is not running"),
    on_register=_mark_interrupted,
  )

  assert result["error"] == "MCP server 'x' is not running"
  assert result.get("terminal_status") is None


def test_run_codex_sdk_turn_failed_turn_still_reports_what_it_spent(
  monkeypatch,
):
  """Tokens burned before a crash are still tokens burned.

  Every other exit routes its usage through with_usage; the raw error exit
  used to drop it, so a turn that streamed for minutes and then died looked
  free in the budget ledger.
  """
  class TokenUsageUpdated:
    def __init__(self, token_usage):
      self.token_usage = token_usage

  class Usage:
    def __init__(self, total_tokens):
      self.last = SimpleNamespace(
        input_tokens=200, cached_input_tokens=100, output_tokens=100,
        reasoning_output_tokens=50, total_tokens=300,
      )
      self.total = SimpleNamespace(
        input_tokens=1_000, cached_input_tokens=400, output_tokens=100,
        reasoning_output_tokens=50, total_tokens=total_tokens,
      )
      self.model_context_window = 200_000

    def model_dump(self, **_kwargs):
      return {
        "last": vars(self.last),
        "total": vars(self.total),
        "modelContextWindow": self.model_context_window,
      }

  result, _bc = _run_turn_whose_stream_dies(
    monkeypatch,
    ValueError("unexpected notification payload"),
    notifications=[
      SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=TokenUsageUpdated(Usage(1_100)),
      ),
    ],
    sdk_patch={"ThreadTokenUsageUpdatedNotification": TokenUsageUpdated},
  )

  assert result["error"] == "unexpected notification payload"
  assert result["usage"]["total"]["total_tokens"] == 1_100
  # One update means no thread delta to compute, so the exact numbers are
  # normalize_codex_usage's business; what this test owns is that the error
  # exit carries the metrics at all.
  assert "usage_metrics" in result


def test_run_codex_sdk_turn_superseded_generation_kill_is_interrupted(
  monkeypatch,
):
  """A newer turn taking over the chat is also Möbius ending this one.

  This leg needs no ActiveCodexTurn at all, so it is the one that reaches
  a teardown during startup — pinned here so a later narrowing of
  stop_requested() to the interrupt flag alone goes red.
  """
  superseded = {"value": False}

  result, _bc = _run_turn_whose_stream_dies(
    monkeypatch,
    _KilledTransportError("Codex process closed stdout. stderr_tail="),
    on_register=lambda _handle: superseded.update(value=True),
    should_abort=lambda: superseded["value"],
  )

  assert result["error"] is None
  assert result["terminal_status"] == "interrupted"


def test_run_codex_sdk_turn_error_notification_will_retry_continues(monkeypatch):
  class AgentMessageDeltaNotification:
    def __init__(self, delta: str):
      self.delta = delta

  sdk = _fake_sdk(object)
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="error",
      payload=sdk["ErrorNotification"](
        error=SimpleNamespace(message="transient"),
        thread_id="thread-1",
        turn_id="turn-1",
        will_retry=True,
      ),
    ),
    SimpleNamespace(
      method="agent_message/delta",
      payload=AgentMessageDeltaNotification("still running"),
    ),
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  turn_handle = _FakeTurnHandle(notifications)
  thread = _FakeThread("thread-1", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk["AsyncCodex"] = FakeAsyncCodex
  sdk["AgentMessageDeltaNotification"] = AgentMessageDeltaNotification
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
    )
  )

  assert result == {
    "session_id": "thread-1",
    "cost_usd": None,
    "error": None,
  }
  assert bc.events == [
    {"type": "session_init", "session_id": "thread-1"},
    {"type": "text", "content": "still running"},
  ]
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None


def test_run_codex_sdk_turn_publishes_thinking_for_reasoning_deltas(
  monkeypatch,
):
  """Codex reasoning deltas surface as `thinking` events, like Claude.

  Both visible reasoning stream names (item/reasoning/textDelta and
  item/reasoning/summaryTextDelta) translate to the same provider-agnostic
  `thinking` event; an empty delta publishes nothing. The runner also asks
  Codex for an auto reasoning summary so the richest public summary stream is
  opted in.
  """
  class ReasoningTextDeltaNotification:
    def __init__(self, delta: str, item_id=None, content_index=None):
      self.delta = delta
      self.item_id = item_id
      self.content_index = content_index

  class ReasoningSummaryTextDeltaNotification:
    def __init__(self, delta: str, item_id=None, summary_index=None):
      self.delta = delta
      self.item_id = item_id
      self.summary_index = summary_index

  class AgentMessageDeltaNotification:
    def __init__(self, delta: str):
      self.delta = delta

  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="item/reasoning/textDelta",
      payload=ReasoningTextDeltaNotification("plotting", "reason-1", 0),
    ),
    SimpleNamespace(
      method="item/reasoning/summaryTextDelta",
      payload=ReasoningSummaryTextDeltaNotification(" the route", "reason-1", 1),
    ),
    SimpleNamespace(
      method="item/reasoning/textDelta",
      payload=ReasoningTextDeltaNotification(""),
    ),
    SimpleNamespace(
      method="item/agentMessage/delta",
      payload=AgentMessageDeltaNotification("answer"),
    ),
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  thread = _FakeThread("thread-1", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk["AgentMessageDeltaNotification"] = AgentMessageDeltaNotification
  sdk["ReasoningTextDeltaNotification"] = ReasoningTextDeltaNotification
  sdk["ReasoningSummaryTextDeltaNotification"] = (
    ReasoningSummaryTextDeltaNotification
  )
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)
  monkeypatch.setattr(codex_sdk_runner.time, "time", lambda: 3.25)

  bc = _FakeBroadcast()
  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=bc,
      pending_questions={},
      db=None,
    )
  )

  assert result["error"] is None
  assert thread.turn_kwargs["summary"] == "auto"
  assert bc.events == [
    {"type": "session_init", "session_id": "thread-1"},
    {"type": "thinking", "content": "plotting", "ts": 3250,
     "segment_id": "codex:reason-1:content:0"},
    {"type": "thinking", "content": " the route", "ts": 3250,
     "segment_id": "codex:reason-1:summary:1"},
    {"type": "text", "content": "answer"},
  ]
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None


def test_run_codex_sdk_turn_persists_thread_id_before_terminal_result(
  monkeypatch,
):
  sdk = _fake_sdk(object)
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  turn_handle = _FakeTurnHandle([
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ])
  thread = _FakeThread("thread-early", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk["AsyncCodex"] = FakeAsyncCodex
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  db = SessionLocal()
  try:
    db.add(models.Chat(
      id="chat-early",
      title="t",
      messages=[],
      pending_messages=[],
      provider="codex",
      session_id=None,
    ))
    db.commit()

    result = asyncio.run(
      codex_sdk_runner.run_codex_sdk_turn(
        user_message="hello",
        session_id=None,
        base_env={},
        cwd="/tmp",
        chat_id="chat-early",
        bc=_FakeBroadcast(),
        pending_questions={},
        db=db,
      )
    )

    assert result["session_id"] == "thread-early"
    db.expire_all()
    chat = db.query(models.Chat).filter(models.Chat.id == "chat-early").first()
    assert chat.session_id == "thread-early"
  finally:
    db.close()


def test_run_codex_sdk_turn_error_notification_fatal_raises(monkeypatch):
  sdk = _fake_sdk(object)
  notifications = [
    SimpleNamespace(
      method="error",
      payload=sdk["ErrorNotification"](
        error=SimpleNamespace(message="fatal error"),
        thread_id="thread-1",
        turn_id="turn-1",
        will_retry=False,
      ),
    )
  ]
  turn_handle = _FakeTurnHandle(notifications)
  thread = _FakeThread("thread-1", turn_handle)

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk["AsyncCodex"] = FakeAsyncCodex
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=_FakeBroadcast(),
      pending_questions={},
      db=None,
    )
  )

  assert result["error"] is not None
  assert "fatal error" in result["error"]


def test_run_codex_sdk_turn_rejects_stream_exhaustion_without_terminal(
  monkeypatch,
):
  """A drained stream without TurnCompleted must not masquerade as success."""
  thread = _FakeThread("thread-1", _FakeTurnHandle([]))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )

  result = asyncio.run(
    codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-1",
      bc=_FakeBroadcast(),
      pending_questions={},
      db=None,
    )
  )

  assert result["error"] == (
    "Codex turn stream ended without a turn/completed notification."
  )
  assert result.get("terminal_status") is None


@pytest.mark.parametrize(
  ("status", "sdk_error", "interrupt_requested", "expected"),
  [
    (
      _FakeTurnStatus.failed,
      None,
      False,
      "Codex turn failed without an error message.",
    ),
    (
      _FakeTurnStatus.interrupted,
      None,
      False,
      "Codex turn was interrupted unexpectedly.",
    ),
    (_FakeTurnStatus.interrupted, None, True, None),
    (_FakeTurnStatus.completed, None, False, None),
  ],
)
def test_codex_terminal_status_is_validated(
  status,
  sdk_error,
  interrupt_requested,
  expected,
):
  sdk = _fake_sdk(object)
  turn = SimpleNamespace(
    status=status,
    error=sdk_error,
    items=[],
  )
  message_phases = (
    [_FakeMessagePhase.final_answer.value]
    if status == _FakeTurnStatus.completed
    else []
  )

  error, terminal_status, final_phase = (
    codex_sdk_runner._codex_terminal_error(
      turn,
      sdk,
      interrupt_requested=interrupt_requested,
      completed_message_phases=message_phases,
    )
  )

  assert error == expected
  assert terminal_status == status.value
  assert final_phase == (
    _FakeMessagePhase.final_answer.value
    if status == _FakeTurnStatus.completed
    else None
  )


def test_chatgpt_model_rejection_explains_connection_and_recovery():
  error = (
    "{\"detail\":\"The 'gpt-5.6-sol' model is not supported when using "
    "Codex with a ChatGPT account.\"}"
  )

  message = codex_sdk_runner._codex_user_error(error)

  assert message == (
    "gpt-5.6-sol isn’t available for this ChatGPT account. Codex is "
    "connected, but this account’s current plan or model rollout does not "
    "include it. Choose another Codex model from this chat’s Model menu, "
    "then try again."
  )


def test_unknown_codex_error_stays_verbatim():
  error = "upstream returned 503"
  assert codex_sdk_runner._codex_user_error(error) == error


def test_codex_completed_turn_without_agent_message_is_not_success():
  sdk = _fake_sdk(object)
  turn = SimpleNamespace(
    status=_FakeTurnStatus.completed,
    error=None,
    items=[],
  )

  error, terminal_status, final_phase = (
    codex_sdk_runner._codex_terminal_error(
      turn,
      sdk,
      interrupt_requested=False,
      completed_message_phases=[],
    )
  )

  assert error == "Codex turn completed without an agent final answer."
  assert terminal_status == "completed"
  assert final_phase is None


def test_codex_commentary_only_completion_is_not_a_final_answer():
  sdk = _fake_sdk(object)
  turn = SimpleNamespace(
    status=_FakeTurnStatus.completed,
    error=None,
    items=[],
  )

  error, terminal_status, final_phase = (
    codex_sdk_runner._codex_terminal_error(
      turn,
      sdk,
      interrupt_requested=False,
      completed_message_phases=[_FakeMessagePhase.commentary.value],
    )
  )

  assert error == (
    "Codex turn completed after commentary without a final answer."
  )
  assert terminal_status == "completed"
  assert final_phase == "commentary"


def test_codex_completed_turn_items_recover_a_dropped_message_notification():
  class AgentMessage:
    def __init__(self, phase):
      self.phase = phase

  sdk = _fake_sdk(object)
  sdk["AgentMessageThreadItem"] = AgentMessage
  turn = SimpleNamespace(
    status=_FakeTurnStatus.completed,
    error=None,
    items=[AgentMessage(_FakeMessagePhase.commentary)],
  )

  error, terminal_status, final_phase = (
    codex_sdk_runner._codex_terminal_error(
      turn,
      sdk,
      interrupt_requested=False,
      completed_message_phases=[],
    )
  )

  assert error == (
    "Codex turn completed after commentary without a final answer."
  )
  assert terminal_status == "completed"
  assert final_phase == "commentary"


def test_codex_completed_turn_items_override_notification_order():
  class AgentMessage:
    def __init__(self, phase):
      self.phase = phase

  sdk = _fake_sdk(object)
  sdk["AgentMessageThreadItem"] = AgentMessage
  turn = SimpleNamespace(
    status=_FakeTurnStatus.completed,
    error=None,
    items=[
      AgentMessage(_FakeMessagePhase.final_answer),
      AgentMessage(_FakeMessagePhase.commentary),
    ],
  )

  error, terminal_status, final_phase = (
    codex_sdk_runner._codex_terminal_error(
      turn,
      sdk,
      interrupt_requested=False,
      # A duplicated earlier notification must not rescue a non-final tail.
      completed_message_phases=[_FakeMessagePhase.final_answer.value],
    )
  )

  assert error == (
    "Codex turn completed after commentary without a final answer."
  )
  assert terminal_status == "completed"
  assert final_phase == "commentary"


def test_codex_earlier_final_followed_by_commentary_is_not_complete():
  sdk = _fake_sdk(object)
  turn = SimpleNamespace(
    status=_FakeTurnStatus.completed,
    error=None,
    items=[],
  )

  error, _, final_phase = codex_sdk_runner._codex_terminal_error(
    turn,
    sdk,
    interrupt_requested=False,
    completed_message_phases=[
      _FakeMessagePhase.final_answer.value,
      _FakeMessagePhase.commentary.value,
    ],
  )

  assert error == (
    "Codex turn completed after commentary without a final answer."
  )
  assert final_phase == "commentary"


@pytest.mark.parametrize("phase", [_FakeMessagePhase.final_answer.value, None])
def test_codex_explicit_and_legacy_final_messages_are_accepted(phase):
  sdk = _fake_sdk(object)
  turn = SimpleNamespace(
    status=_FakeTurnStatus.completed,
    error=None,
    items=[],
  )

  error, terminal_status, final_phase = (
    codex_sdk_runner._codex_terminal_error(
      turn,
      sdk,
      interrupt_requested=False,
      completed_message_phases=[
        _FakeMessagePhase.commentary.value,
        phase,
      ],
    )
  )

  assert error is None
  assert terminal_status == "completed"
  assert final_phase == phase


# ---------------------------------------------------------------------------
# skill_loaded observability — Codex mirror. Codex has no Read tool and
# no can_use_tool hook; skill loads surface as shell reads of
# /data/shared/skills/<name>.md in the command-execution item stream.
# ---------------------------------------------------------------------------

def test_skill_names_in_command_extracts_and_dedupes():
  cmd = (
    "cat /data/shared/skills/memory.md && "
    "sed -n 1,40p /data/shared/skills/building-apps.md; "
    "cat /data/shared/skills/memory.md"
  )
  names = codex_sdk_runner._skill_names_in_command(cmd, "/data")
  assert names == ["memory", "building-apps"]


def test_skill_names_in_command_counts_entry_documents_not_resources():
  cmd = (
    "cat /data/.codex/skills/theming/SKILL.md && "
    "node /data/.codex/skills/impeccable/scripts/context.mjs "
    "--target frontend/src/components/ChatView/ChatView.jsx && "
    "cat /data/.codex/skills/impeccable/reference/optimize.md && "
    "cat .codex/skills/impeccable/reference/craft-floor.md"
  )
  names = codex_sdk_runner._skill_names_in_command(cmd, "/data")
  assert names == ["theming"]


def test_skill_names_in_command_ignores_other_paths():
  fn = codex_sdk_runner._skill_names_in_command
  assert fn("cat /data/shared/memory/index.md", "/data") == []
  assert fn("cat /elsewhere/shared/skills/memory.md", "/data") == []
  assert fn("cat /elsewhere/.codex/skills/theming/SKILL.md", "/data") == []
  assert fn("cat /data/shared/skills/notes.txt", "/data") == []
  assert fn("cat /data/.codex/skills/.mobius-managed.json", "/data") == []
  assert fn("", "/data") == []


def test_observe_skill_reads_publishes_targeted_receipt_and_activity(monkeypatch):
  import os

  from app import activity
  from app.config import get_settings

  logged: list[tuple] = []
  monkeypatch.setattr(
    activity, "log_skill_load",
    lambda chat_id, skill, ts=None: logged.append((chat_id, skill)),
  )

  class _Cmd:
    def __init__(self, command):
      self.command = command
      self.id = "cmd-skill-1"

  sdk = {"CommandExecutionThreadItem": _Cmd}
  bc = _FakeBroadcast()
  skills = os.path.join(get_settings().data_dir, "shared", "skills")
  item = _Cmd(f"cat {skills}/cron.md")
  codex_sdk_runner._observe_skill_reads(item, sdk, bc=bc, chat_id="cx-1")
  assert bc.events == [{
    "type": "skill_loaded", "skill": "cron",
    "tool_use_id": "cmd-skill-1",
  }]
  assert logged == [("cx-1", "cron")]

  # Non-command items and non-skill commands emit nothing.
  class _Other:
    command = f"cat {skills}/cron.md"

  codex_sdk_runner._observe_skill_reads(
    _Other(), sdk, bc=bc, chat_id="cx-1",
  )
  codex_sdk_runner._observe_skill_reads(
    _Cmd("ls /data"), sdk, bc=bc, chat_id="cx-1",
  )
  assert len(bc.events) == 1


def test_observe_skill_reads_never_raises(monkeypatch):
  """Fire-and-forget: a broken broadcast must not break the loop."""
  import os

  from app.config import get_settings

  class _Cmd:
    def __init__(self, command):
      self.command = command

  class _ExplodingBus:
    def publish(self, event):
      raise RuntimeError("wire down")

  sdk = {"CommandExecutionThreadItem": _Cmd}
  skills = os.path.join(get_settings().data_dir, "shared", "skills")
  codex_sdk_runner._observe_skill_reads(
    _Cmd(f"cat {skills}/memory.md"), sdk, bc=_ExplodingBus(),
    chat_id="cx-2",
  )


class _FakeCollabItem:
  """Stand-in CollabAgentToolCallThreadItem exposing only what the collab
  builders + child-link recorder read: id, tool.value, prompt, status.value,
  agents_states[*].message, and receiver_thread_ids. Instances are registered
  as the sdk["CollabAgentToolCallThreadItem"] class so isinstance dispatch fires."""

  def __init__(
    self,
    *,
    item_id="collab-1",
    tool="spawnAgent",
    prompt=None,
    status="completed",
    messages=None,
    receivers=None,
  ):
    self.id = item_id
    self.tool = SimpleNamespace(value=tool)
    self.prompt = prompt
    self.status = SimpleNamespace(value=status)
    self.agents_states = {
      f"child-{i}": SimpleNamespace(
        message=m, status=SimpleNamespace(value="completed"),
      )
      for i, m in enumerate(messages or [])
    }
    self.receiver_thread_ids = receivers or []


def test_tool_start_event_collab_wait_is_ordinary_background_activity():
  # VERIFIED live reality on codex 0.144.5: a delegation turn streams the collab
  # tool ONLY as the `wait` op, which carries no helper identity. The invariant
  # is that this remains ordinary Task activity and never opens task lifecycle.
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(item_id="c-0", tool="wait", prompt=None)
  event = codex_sdk_runner._tool_start_event(item, sdk)
  assert event == {
    "type": "tool_start",
    "tool": "Task",
    "input": "Working in the background",
  }
  assert not event["type"].startswith("task_")


def test_tool_start_event_collab_prompt_remains_ordinary_activity():
  # Prompt-present path (a future SDK that surfaces the spawn op with a prompt,
  # or this test fake): keep the "<op>: <prompt>" form so the chip names the
  # delegated work. Not what fires today — the live wait op has no prompt.
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(
    item_id="c-1", tool="spawnAgent", prompt="review the diff for races",
  )
  event = codex_sdk_runner._tool_start_event(item, sdk)
  assert event == {
    "type": "tool_start",
    "tool": "Task",
    "input": "spawnAgent: review the diff for races",
  }


def test_tool_start_event_collab_description_truncates_long_prompt():
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(tool="spawnAgent", prompt="x" * 500)
  event = codex_sdk_runner._tool_start_event(item, sdk)
  assert len(event["input"]) == 120
  assert event["input"].startswith("spawnAgent: xxx")


def test_tool_completed_events_collab_summary_is_ordinary_tool_output():
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(
    item_id="c-2", status="completed",
    messages=["found a bug", "wrote a test"],
  )
  events = codex_sdk_runner._tool_completed_events(item, sdk)
  assert events == [
    {"type": "tool_output", "content": "found a bug; wrote a test"},
    {"type": "tool_end"},
  ]
  assert all(not event["type"].startswith("task_") for event in events)


def test_tool_completed_events_collab_wait_is_ordinary_tool_end():
  # The invariant is that an empty runtime wait emits only the ordinary tool_end
  # and never manufactures a task_done with a per-helper status.
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(item_id="c-3", status="failed", messages=[])
  events = codex_sdk_runner._tool_completed_events(item, sdk)
  assert events == [{"type": "tool_end"}]
  assert all(not event["type"].startswith("task_") for event in events)


def test_collab_branch_skipped_when_sdk_lacks_type():
  # An SDK/build without the collab type registers None; the builders must fall
  # through to their tool branches, never raise on isinstance(item, None).
  sdk = {
    "CollabAgentToolCallThreadItem": None,
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "WebSearchThreadItem": type("WebSearchThreadItem", (), {}),
    "AgentMessageThreadItem": type("AgentMessageThreadItem", (), {}),
  }
  assert codex_sdk_runner._tool_start_event(_FakeCollabItem(), sdk) is None
  assert codex_sdk_runner._tool_completed_events(_FakeCollabItem(), sdk) == []


def test_record_collab_child_links_attributes_spawned_children(db):
  # Locks the DEFENSIVE path: on codex 0.144.5 receiver_thread_ids is always
  # empty so this never fires in production, but a future SDK that populates it
  # on a spawn op must still attribute each child thread to this chat.
  db.add(models.Chat(
    id="collab-chat", title="t", messages=[], pending_messages=[],
    provider="codex", session_id=None,
  ))
  db.commit()

  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(tool="spawnAgent", receivers=["child-A", "child-B"])
  asyncio.run(
    codex_sdk_runner._record_collab_child_links(
      item, sdk, chat_id="collab-chat",
    )
  )

  db.expire_all()
  a = db.get(models.ChatSessionLink, ("codex", "child-A"))
  b = db.get(models.ChatSessionLink, ("codex", "child-B"))
  assert a is not None and a.chat_id == "collab-chat"
  assert b is not None and b.chat_id == "collab-chat"


def test_record_collab_child_links_ignores_non_spawn_ops(db):
  # sendInput / resumeAgent reference a child already recorded at its spawn;
  # they must not mint a fresh first-sight row here (gate is spawn-only).
  db.add(models.Chat(
    id="collab-chat-2", title="t", messages=[], pending_messages=[],
    provider="codex", session_id=None,
  ))
  db.commit()

  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(tool="sendInput", receivers=["child-A"])
  asyncio.run(
    codex_sdk_runner._record_collab_child_links(
      item, sdk, chat_id="collab-chat-2",
    )
  )

  assert db.get(models.ChatSessionLink, ("codex", "child-A")) is None


def test_persist_session_id_records_codex_link(db):
  # Item 1: the persistence funnel (run on both thread_start and thread_resume)
  # records the append-only codex session->chat link alongside the actor's
  # Chat.session_id write.
  db.add(models.Chat(
    id="codex-persist", title="t", messages=[], pending_messages=[],
    provider="codex", session_id=None,
  ))
  db.commit()

  asyncio.run(
    codex_sdk_runner._persist_session_id(db, "codex-persist", "thread-xyz")
  )

  db.expire_all()
  link = db.get(models.ChatSessionLink, ("codex", "thread-xyz"))
  assert link is not None
  assert link.chat_id == "codex-persist"
  assert link.first_seen_at == link.last_seen_at


def test_persist_session_id_skips_synthetic_turn_without_db(monkeypatch, caplog):
  # Reflection invokes the shared Codex runner with a synthetic chat id and no
  # database session. It has no Chat row to update and must not initialize the
  # chat writer/session-link stack (which also requires the web app's secrets).
  from app import chat_writer, session_links

  def fail_writer_lookup():
    raise AssertionError("synthetic turn must not initialize the chat writer")

  async def fail_link_write(*_args, **_kwargs):
    raise AssertionError("synthetic turn must not record a chat session link")

  monkeypatch.setattr(chat_writer, "get_writer", fail_writer_lookup)
  monkeypatch.setattr(session_links, "record_session_link_async", fail_link_write)

  asyncio.run(
    codex_sdk_runner._persist_session_id(
      None, "reflection-nightly", "thread-synthetic",
    )
  )

  assert "Codex session id persistence failed" not in caplog.text


def test_codex_config_overrides_default_pins_agents_namespace(monkeypatch):
  """Multi-agent is on by default AND pins the 'agents' tool namespace so the
  reserved 'collaboration' default (Codex #31864) can never brick a turn."""
  from app import codex_sdk_runner as runner
  monkeypatch.delenv("MOEBIUS_CODEX_MULTI_AGENT", raising=False)
  ov = runner._codex_config_overrides()
  assert "features.multi_agent_v2.enabled=true" in ov
  assert "features.multi_agent_v2.tool_namespace=agents" in ov


def test_codex_config_overrides_kill_switch(monkeypatch):
  """MOEBIUS_CODEX_MULTI_AGENT=off disables multi-agent at runtime (no rebuild),
  leaving only request_user_input — the reversible rollback."""
  from app import codex_sdk_runner as runner
  monkeypatch.setenv("MOEBIUS_CODEX_MULTI_AGENT", "off")
  ov = runner._codex_config_overrides()
  assert ov == [
    'instructions=""',
    'developer_instructions=""',
    "project_doc_max_bytes=0",
    "features.default_mode_request_user_input=true",
    "features.goals=true",
  ]
  assert not any("multi_agent_v2" in o for o in ov)


def test_codex_config_overrides_enable_native_goal_runtime(monkeypatch):
  monkeypatch.delenv("MOEBIUS_CODEX_MULTI_AGENT", raising=False)
  assert "features.goals=true" in codex_sdk_runner._codex_config_overrides()


def test_ordinary_codex_turns_do_not_expose_provider_private_goal_tools():
  assert codex_sdk_runner._needs_native_goal_control(
    goal_mode=False,
    goal_objective=None,
    goal_clear=False,
    fallback_goal_objective=None,
  ) is False
  assert "features.goals=true" not in codex_sdk_runner._codex_config_overrides(
    allow_goals=False,
  )
  assert codex_sdk_runner._needs_native_goal_control(
    goal_mode=True,
    goal_objective="Ship and verify",
    goal_clear=False,
    fallback_goal_objective=None,
  ) is True


def test_codex_app_server_launch_args_preserve_overrides_under_setsid(
  monkeypatch,
):
  paths = {
    "setsid": "/usr/bin/setsid",
  }
  monkeypatch.setattr(
    codex_sdk_runner.shutil,
    "which",
    lambda name: paths.get(name),
  )

  args = codex_sdk_runner._codex_app_server_launch_args(
    "/usr/local/bin/codex",
    ["feature.one=true", "feature.two=false"],
  )

  assert args == [
    "/usr/bin/setsid",
    "/usr/local/bin/codex",
    "--config",
    "feature.one=true",
    "--config",
    "feature.two=false",
    "app-server",
    "--listen",
    "stdio://",
  ]


def test_codex_process_group_id_refuses_shared_uvicorn_group(monkeypatch):
  codex = SimpleNamespace(
    _client=SimpleNamespace(
      _sync=SimpleNamespace(_proc=SimpleNamespace(pid=4321)),
    ),
  )
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4000)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 4000)

  assert codex_sdk_runner._codex_process_group_id(codex) is None


def test_codex_call_executor_progresses_with_default_pool_saturated():
  async def scenario():
    loop = asyncio.get_running_loop()
    release = threading.Event()
    occupied = threading.Event()
    saturated = ThreadPoolExecutor(max_workers=1)
    replacement = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(saturated)

    def occupy_default_worker():
      occupied.set()
      release.wait()

    blocker = loop.run_in_executor(None, occupy_default_worker)
    while not occupied.is_set():
      await asyncio.sleep(0)

    client = SimpleNamespace(_call_sync=lambda *_args, **_kwargs: None)
    codex = SimpleNamespace(_client=client)
    owner = codex_sdk_runner._install_codex_call_executor(
      codex, "chat-owned-executor",
    )
    try:
      assert owner is not None
      worker_name = await asyncio.wait_for(
        client._call_sync(lambda: threading.current_thread().name),
        timeout=1,
      )
      assert worker_name.startswith("mobius-codex-chat-own")
    finally:
      owner.close()
      release.set()
      await blocker
      loop.set_default_executor(replacement)
      saturated.shutdown(wait=True)

  asyncio.run(scenario())


def test_process_group_capture_poll_backs_off_after_startup_window():
  spin = codex_sdk_runner._PROCESS_GROUP_CAPTURE_SPIN_SECONDS
  assert codex_sdk_runner._process_group_capture_delay(spin / 2) == 0
  assert codex_sdk_runner._process_group_capture_delay(spin) == (
    codex_sdk_runner._PROCESS_GROUP_CAPTURE_POLL_SECONDS
  )
  assert codex_sdk_runner._PROCESS_GROUP_CAPTURE_POLL_SECONDS > 0


def test_terminate_codex_process_group_has_sigkill_backstop(monkeypatch):
  calls = []
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    codex_sdk_runner.os,
    "killpg",
    lambda pgid, sig: calls.append((pgid, sig)),
  )

  assert codex_sdk_runner._terminate_codex_process_group(
    4321, grace_seconds=0,
  ) is True
  assert calls == [
    (4321, signal.SIGTERM),
    (4321, signal.SIGKILL),
  ]


def test_run_codex_sdk_turn_reaps_isolated_descendants(monkeypatch):
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    )
  ]
  thread = _FakeThread("thread-1", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(
          _proc=SimpleNamespace(pid=4321),
          _approval_handler=None,
        ),
      )

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": "/usr/bin/setsid",
  }.get(name))
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4321)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)
  reaped = []
  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    lambda pgid: reaped.append(pgid) or True,
  )
  monkeypatch.setattr(
    codex_sdk_runner,
    "_persist_session_id",
    lambda *_args, **_kwargs: asyncio.sleep(0),
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-process-group",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
  ))

  assert result["error"] is None
  assert reaped == [4321]


def test_run_codex_sdk_turn_reaps_group_when_initialization_fails(monkeypatch):
  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(_proc=None, _approval_handler=None),
      )

    async def __aenter__(self):
      # Model the pinned SDK's lifecycle: start() publishes _proc, initialize()
      # yields/fails, and close() clears _proc before __aenter__ re-raises.
      self._client._sync._proc = SimpleNamespace(pid=4321)
      await asyncio.sleep(0)
      self._client._sync._proc = None
      raise RuntimeError("initialize failed")

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": "/usr/bin/setsid",
  }.get(name))
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4321)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)
  reaped = []
  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    lambda pgid: reaped.append(pgid) or True,
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-init-failure",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
  ))

  assert "initialize failed" in result["error"]
  assert reaped == [4321]


def test_run_codex_sdk_turn_cancel_after_entry_still_reaps_group(monkeypatch):
  owner_task = None

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(
          _proc=SimpleNamespace(pid=4321),
          _approval_handler=None,
        ),
      )

    async def __aenter__(self):
      # Deliver cancellation at the first await after entry. The runner must
      # synchronously retain the PGID and must not cancel its capture-task
      # ownership while unwinding.
      asyncio.get_running_loop().call_soon(owner_task.cancel)
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      await asyncio.sleep(0)
      raise AssertionError("cancellation should land before thread start")

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": "/usr/bin/setsid",
  }.get(name))
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4321)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)
  reaped = []
  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    lambda pgid: reaped.append(pgid) or True,
  )

  async def scenario():
    nonlocal owner_task
    owner_task = asyncio.current_task()
    with pytest.raises(asyncio.CancelledError):
      await codex_sdk_runner.run_codex_sdk_turn(
        user_message="hello",
        session_id=None,
        base_env={},
        cwd="/tmp",
        chat_id="chat-cancel-after-entry",
        bc=_FakeBroadcast(),
        pending_questions={},
        db=None,
      )

  asyncio.run(scenario())

  assert reaped == [4321]


def test_run_codex_sdk_turn_cancel_during_threaded_start_waits_then_reaps(
  monkeypatch,
):
  """Cancellation cannot outrun the pinned SDK's asyncio.to_thread(start)."""
  worker_started = threading.Event()
  release_worker = threading.Event()

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(_proc=None, _approval_handler=None),
      )

    async def __aenter__(self):
      def threaded_start():
        worker_started.set()
        assert release_worker.wait(timeout=2)
        self._client._sync._proc = SimpleNamespace(pid=4321)

      # Match the pinned SDK: cancelling this await does not stop the worker.
      await asyncio.to_thread(threaded_start)
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      self._client._sync._proc = None
      return None

    async def thread_start(self, *_args, **_kwargs):
      raise AssertionError("deferred cancellation must land before thread start")

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": "/usr/bin/setsid",
  }.get(name))
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4321)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)
  reaped = []
  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    lambda pgid: reaped.append(pgid) or True,
  )

  async def scenario():
    task = asyncio.create_task(codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id="chat-cancel-during-start",
      bc=_FakeBroadcast(),
      pending_questions={},
      db=None,
    ))
    while not worker_started.is_set():
      await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
      await task

  asyncio.run(scenario())

  assert reaped == [4321]


@pytest.mark.parametrize("cancel_count", [1, 2, 5])
def test_run_codex_sdk_turn_start_failure_preserves_deferred_cancellation(
  monkeypatch, cancel_count,
):
  """Caller cancellation wins after owned startup later fails internally."""
  startup_ready = None
  release_startup = None

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(_proc=None, _approval_handler=None),
      )

    async def __aenter__(self):
      self._client._sync._proc = SimpleNamespace(pid=4321)
      startup_ready.set()
      await release_startup.wait()
      # Match an initialize failure after the SDK has closed/forgotten its
      # direct process. The concurrent capture task is now the only PGID owner.
      self._client._sync._proc = None
      raise RuntimeError("initialize failed after caller cancellation")

    async def __aexit__(self, _exc_type, _exc, _tb):
      raise AssertionError("a failed enter must not invoke __aexit__")

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": "/usr/bin/setsid",
  }.get(name))
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4321)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)
  reaped = []
  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    lambda pgid: reaped.append(pgid) or True,
  )

  async def scenario():
    nonlocal startup_ready, release_startup
    startup_ready = asyncio.Event()
    release_startup = asyncio.Event()
    task = asyncio.create_task(codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id=f"chat-cancel-failed-start-{cancel_count}",
      bc=_FakeBroadcast(),
      pending_questions={},
      db=None,
    ))
    await startup_ready.wait()
    # Let the concurrent PGID watcher observe the published process before the
    # fake SDK forgets it on initialization failure.
    await asyncio.sleep(0)
    for _ in range(cancel_count):
      task.cancel()
      await asyncio.sleep(0)
      assert not task.done()
    release_startup.set()
    with pytest.raises(asyncio.CancelledError):
      await task

  asyncio.run(scenario())

  assert reaped == [4321]


@pytest.mark.parametrize("cancel_count", [1, 2, 5])
def test_run_codex_sdk_turn_waits_for_sdk_exit_before_reap_and_return(
  monkeypatch, cancel_count,
):
  """Repeated cancellation cannot outrun the SDK's threaded close/wait."""
  body_ready = None
  close_started = threading.Event()
  release_close = threading.Event()
  close_finished = threading.Event()
  order = []

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(
          _proc=SimpleNamespace(pid=4321),
          _approval_handler=None,
        ),
      )

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      def close_worker():
        close_started.set()
        assert release_close.wait(timeout=5)
        self._client._sync._proc = None
        order.append("close")
        close_finished.set()

      # Match the pinned SDK close path. Cancelling this await must never
      # abandon either a queued or already-running direct-child wait.
      await asyncio.to_thread(close_worker)
      return None

    async def thread_start(self, *_args, **_kwargs):
      body_ready.set()
      await asyncio.Future()

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": "/usr/bin/setsid",
  }.get(name))
  monkeypatch.setattr(codex_sdk_runner.os, "getpgid", lambda _pid: 4321)
  monkeypatch.setattr(codex_sdk_runner.os, "getpgrp", lambda: 9999)

  def reap(pgid):
    assert pgid == 4321
    assert close_finished.is_set()
    order.append("reap")
    return True

  monkeypatch.setattr(
    codex_sdk_runner,
    "_terminate_codex_process_group",
    reap,
  )

  async def scenario():
    nonlocal body_ready
    body_ready = asyncio.Event()
    task = asyncio.create_task(codex_sdk_runner.run_codex_sdk_turn(
      user_message="hello",
      session_id=None,
      base_env={},
      cwd="/tmp",
      chat_id=f"chat-cancel-sdk-exit-{cancel_count}",
      bc=_FakeBroadcast(),
      pending_questions={},
      db=None,
    ))
    await body_ready.wait()
    task.cancel()
    assert await asyncio.to_thread(close_started.wait, 2)
    for _ in range(cancel_count - 1):
      task.cancel()
      await asyncio.sleep(0)
    assert not task.done()
    assert not close_finished.is_set()
    assert order == []
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
      await task

  asyncio.run(scenario())

  assert close_finished.is_set()
  assert order == ["close", "reap"]


def test_run_codex_sdk_turn_fallback_does_not_start_capture_poller(
  monkeypatch,
):
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    )
  ]
  thread = _FakeThread("thread-1", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  monkeypatch.setattr(
    codex_sdk_runner,
    "_sdk_imports",
    lambda: _fake_sdk(FakeAsyncCodex),
  )
  monkeypatch.setattr(codex_sdk_runner.shutil, "which", lambda name: {
    "codex": "/usr/local/bin/codex",
    "setsid": None,
  }.get(name))

  async def forbidden_poller(*_args, **_kwargs):
    raise AssertionError("fallback launch must not start PGID polling")

  monkeypatch.setattr(
    codex_sdk_runner,
    "_capture_codex_process_group_during_start",
    forbidden_poller,
  )
  monkeypatch.setattr(
    codex_sdk_runner,
    "_persist_session_id",
    lambda *_args, **_kwargs: asyncio.sleep(0),
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="hello",
    session_id=None,
    base_env={},
    cwd="/tmp",
    chat_id="chat-fallback-no-poll",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
  ))

  assert result["error"] is None


@pytest.mark.parametrize("session_id", [None, "resumed-thread"])
def test_run_codex_sdk_turn_controls_prompt_layers(monkeypatch, session_id):
  # Fresh and resumed threads receive the same immutable constitution as their
  # base prompt, with provider-authored developer/personality layers disabled.
  # Re-supplying the snapshot on resume also keeps a compacted thread anchored
  # without trusting its original instructions to survive indefinitely.
  completed_turn = SimpleNamespace(id="turn-r", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  thread = _FakeThread("resumed-thread", _FakeTurnHandle(notifications))
  captured: dict = {}
  connector_plan = connector_core.ConnectorTurnPlan(
    codex_config={
      "mcp_servers": {
        "search": {
          "url": "https://mcp.example/mcp",
          "bearer_token_env_var": "MOBIUS_CONNECTOR_3_SEARCH",
        },
      },
    },
    codex_env={"MOBIUS_CONNECTOR_3_SEARCH": "private-key"},
  )

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      captured["process_config"] = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_a):
      return None

    async def thread_resume(self, session_id, **kwargs):
      captured["session_id"] = session_id
      captured["thread_options"] = kwargs
      return thread

    async def thread_start(self, **kwargs):
      captured["session_id"] = None
      captured["thread_options"] = kwargs
      return thread

  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )

  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="continue",
    session_id=session_id,
    base_env={},
    cwd="/tmp",
    chat_id="chat-resume",
    bc=_FakeBroadcast(),
    pending_questions={},
    db=None,
    system_prompt="FROZEN CONSTITUTION SNAPSHOT",
    connector_plan=connector_plan,
  ))

  assert captured["session_id"] == session_id
  assert captured["thread_options"]["base_instructions"] == (
    "FROZEN CONSTITUTION SNAPSHOT"
  )
  assert captured["thread_options"]["developer_instructions"] == ""
  assert captured["thread_options"]["personality"] == "none"
  thread_mcp = captured["thread_options"]["config"]["mcp_servers"]
  assert thread_mcp["search"] == (
    connector_plan.codex_config["mcp_servers"]["search"]
  )
  assert thread_mcp["mobius_control"]["args"][0].endswith(
    "/scripts/mobius_control_mcp.py"
  )
  process_env = captured["process_config"].kwargs["env"]
  assert process_env["MOBIUS_CONNECTOR_3_SEARCH"] == "private-key"
  assert "private-key" not in repr(captured["thread_options"]["config"])
  assert result["session_id"] == "resumed-thread"
  assert result["error"] is None
# --- Structured rate-limit reset extraction --------------------------------
# Mirrors the installed SDK shapes:
# - RateLimitSnapshot: openai_codex/generated/v2_all.py:6724
#   (primary/secondary: RateLimitWindow|None, rate_limit_reached_type: Enum|None)
# - RateLimitWindow: v2_all.py:2928 (resets_at: int|None, used_percent: int)
# The runner reads these off AccountRateLimitsUpdatedNotification so a Codex
# quota kill parks on the provider's real reset time instead of the 30-minute
# text-parse fallback (chat._limit_park_fields), and is detected structurally.


def _window(resets_at, used_percent):
  return SimpleNamespace(resets_at=resets_at, used_percent=used_percent)


def test_extract_rate_limit_reset_picks_most_constrained_window():
  # The binding limit is the fullest window; its reset is the one worth waiting
  # for even though the other window resets sooner.
  snapshot = SimpleNamespace(
    primary=_window(1_800_000_000, 40),
    secondary=_window(1_800_009_999, 97),
    rate_limit_reached_type="rate_limit_reached",
  )
  reset, reached = codex_sdk_runner._extract_rate_limit_reset(snapshot)
  assert reset == 1_800_009_999
  assert reached is True


def test_extract_rate_limit_reset_not_reached_when_type_none():
  # rate_limit_reached_type has no "ok" member, so None reliably means the cap
  # was not hit — even while windows still report their reset schedule.
  snapshot = SimpleNamespace(
    primary=_window(1_800_000_000, 30),
    secondary=None,
    rate_limit_reached_type=None,
  )
  reset, reached = codex_sdk_runner._extract_rate_limit_reset(snapshot)
  assert reset == 1_800_000_000
  assert reached is False


def test_extract_rate_limit_reset_skips_windows_without_reset():
  # A window missing resets_at must never be chosen even if it is the fullest.
  snapshot = SimpleNamespace(
    primary=_window(None, 99),
    secondary=_window(1_800_005_000, 55),
    rate_limit_reached_type="workspace_owner_usage_limit_reached",
  )
  reset, reached = codex_sdk_runner._extract_rate_limit_reset(snapshot)
  assert reset == 1_800_005_000
  assert reached is True


def test_extract_rate_limit_reset_handles_empty_and_none():
  assert codex_sdk_runner._extract_rate_limit_reset(None) == (None, False)
  empty = SimpleNamespace(
    primary=None, secondary=None, rate_limit_reached_type=None
  )
  assert codex_sdk_runner._extract_rate_limit_reset(empty) == (None, False)


def test_run_codex_sdk_turn_reseeds_lost_session_and_records_event(monkeypatch):
  # A lost Codex session — thread_resume returns a DIFFERENT thread id than
  # requested — must reseed like the Claude runner: no error, continue on the
  # fresh thread with the chat's prior history prepended, and record a
  # codex_session_reseed activity event so the loss is not masked (Reflection
  # reads /data/logs/activity.jsonl). A genuine resume error still raises; only
  # this "different thread returned" (lost-session) case reseeds.
  completed_turn = SimpleNamespace(id="turn-x", usage=None, error=None)
  notifications = [
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    ),
  ]
  fresh_thread = _FakeThread("fresh-thread-999", _FakeTurnHandle(notifications))

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_a):
      return None

    async def thread_resume(self, session_id, **kwargs):
      return fresh_thread

    async def thread_start(self, *_a, **_k):
      raise AssertionError("reseed must ride the fresh thread, not thread_start")

  events: list = []
  monkeypatch.setattr(
    codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex),
  )
  from app import activity
  monkeypatch.setattr(
    activity, "log_event", lambda ev, **f: events.append((ev, f)) or True,
  )

  bc = _FakeBroadcast()
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="please continue",
    session_id="lost-thread-1",
    base_env={},
    cwd="/tmp",
    chat_id="chat-reseed",
    bc=bc,
    pending_questions={},
    db=None,
    resumed_context="<resumed_context>earlier chat</resumed_context>",
  ))

  assert result["error"] is None
  assert result["session_id"] == "fresh-thread-999"
  assert {"type": "session_init", "session_id": "fresh-thread-999"} in bc.events
  assert not any(e.get("type") == "error" for e in bc.events)
  assert "<resumed_context>earlier chat</resumed_context>" in fresh_thread.turn_args[0]
  assert any(
    ev == "codex_session_reseed"
    and f.get("chat_id") == "chat-reseed"
    and f.get("requested_session") == "lost-thread-1"
    and f.get("replacement_session") == "fresh-thread-999"
    and f.get("reseeded") is True
    for ev, f in events
  )
