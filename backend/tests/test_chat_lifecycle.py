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


class HangingClient:
  """Fake ActiveClaudeClient whose interrupt() never returns."""

  async def interrupt(self):
    await asyncio.Event().wait()  # forever


def test_stop_chat_for_wedged_sdk_client_times_out(client, auth, chat):
  """A wedged SDK client must not hang stop_chat_for past ~2s."""
  chat_mod._active_clients[chat.id] = HangingClient()
  start = time.monotonic()
  asyncio.run(chat_mod.stop_chat_for(chat.id))
  elapsed = time.monotonic() - start
  assert elapsed < 3.0, f"stop_chat_for hung for {elapsed}s"
  assert chat.id not in chat_mod._active_clients


def test_global_stop_targets_sdk_only_chats(client, auth, chat):
  """stop_chat(None) must interrupt chats registered only in
  _active_clients / _active_sessions (no proc, no broadcast)."""
  called = {"claude": False, "codex": False}

  class FakeClient:
    async def interrupt(self):
      called["claude"] = True

  class FakeSession:
    async def interrupt(self):
      called["codex"] = True

  chat_mod._active_clients["claude-chat-id"] = FakeClient()
  chat_mod._active_sessions["codex-chat-id"] = FakeSession()
  asyncio.run(chat_mod.stop_chat(None))
  assert called["claude"] and called["codex"]
  assert "claude-chat-id" not in chat_mod._active_clients
  assert "codex-chat-id" not in chat_mod._active_sessions
