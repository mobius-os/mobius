"""Delete-safety contract for registry-backed stop."""

import asyncio

from app import chat as chat_mod
from app.runner_registry import RunnerKind, registry


class _FailingHandle:
  def __init__(self, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self.stop_calls = 0

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self.stop_calls += 1
    return False


def test_stop_chat_for_false_unregisters_handle():
  handle = _FailingHandle("chat-delete-safety")
  registry.register(handle)

  stopped = asyncio.run(chat_mod.stop_chat_for("chat-delete-safety"))

  assert stopped is False
  assert handle.stop_calls == 1
  assert (
    registry.get_handle("chat-delete-safety", RunnerKind.CLAUDE_SDK)
    is None
  )


def test_stop_chat_for_unregisters_on_timeout():
  chat_id = "chat-delete-safety-timeout"
  handle = _FailingHandle(chat_id)
  registry.register(handle)

  stopped = asyncio.run(chat_mod.stop_chat_for(chat_id))

  assert stopped is False
  assert handle.stop_calls == 1
  assert registry.is_alive(chat_id) is False
  assert chat_mod.is_chat_running(chat_id) is False
