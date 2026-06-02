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


def test_forget_clears_handles_and_starting():
  registry = RunnerRegistry()
  handle = _FakeHandle("chat-1", RunnerKind.SUBPROCESS, "proc")

  assert registry.mark_starting("chat-1") is True
  registry.register(handle)
  registry._starting.add("chat-1")
  registry.forget("chat-1")

  assert registry.get_handle("chat-1", RunnerKind.SUBPROCESS) is None
  assert "chat-1" not in registry.starting_chat_ids()
  assert registry.current_generation("chat-1") == 0


def test_public_starting_accessor_and_reset_for_tests():
  registry = RunnerRegistry()
  registry.mark_starting("chat-1")
  registry.bump_generation("chat-1")
  registry.register(_FakeHandle("chat-2", RunnerKind.SUBPROCESS, "proc"))

  assert registry.starting_chat_ids() == {"chat-1"}

  registry.reset_for_tests()

  assert registry.starting_chat_ids() == set()
  assert registry.all_alive_chat_ids() == set()
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


def test_forget_resets_generation_to_reusable_zero():
  # forget is normal turn-end cleanup: the chat lives on, so the next turn
  # legitimately reuses generation 0.
  registry = RunnerRegistry()
  registry.bump_generation("c")
  registry.forget("c")
  assert registry.current_generation("c") == 0


def test_mark_deleted_denies_ownership_via_infinity():
  # The delete-ABA guard: a soft-deleted chat reads +inf, so a run holding
  # ANY finite pre-delete generation (including 0 on a brand-new chat) reads
  # we_own_gen=False and skips finalizing onto the dead row.
  registry = RunnerRegistry()
  registry.mark_deleted("fresh")  # never bumped → would default to 0
  assert registry.current_generation("fresh") == float("inf")
  run_gen = 0
  we_own_gen = registry.current_generation("fresh") == run_gen
  assert we_own_gen is False


def test_recover_generation_resumes_strictly_newer_and_finite():
  registry = RunnerRegistry()
  registry.bump_generation("c")  # → 1, a pre-delete run holds this
  registry.mark_deleted("c")
  assert registry.current_generation("c") == float("inf")
  recovered = registry.recover_generation("c")
  assert recovered > 1, "recovery must resume strictly newer than any run"
  assert registry.current_generation("c") == recovered  # finite again
  assert registry.current_generation("c") != float("inf")
