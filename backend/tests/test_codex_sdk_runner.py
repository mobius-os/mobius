import asyncio
import signal
from types import SimpleNamespace

import pytest

from app import codex_sdk_runner, models
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry


# Mirrors the installed SDK:
# - ErrorNotification: /usr/local/lib/python3.12/site-packages/openai_codex/generated/v2_all.py:6958
# - CodexRpcError: /usr/local/lib/python3.12/site-packages/openai_codex/errors.py:24
# - InvalidParamsError: /usr/local/lib/python3.12/site-packages/openai_codex/errors.py:40


class _FakeBroadcast:
  def __init__(self):
    self.events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)


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

  class _FakeCodexRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data=None):
      super().__init__(f"JSON-RPC error {code}: {message}")
      self.code = code
      self.message = message
      self.data = data

  return {
    "AgentMessageDeltaNotification": _Dummy,
    "ApprovalMode": _FakeApprovalMode,
    "AsyncCodex": async_codex_cls,
    "CodexConfig": _FakeCodexConfig,
    "CodexRpcError": _FakeCodexRpcError,
    "CommandExecutionOutputDeltaNotification": _Dummy,
    "CommandExecutionThreadItem": _Dummy,
    "ContextCompactedNotification": _Dummy,
    "DynamicToolCallThreadItem": _Dummy,
    "ErrorNotification": _FakeErrorNotification,
    "FileChangePatchUpdatedNotification": _Dummy,
    "FileChangeThreadItem": _Dummy,
    "InvalidParamsError": _FakeInvalidParamsError,
    "ReasoningEffort": _FakeReasoningEffort(),
    "ReasoningSummary": _FakeReasoningSummary(),
    "Sandbox": _FakeSandbox,
    "ItemCompletedNotification": _Dummy,
    "ItemGuardianApprovalReviewCompletedNotification": _Dummy,
    "ItemGuardianApprovalReviewStartedNotification": _Dummy,
    "ItemStartedNotification": _Dummy,
    "McpToolCallThreadItem": _Dummy,
    "ReasoningSummaryTextDeltaNotification": _Dummy,
    "ReasoningTextDeltaNotification": _Dummy,
    "ThreadTokenUsageUpdatedNotification": _Dummy,
    "TurnCompletedNotification": _FakeTurnCompletedNotification,
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
    def __init__(self, output: str):
      self.aggregated_output = output

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
    {"type": "tool_output", "content": "hello"},
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


def test_steer_into_active_turn_cleans_dead_handle(monkeypatch):
  sdk = _fake_sdk(async_codex_cls=object)

  async def _scenario() -> bool:
    active_turn = codex_sdk_runner.ActiveCodexTurn(
      object(),
      _FakeTurnHandle(
        steer_exc=sdk["InvalidParamsError"](-32602, "turn is not running"),
      ),
      chat_id="chat-1",
    )
    registry.register(active_turn)
    return await codex_sdk_runner.steer_into_active_turn(
      "chat-1", "ping",
    )

  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)
  assert asyncio.run(_scenario()) is False
  assert registry.get_handle("chat-1", RunnerKind.CODEX_SDK) is None


def test_steer_into_active_turn_reraises_real_errors():
  async def _scenario() -> None:
    registry.register(codex_sdk_runner.ActiveCodexTurn(
      object(),
      _FakeTurnHandle(steer_exc=RuntimeError("real failure")),
      chat_id="chat-1",
    ))
    await codex_sdk_runner.steer_into_active_turn(
      "chat-1", "ping",
    )

  with pytest.raises(RuntimeError, match="real failure"):
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
    active = codex_sdk_runner.ActiveCodexTurn(
      object(), turn, chat_id="atomic-steer",
    )
    registry.register(active)
    try:
      first = asyncio.create_task(codex_sdk_runner.steer_into_active_turn(
        "atomic-steer", "first",
      ))
      await asyncio.wait_for(entered.wait(), timeout=1)
      assert active.steer_in_flight
      assert await codex_sdk_runner.steer_into_active_turn(
        "atomic-steer", "second",
      ) is False
      assert turn.steered == ["first"]

      release.set()
      assert await asyncio.wait_for(first, timeout=1) is True
      assert not active.steer_in_flight
    finally:
      registry.unregister("atomic-steer", RunnerKind.CODEX_SDK)

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
    task = asyncio.create_task(active_turn.interrupt())
    await asyncio.sleep(0)
    assert turn.interrupt_calls == 1
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


def test_is_closed_turn_error_matches_sdk_rpc_errors(monkeypatch):
  sdk = _fake_sdk(async_codex_cls=object)
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  invalid_params = sdk["InvalidParamsError"](-32602, "turn is not running")
  rpc_error = sdk["CodexRpcError"](-32000, "turn closed")

  assert codex_sdk_runner._is_closed_turn_error(invalid_params) is True
  assert codex_sdk_runner._is_closed_turn_error(rpc_error) is True


def test_is_closed_turn_error_does_not_treat_arbitrary_oserror_as_closed():
  assert codex_sdk_runner._is_closed_turn_error(OSError("disk full")) is False


def test_run_codex_sdk_turn_resume_mismatch_returns_error(monkeypatch):
  mismatched_thread = _FakeThread("actual-thread", _FakeTurnHandle())

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
  assert "different session id" in result["error"]
  assert bc.events == [{
    "type": "error",
    "message": (
      "Codex resume returned a different session id "
      "(actual-thread) than requested (requested-thread); "
      "start a fresh chat turn."
    ),
  }]


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


def test_subagent_activity_item_dispatch_is_noop():
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


def test_run_codex_sdk_turn_stream_exhaustion_relies_on_sdk_terminal_contract(
  monkeypatch,
):
  """SDK turn streams are expected to end via TurnCompleted, not fall-through."""
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

  assert result["error"] is None


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


def test_skill_names_in_command_ignores_other_paths():
  fn = codex_sdk_runner._skill_names_in_command
  assert fn("cat /data/shared/memory/index.md", "/data") == []
  assert fn("cat /elsewhere/shared/skills/memory.md", "/data") == []
  assert fn("cat /data/shared/skills/notes.txt", "/data") == []
  assert fn("", "/data") == []


def test_observe_skill_reads_publishes_chip_and_activity(monkeypatch):
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

  sdk = {"CommandExecutionThreadItem": _Cmd}
  bc = _FakeBroadcast()
  skills = os.path.join(get_settings().data_dir, "shared", "skills")
  item = _Cmd(f"cat {skills}/cron.md")
  codex_sdk_runner._observe_skill_reads(item, sdk, bc=bc, chat_id="cx-1")
  assert bc.events == [{"type": "skill_loaded", "skill": "cron"}]
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
  assert ov == ["features.default_mode_request_user_input=true"]
  assert not any("multi_agent_v2" in o for o in ov)


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
