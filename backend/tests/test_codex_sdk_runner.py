import asyncio
from types import SimpleNamespace

import pytest

from app import codex_sdk_runner, models
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry


# Mirrors the installed SDK:
# - ErrorNotification: /usr/local/lib/python3.12/site-packages/openai_codex/generated/v2_all.py:6958
# - AppServerRpcError: /usr/local/lib/python3.12/site-packages/openai_codex/errors.py:24
# - InvalidParamsError: /usr/local/lib/python3.12/site-packages/openai_codex/errors.py:40


class _FakeBroadcast:
  def __init__(self):
    self.events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)


class _FakeAppServerConfig:
  def __init__(self, **kwargs):
    self.kwargs = kwargs


class _FakeApprovalMode:
  auto_review = "auto_review"


class _FakeSandboxMode:
  read_only = "read-only"
  workspace_write = "workspace-write"
  danger_full_access = "danger-full-access"


class _FakeReasoningEffort:
  """Callable enum stand-in so `ReasoningEffort(str)` works in tests."""
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

  async def turn(self, *_args, **_kwargs):
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

  class _FakeAppServerRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data=None):
      super().__init__(f"JSON-RPC error {code}: {message}")
      self.code = code
      self.message = message
      self.data = data

  return {
    "AgentMessageDeltaNotification": _Dummy,
    "ApprovalMode": _FakeApprovalMode,
    "AppServerConfig": _FakeAppServerConfig,
    "AsyncCodex": async_codex_cls,
    "AppServerRpcError": _FakeAppServerRpcError,
    "CommandExecutionOutputDeltaNotification": _Dummy,
    "CommandExecutionThreadItem": _Dummy,
    "ContextCompactedNotification": _Dummy,
    "DynamicToolCallThreadItem": _Dummy,
    "ErrorNotification": _FakeErrorNotification,
    "FileChangePatchUpdatedNotification": _Dummy,
    "FileChangeThreadItem": _Dummy,
    "InvalidParamsError": _FakeInvalidParamsError,
    "ReasoningEffort": _FakeReasoningEffort(),
    "SandboxMode": _FakeSandboxMode,
    "ItemCompletedNotification": _Dummy,
    "ItemGuardianApprovalReviewCompletedNotification": _Dummy,
    "ItemGuardianApprovalReviewStartedNotification": _Dummy,
    "ItemStartedNotification": _Dummy,
    "McpToolCallThreadItem": _Dummy,
    "ThreadTokenUsageUpdatedNotification": _Dummy,
    "TurnCompletedNotification": _FakeTurnCompletedNotification,
    "WebSearchThreadItem": _Dummy,
  }


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
  rpc_error = sdk["AppServerRpcError"](-32000, "turn closed")

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
