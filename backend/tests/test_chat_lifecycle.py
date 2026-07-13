"""Stop-contract tests for the SDK paths (round-3 hardening).

Covers two failure modes that subprocess-only tests can't reach:

- R2-6: a wedged Claude SDK client whose `interrupt()` never returns
  must not let `stop_chat_for` hang past the 2s bound (we wrap with
  `asyncio.wait_for`).
- R2-4: the global stop sweep (`stop_chat(None)`) must iterate the
  SDK registries (`_active_clients`, `_active_sessions`) so an
  SDK-only chat — no subprocess, no live broadcast — still gets
  interrupted.
"""

import asyncio
import time

from app import chat as chat_mod
from app.runner_registry import RunnerKind, registry


class HangingHandle:
  """Fake handle whose stop() never returns before the timeout."""

  chat_id = "testchat"
  kind = RunnerKind.CLAUDE_SDK

  async def stop(self, timeout=2.0):
    await asyncio.sleep(timeout + 0.1)
    return False


def test_stop_chat_for_wedged_sdk_client_times_out(client, auth, chat):
  """A wedged SDK client must not hang stop_chat_for past ~2s.

  The handle is intentionally left registered when stop() times out:
  the zombie runner is still draining (its SDK subprocess has not
  confirmed shutdown), so unregistering it now would allow a later
  reclaim of the chat before the runner's own finally runs — the
  registry entry is the guard the runner's teardown path uses.
  """
  hanging = HangingHandle()
  hanging.chat_id = chat.id
  registry.register(hanging)
  start = time.monotonic()
  stopped, _, _ = asyncio.run(chat_mod.stop_chat_for(chat.id))
  elapsed = time.monotonic() - start
  assert elapsed < 3.0, f"stop_chat_for hung for {elapsed}s"
  assert stopped is False
  # Handle stays registered: the zombie runner owns teardown.
  assert registry.get_handle(chat.id, RunnerKind.CLAUDE_SDK) is not None


def test_global_stop_targets_sdk_only_chats(client, auth, chat):
  """stop_chat(None) must interrupt chats registered only in
  the runner registry (no proc, no broadcast)."""
  called = {"claude": False, "codex": False}

  class FakeClient:
    chat_id = "claude-chat-id"
    kind = RunnerKind.CLAUDE_SDK

    async def stop(self, timeout=2.0):
      del timeout
      called["claude"] = True
      return True

  class FakeSession:
    chat_id = "codex-chat-id"
    kind = RunnerKind.CODEX_SDK

    async def stop(self, timeout=2.0):
      del timeout
      called["codex"] = True
      return True

  registry.register(FakeClient())
  registry.register(FakeSession())
  stopped, _, _ = asyncio.run(chat_mod.stop_chat(None))
  assert stopped is True
  assert called["claude"] and called["codex"]
  assert registry.get_handle("claude-chat-id", RunnerKind.CLAUDE_SDK) is None
  assert registry.get_handle("codex-chat-id", RunnerKind.CODEX_SDK) is None
