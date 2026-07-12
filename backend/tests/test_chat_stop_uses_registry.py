"""Registry-backed stop coverage for chat lifecycle."""

import asyncio

from app import chat as chat_mod
from app.runner_registry import RunnerKind, registry


class _ClaudeHandle:
  def __init__(self, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self.stop_calls = 0

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self.stop_calls += 1
    return True


def test_stop_chat_for_uses_registry_and_clears_handle():
  handle = _ClaudeHandle("chat-claude-stop")
  registry.register(handle)

  stopped, _, _ = asyncio.run(chat_mod.stop_chat_for("chat-claude-stop"))

  assert stopped is True
  assert handle.stop_calls == 1
  assert registry.get_handle("chat-claude-stop", RunnerKind.CLAUDE_SDK) is None
