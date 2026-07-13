"""Generation bump ordering coverage for stop_chat_for."""

import asyncio

from app import chat as chat_mod
from app.runner_registry import RunnerKind, registry


class _ObservingHandle:
  def __init__(self, chat_id: str, seen: list[int]):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self._seen = seen

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self._seen.append(chat_mod.current_run_generation(self.chat_id))
    return True


def test_stop_chat_for_bumps_generation_before_handle_stop():
  seen: list[int] = []
  registry.register(_ObservingHandle("chat-bump-order", seen))

  stopped, _, _ = asyncio.run(chat_mod.stop_chat_for("chat-bump-order"))

  assert stopped is True
  assert seen == [1]
