"""Unit tests for the runner registry module."""

import asyncio
from dataclasses import dataclass

from app.runner_registry import RunnerKind, RunnerRegistry


@dataclass
class _FakeHandle:
  chat_id: str
  kind: RunnerKind
  name: str

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    return True


def test_mark_starting_duplicate_returns_false():
  registry = RunnerRegistry()

  assert registry.mark_starting("chat-1") is True
  assert registry.mark_starting("chat-1") is False


def test_register_replaces_same_kind_handle():
  registry = RunnerRegistry()
  first = _FakeHandle("chat-1", RunnerKind.CLAUDE_SDK, "first")
  second = _FakeHandle("chat-1", RunnerKind.CLAUDE_SDK, "second")

  registry.register(first)
  registry.register(second)

  assert registry.get_handle("chat-1", RunnerKind.CLAUDE_SDK) is second
  assert registry.get_handles("chat-1") == [second]


def test_unregister_is_idempotent():
  registry = RunnerRegistry()
  registry.register(_FakeHandle("chat-1", RunnerKind.SUBPROCESS, "proc"))

  registry.unregister("chat-1", RunnerKind.SUBPROCESS)
  registry.unregister("chat-1", RunnerKind.SUBPROCESS)

  assert registry.get_handle("chat-1", RunnerKind.SUBPROCESS) is None


def test_is_alive_covers_starting_and_registered_handles():
  registry = RunnerRegistry()

  assert registry.is_alive("chat-1") is False
  assert registry.mark_starting("chat-1") is True
  assert registry.is_alive("chat-1") is True

  registry.discard_starting("chat-1")
  registry.register(_FakeHandle("chat-1", RunnerKind.CODEX_SDK, "codex"))

  assert registry.is_alive("chat-1") is True


def test_get_accessors_and_handles_by_kind():
  registry = RunnerRegistry()
  proc = _FakeHandle("chat-1", RunnerKind.SUBPROCESS, "proc")
  claude = _FakeHandle("chat-1", RunnerKind.CLAUDE_SDK, "claude")
  codex = _FakeHandle("chat-2", RunnerKind.CODEX_SDK, "codex")

  registry.register(proc)
  registry.register(claude)
  registry.register(codex)

  assert registry.get_handle("chat-1", RunnerKind.SUBPROCESS) is proc
  assert registry.get_handles("chat-1") == [proc, claude]
  assert registry.get_handles(
    "chat-1", RunnerKind.CLAUDE_SDK
  ) == [claude]
  assert registry.handles_by_kind(RunnerKind.CODEX_SDK) == [codex]
  assert registry.all_alive_chat_ids() == {"chat-1", "chat-2"}


def test_generation_helpers_and_forget():
  registry = RunnerRegistry()

  assert registry.current_generation("chat-1") == 0
  assert registry.bump_generation("chat-1") == 1
  assert registry.bump_generation("chat-1") == 2
  assert registry.current_generation("chat-1") == 2

  registry.forget("chat-1")
  assert registry.current_generation("chat-1") == 0


def test_mark_starting_register_clears_starting():
  registry = RunnerRegistry()

  assert registry.mark_starting("chat-1") is True
  registry.register(_FakeHandle("chat-1", RunnerKind.SUBPROCESS, "proc"))

  assert registry.mark_starting("chat-1") is False
  registry.unregister("chat-1", RunnerKind.SUBPROCESS)
  assert registry.mark_starting("chat-1") is True


def test_concurrent_mark_starting_only_one_true():
  registry = RunnerRegistry()

  async def _mark() -> bool:
    await asyncio.sleep(0)
    return registry.mark_starting("chat-1")

  async def _run() -> list[bool]:
    return await asyncio.gather(*[_mark() for _ in range(8)])

  results = asyncio.run(_run())

  assert sum(1 for result in results if result) == 1
