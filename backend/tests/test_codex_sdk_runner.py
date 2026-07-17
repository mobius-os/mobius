import asyncio
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

  assert events == [{"type": "tool_end"}]


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


def test_run_codex_sdk_turn_resumes_subagent_activity_history(monkeypatch, caplog):
  completed_turn = SimpleNamespace(id="turn-1", usage=None, error=None)
  turn_handle = _FakeTurnHandle([
    SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(completed_turn),
    )
  ])

  activities = [
    ("started", "/root/scout"),
    ("started", "/root/builder"),
    ("started", "/root/reviewer"),
    ("interacted", "/root/reviewer"),
    ("interrupted", "/root/reviewer"),
    ("interacted", "/root/reviewer"),
  ]

  class ResumeValidationError(Exception):
    def errors(self, include_url=False):
      del include_url
      errors = []
      for item_index, (kind, agent_path) in enumerate(activities):
        item = {
          "type": "subAgentActivity",
          "id": f"activity-{item_index}",
          "kind": kind,
          "agentThreadId": f"thread-{item_index}",
          "agentPath": agent_path,
        }
        for variant_index in range(44):
          errors.append({
            "loc": (
              "thread",
              "turns",
              1,
              "items",
              item_index,
              f"KnownThreadItem{variant_index}",
              "type",
            ),
            "input": item if variant_index % 2 == 0 else "subAgentActivity",
          })
      return errors

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_resume(self, *_args, **_kwargs):
      raise ResumeValidationError("264 validation errors for ThreadResumeResponse")

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk["AsyncThread"] = lambda _codex, thread_id: _FakeThread(
    thread_id,
    turn_handle,
  )
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

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

  assert result == {
    "session_id": "requested-thread",
    "cost_usd": None,
    "error": None,
  }
  assert bc.events == [{
    "type": "session_init",
    "session_id": "requested-thread",
  }]
  assert "rejected subAgentActivity history" in caplog.text


def test_subagent_activity_resume_compat_rejects_mixed_schema_drift():
  class MixedValidationError(Exception):
    def errors(self, include_url=False):
      del include_url
      return [
        {
          "loc": ("thread", "turns", 1, "items", 3, "KnownItem", "type"),
          "input": {
            "type": "subAgentActivity",
            "id": "activity-1",
            "kind": "started",
            "agentThreadId": "thread-1",
            "agentPath": "/root/scout",
          },
        },
        {
          "loc": ("thread", "model"),
          "input": {"unexpected": "response drift"},
        },
      ]

  assert codex_sdk_runner._is_subagent_activity_resume_validation_error(
    MixedValidationError(),
  ) is False


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


def test_tool_start_event_builds_collab_task_start():
  # A collab spawn becomes a subagent task_start so the shell renders it on the
  # same lane Claude's Task tool uses (task_type distinguishes the producer).
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(
    item_id="c-1", tool="spawnAgent", prompt="review the diff for races",
  )
  event = codex_sdk_runner._tool_start_event(item, sdk)
  assert event == {
    "type": "task_start",
    "task_id": "c-1",
    "description": "spawnAgent: review the diff for races",
    "task_type": "codex-collab",
    "tool_use_id": "c-1",
  }


def test_tool_start_event_collab_description_truncates_long_prompt():
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(tool="spawnAgent", prompt="x" * 500)
  event = codex_sdk_runner._tool_start_event(item, sdk)
  assert len(event["description"]) == 120
  assert event["description"].startswith("spawnAgent: xxx")


def test_tool_completed_events_builds_collab_task_done_completed():
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(
    item_id="c-2", status="completed",
    messages=["found a bug", "wrote a test"],
  )
  events = codex_sdk_runner._tool_completed_events(item, sdk)
  assert events == [{
    "type": "task_done",
    "task_id": "c-2",
    "status": "done",
    "summary": "found a bug; wrote a test",
    "tool_use_id": "c-2",
  }]


def test_tool_completed_events_collab_task_done_failed_empty_summary():
  # failed -> "failed"; a still-silent fleet (no messages) yields summary None,
  # never an empty string.
  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(item_id="c-3", status="failed", messages=[])
  events = codex_sdk_runner._tool_completed_events(item, sdk)
  assert events == [{
    "type": "task_done",
    "task_id": "c-3",
    "status": "failed",
    "summary": None,
    "tool_use_id": "c-3",
  }]


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
  db.add(models.Chat(
    id="collab-chat", title="t", messages=[], pending_messages=[],
    provider="codex", session_id=None,
  ))
  db.commit()

  sdk = {"CollabAgentToolCallThreadItem": _FakeCollabItem}
  item = _FakeCollabItem(tool="spawnAgent", receivers=["child-A", "child-B"])
  codex_sdk_runner._record_collab_child_links(
    item, sdk, db=db, chat_id="collab-chat",
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
  codex_sdk_runner._record_collab_child_links(
    item, sdk, db=db, chat_id="collab-chat-2",
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
