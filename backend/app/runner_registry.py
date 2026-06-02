"""Runner lifecycle registry shared across chat backends."""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable


class RunnerKind(str, Enum):
  """Concrete runner categories tracked per chat."""

  SUBPROCESS = "subprocess"
  CLAUDE_SDK = "claude_sdk"
  CODEX_SDK = "codex_sdk"


@runtime_checkable
class RunnerHandle(Protocol):
  """Protocol implemented by concrete runtime stop handles."""

  chat_id: str
  kind: RunnerKind

  async def stop(self, timeout: float = 2.0) -> bool:
    """Stops the underlying runner.

    Implementations re-raise `asyncio.CancelledError`, and otherwise
    log and return False on failure or timeout.
    """


class RunnerRegistry:
  """Single source of truth for per-chat runner state."""

  def __init__(self) -> None:
    self._starting: set[str] = set()
    self._handles: dict[tuple[str, RunnerKind], RunnerHandle] = {}
    self._generation: dict[str, int] = {}
    # Chats soft-deleted while a run was in flight. `current_generation`
    # returns +inf for these so a run holding any finite pre-delete run_gen
    # computes `we_own_gen=False` and skips finalize onto the dead row — the
    # delete-ABA guard. The FINITE counter is preserved (not popped), so
    # recovery resumes at a generation strictly newer than every pre-delete
    # run. Distinct from `forget`, which is normal turn-end cleanup.
    self._deleted: set[str] = set()

  def mark_starting(self, chat_id: str) -> bool:
    """Reserves a spawn slot for a chat if it is currently idle."""
    if chat_id in self._starting:
      return False
    if any(cid == chat_id for cid, _ in self._handles):
      return False
    self._starting.add(chat_id)
    return True

  def discard_starting(self, chat_id: str) -> None:
    """Clears a chat's starting reservation."""
    self._starting.discard(chat_id)

  def register(self, handle: RunnerHandle) -> None:
    """Registers or replaces the handle for one `(chat_id, kind)`."""
    self._handles[(handle.chat_id, handle.kind)] = handle
    self._starting.discard(handle.chat_id)

  def unregister(self, chat_id: str, kind: RunnerKind) -> None:
    """Drops one registered handle, if present."""
    self._handles.pop((chat_id, kind), None)

  def is_alive(self, chat_id: str) -> bool:
    """Returns True when the chat is starting or has any handle."""
    if chat_id in self._starting:
      return True
    return any(cid == chat_id for cid, _ in self._handles)

  def get_handle(
    self,
    chat_id: str,
    kind: RunnerKind,
  ) -> RunnerHandle | None:
    """Returns the registered handle for one `(chat_id, kind)`."""
    return self._handles.get((chat_id, kind))

  def get_handles(
    self,
    chat_id: str,
    kind: RunnerKind | None = None,
  ) -> list[RunnerHandle]:
    """Returns all handles for a chat, optionally filtered by kind."""
    return [
      handle
      for (cid, handle_kind), handle in self._handles.items()
      if cid == chat_id and (kind is None or handle_kind == kind)
    ]

  def all_alive_chat_ids(self) -> set[str]:
    """Returns the union of starting and registered chat ids."""
    return self._starting | {cid for cid, _ in self._handles}

  def starting_chat_ids(self) -> set[str]:
    """Returns a snapshot of chat ids reserved in the starting set."""
    return set(self._starting)

  def handles_by_kind(self, kind: RunnerKind) -> list[RunnerHandle]:
    """Returns all registered handles for a runner kind."""
    return [
      handle
      for (_, handle_kind), handle in self._handles.items()
      if handle_kind == kind
    ]

  def bump_generation(self, chat_id: str) -> int:
    """Increments and returns the per-chat generation counter."""
    next_generation = self._generation.get(chat_id, 0) + 1
    self._generation[chat_id] = next_generation
    return next_generation

  def current_generation(self, chat_id: str) -> int | float:
    """Returns the current generation for a chat.

    +inf while the chat is soft-deleted (see `mark_deleted`): no finite
    run_gen a live run holds can equal it, so every such run reads
    `we_own_gen=False` and bows out instead of finalizing onto the dead row.
    """
    if chat_id in self._deleted:
      return float("inf")
    return self._generation.get(chat_id, 0)

  def mark_deleted(self, chat_id: str) -> None:
    """Marks a chat soft-deleted: kills its in-flight run + denies ownership.

    Drops handles + the starting reservation (the run is being torn down) and
    flags the chat deleted so `current_generation` returns +inf. PRESERVES the
    finite generation counter so `recover_generation` can resume strictly
    newer. Unlike `forget`, this is delete-time cleanup, not turn-end.
    """
    self._deleted.add(chat_id)
    for kind in RunnerKind:
      self._handles.pop((chat_id, kind), None)
    self._starting.discard(chat_id)

  def recover_generation(self, chat_id: str) -> int:
    """Clears the deleted flag and bumps to a generation newer than any run.

    Called when a soft-deleted chat is recovered. Bumping the PRESERVED finite
    counter guarantees the recovered chat's next run_gen exceeds every
    pre-delete run, so no resurrected stale run can reclaim it.
    """
    self._deleted.discard(chat_id)
    return self.bump_generation(chat_id)

  def forget(self, chat_id: str) -> None:
    """Drops ALL registry state for a chat (normal turn-end cleanup)."""
    self._generation.pop(chat_id, None)
    self._deleted.discard(chat_id)
    for kind in RunnerKind:
      self._handles.pop((chat_id, kind), None)
    self._starting.discard(chat_id)

  def reset_for_tests(self) -> None:
    """Clears all registry state between tests."""
    self._starting.clear()
    self._handles.clear()
    self._generation.clear()
    self._deleted.clear()


registry = RunnerRegistry()
