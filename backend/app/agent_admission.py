"""Global admission control for agent turns.

Turn dispatch is otherwise unbounded — every turn spawns a heavy provider
subprocess, so a fan-out of many concurrent chats + delegated subagents can
exhaust the container's memory. This module caps the number of *concurrent*
turns with two in-process semaphores; excess turns park at ``acquire`` holding
no subprocess (and no registry handle) until a slot frees.

Two buckets, keyed on whether the turn is a delegation CHILD: a parent turn that
blocks in-turn on a delegated child must not compete with that child for the
SAME slot, or a saturated pool would deadlock (parent holds a slot, child can't
get one). Children are depth-1 (they cannot spawn further children), so two
buckets are sufficient — there is no deeper nesting to account for.

Single-process design: the whole backend runs one event loop, so plain
``asyncio.Semaphore`` (not a cross-process lock) is the right primitive. The
per-turn subprocess is a child of this process; the semaphore gates how many
``run_chat`` coroutines may proceed to spawn one.
"""

from __future__ import annotations

import asyncio
import contextlib

_main_sem: asyncio.Semaphore | None = None
_child_sem: asyncio.Semaphore | None = None


def _semaphores() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
  """Lazily build the two semaphores from settings on first use.

  Built lazily (inside a running turn, hence inside the event loop) rather than
  at import so construction can't race module import order, and so tests that
  reconfigure the ceilings via ``reset_for_tests`` take effect.
  """
  global _main_sem, _child_sem
  if _main_sem is None or _child_sem is None:
    from app.config import get_settings

    settings = get_settings()
    _main_sem = asyncio.Semaphore(max(1, settings.max_concurrent_agent_turns))
    _child_sem = asyncio.Semaphore(
      max(1, settings.max_concurrent_delegated_turns)
    )
  return _main_sem, _child_sem


@contextlib.asynccontextmanager
async def turn_slot(*, delegated: bool):
  """Hold one admission slot for the duration of a turn.

  ``delegated`` selects the bucket: delegation children draw from the child
  pool, everything else from the main pool. A queued turn awaits here holding
  no subprocess; the slot is released on every exit (return, error, cancel).
  """
  main, child = _semaphores()
  sem = child if delegated else main
  await sem.acquire()
  try:
    yield
  finally:
    sem.release()


def reset_for_tests() -> None:
  """Drop the cached semaphores so the next turn rebuilds them from settings.

  Tests set small ceilings via the settings override then call this so the new
  values take effect.
  """
  global _main_sem, _child_sem
  _main_sem = None
  _child_sem = None
