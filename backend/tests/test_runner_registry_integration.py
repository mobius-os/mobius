"""Integration coverage for SDK runner registration lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.runner_registry import RunnerKind, registry


class _FakeBroadcast:
  def __init__(self):
    self.events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)

  def mark_completed(self) -> None:
    return None


def test_claude_runner_registers_then_unregisters_handle():
  from app import claude_sdk_runner as runner

  class FakeStreamEvent:
    def __init__(self, session_id, event):
      self.session_id = session_id
      self.event = event

  class FakeResultMessage:
    def __init__(self, session_id):
      self.session_id = session_id
      self.total_cost_usd = 0.25
      self.is_error = False
      self.result = ""
      self.errors = []
      self.subtype = "success"

  release = asyncio.Event()

  class FakeClaudeSDKClient:
    def __init__(self, _options):
      self.connected = False

    async def connect(self):
      self.connected = True

    async def query(self, _user_message):
      return None

    async def receive_response(self):
      await release.wait()
      yield FakeStreamEvent(
        "session-1",
        {
          "type": "content_block_delta",
          "delta": {"type": "text_delta", "text": "hello"},
        },
      )
      yield FakeResultMessage("session-1")

    async def disconnect(self):
      self.connected = False

    async def interrupt(self):
      return None

  async def _scenario() -> None:
    task = asyncio.create_task(
      runner.run_claude_sdk_turn(
        user_message="hello",
        session_id=None,
        base_env={},
        cwd="/tmp",
        chat_id="chat-claude",
        skill_text="skill",
        bc=_FakeBroadcast(),
        pending_questions={},
        db=None,
      )
    )
    for _ in range(20):
      handle = registry.get_handle("chat-claude", RunnerKind.CLAUDE_SDK)
      if handle is not None:
        break
      await asyncio.sleep(0)
    else:
      raise AssertionError("claude handle never registered")

    assert registry.get_handle("chat-claude", RunnerKind.CLAUDE_SDK) is not None
    release.set()
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["session_id"] == "session-1"
    assert registry.get_handle("chat-claude", RunnerKind.CLAUDE_SDK) is None

  with patch.object(runner, "ClaudeSDKClient", FakeClaudeSDKClient), \
       patch.object(runner, "StreamEvent", FakeStreamEvent), \
       patch.object(runner, "ResultMessage", FakeResultMessage), \
       patch.object(runner, "SystemMessage", type("FakeSystemMessage", (), {})), \
       patch.object(runner, "AssistantMessage", type("FakeAssistantMessage", (), {})), \
       patch.object(runner, "UserMessage", type("FakeUserMessage", (), {})):
    asyncio.run(_scenario())


def test_codex_runner_registers_then_unregisters_handle(monkeypatch):
  from app import codex_sdk_runner as runner

  release = asyncio.Event()

  class FakeTurnCompletedNotification:
    def __init__(self, turn):
      self.turn = turn

  class FakeTurnHandle:
    async def stream(self):
      await release.wait()
      yield SimpleNamespace(
        method="turn/completed",
        payload=FakeTurnCompletedNotification(
          SimpleNamespace(id="turn-1", usage=None, error=None),
        ),
      )

    async def interrupt(self):
      return None

  class FakeThread:
    def __init__(self):
      self.id = "thread-1"

    async def turn(self, *_args, **_kwargs):
      return FakeTurnHandle()

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config
      self._client = SimpleNamespace(
        _sync=SimpleNamespace(_approval_handler=None)
      )

    async def __aenter__(self):
      return self

    async def __aexit__(self, _exc_type, _exc, _tb):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return FakeThread()

  sdk = {
    "AgentMessageDeltaNotification": type("AgentMessageDeltaNotification", (), {}),
    "ApprovalMode": type("ApprovalMode", (), {"auto_review": "auto_review"}),
    "AppServerConfig": lambda **kwargs: SimpleNamespace(**kwargs),
    "AsyncCodex": FakeAsyncCodex,
    "AppServerRpcError": RuntimeError,
    "CommandExecutionOutputDeltaNotification": type(
      "CommandExecutionOutputDeltaNotification", (), {}
    ),
    "CommandExecutionThreadItem": type("CommandExecutionThreadItem", (), {}),
    "ContextCompactedNotification": type("ContextCompactedNotification", (), {}),
    "DynamicToolCallThreadItem": type("DynamicToolCallThreadItem", (), {}),
    "ErrorNotification": type("ErrorNotification", (), {}),
    "FileChangePatchUpdatedNotification": type(
      "FileChangePatchUpdatedNotification", (), {}
    ),
    "FileChangeThreadItem": type("FileChangeThreadItem", (), {}),
    "InvalidParamsError": RuntimeError,
    "ItemCompletedNotification": type("ItemCompletedNotification", (), {}),
    "ItemGuardianApprovalReviewCompletedNotification": type(
      "ItemGuardianApprovalReviewCompletedNotification", (), {}
    ),
    "ItemGuardianApprovalReviewStartedNotification": type(
      "ItemGuardianApprovalReviewStartedNotification", (), {}
    ),
    "ItemStartedNotification": type("ItemStartedNotification", (), {}),
    "McpToolCallThreadItem": type("McpToolCallThreadItem", (), {}),
    "ReasoningEffort": lambda value: value,
    "SandboxMode": type(
      "SandboxMode",
      (),
      {
        "read_only": "read-only",
        "workspace_write": "workspace-write",
        "danger_full_access": "danger-full-access",
      },
    ),
    "ThreadTokenUsageUpdatedNotification": type(
      "ThreadTokenUsageUpdatedNotification", (), {}
    ),
    "TurnCompletedNotification": FakeTurnCompletedNotification,
    "WebSearchThreadItem": type("WebSearchThreadItem", (), {}),
  }

  async def _scenario() -> None:
    task = asyncio.create_task(
      runner.run_codex_sdk_turn(
        user_message="hello",
        session_id=None,
        base_env={},
        cwd="/tmp",
        chat_id="chat-codex",
        bc=_FakeBroadcast(),
        pending_questions={},
        db=None,
      )
    )
    for _ in range(20):
      handle = registry.get_handle("chat-codex", RunnerKind.CODEX_SDK)
      if handle is not None:
        break
      await asyncio.sleep(0)
    else:
      raise AssertionError("codex handle never registered")

    assert registry.get_handle("chat-codex", RunnerKind.CODEX_SDK) is not None
    release.set()
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["session_id"] == "thread-1"
    assert registry.get_handle("chat-codex", RunnerKind.CODEX_SDK) is None

  monkeypatch.setattr(runner, "_sdk_imports", lambda: sdk)
  asyncio.run(_scenario())
