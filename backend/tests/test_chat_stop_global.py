"""Global stop coverage across runner kinds."""

import asyncio

from app import chat as chat_mod
from app.runner_registry import RunnerKind, registry


class _Handle:
  def __init__(self, chat_id: str, kind: RunnerKind, called: dict[str, int]):
    self.chat_id = chat_id
    self.kind = kind
    self._called = called

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self._called[self.chat_id] = self._called.get(self.chat_id, 0) + 1
    return True


def test_global_stop_stops_all_registered_kinds():
  called: dict[str, int] = {}
  registry.register(_Handle("chat-proc", RunnerKind.SUBPROCESS, called))
  registry.register(_Handle("chat-claude", RunnerKind.CLAUDE_SDK, called))
  registry.register(_Handle("chat-codex", RunnerKind.CODEX_SDK, called))

  stopped = asyncio.run(chat_mod.stop_chat(None))

  assert stopped is True
  assert called == {
    "chat-proc": 1,
    "chat-claude": 1,
    "chat-codex": 1,
  }
  assert registry.all_alive_chat_ids() == set()


def test_chat_stop_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    "/api/chat/stop",
    json={"chat_id": chat.id},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403
