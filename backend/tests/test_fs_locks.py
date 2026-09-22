"""The per-app / per-source-dir async locks that serialize storage-tree and
source-tree mutations against uninstall (Codex review round-6 #3, #4)."""

import asyncio

import pytest

from app import fs_locks


def test_same_key_returns_same_lock():
  # Hold strong refs so the WeakValueDictionary can't GC between calls.
  a1 = fs_locks.app_storage_lock(7)
  a2 = fs_locks.app_storage_lock(7)
  a8 = fs_locks.app_storage_lock(8)
  assert a1 is a2
  assert a1 is not a8
  s1 = fs_locks.source_dir_lock("/data/apps/x")
  s2 = fs_locks.source_dir_lock("/data/apps/x")
  s3 = fs_locks.source_dir_lock("/data/apps/y")
  assert s1 is s2
  assert s1 is not s3


@pytest.mark.asyncio
async def test_lock_serializes_critical_sections():
  """Two tasks holding the same lock never overlap — the write/uninstall and
  create/uninstall critical sections can't interleave on the one worker."""
  lock = fs_locks.app_storage_lock(99)
  order = []

  async def worker(n):
    async with lock:
      order.append(("enter", n))
      await asyncio.sleep(0.01)   # yield — a non-serialized lock would interleave
      order.append(("exit", n))

  await asyncio.gather(worker(1), worker(2))
  # Each enter is immediately followed by its OWN exit (no interleaving).
  assert order in (
    [("enter", 1), ("exit", 1), ("enter", 2), ("exit", 2)],
    [("enter", 2), ("exit", 2), ("enter", 1), ("exit", 1)],
  )


@pytest.mark.asyncio
async def test_cwd_reservation_serializes_mutating_tool_calls(tmp_path):
  """Closes the residual cross-chat race generated_files' own-namespace
  exclusion can't: two chats sharing the same cwd (settings.data_dir) must
  never have their mutating-tool diff windows overlap, or one chat's
  concurrently-written file can land inside the other's before/after
  snapshot pair and get misattributed."""
  cwd = str(tmp_path)
  order = []

  async def turn(n):
    reservation = await fs_locks.reserve_cwd_for_mutation(cwd)
    assert reservation is not None
    order.append(("enter", n))
    await asyncio.sleep(0.01)
    order.append(("exit", n))
    reservation.release()

  await asyncio.gather(turn(1), turn(2))
  assert order in (
    [("enter", 1), ("exit", 1), ("enter", 2), ("exit", 2)],
    [("enter", 2), ("exit", 2), ("enter", 1), ("exit", 1)],
  )


@pytest.mark.asyncio
async def test_cwd_reservation_release_is_idempotent(tmp_path):
  reservation = await fs_locks.reserve_cwd_for_mutation(str(tmp_path))
  assert reservation is not None
  reservation.release()
  reservation.release()  # must not raise or double-unlock


@pytest.mark.asyncio
async def test_cwd_reservation_self_expires_without_explicit_release(tmp_path):
  """A reservation whose release() is never called (a killed provider
  process, an interrupted turn) must not wedge every other chat sharing
  this cwd forever — it self-expires via a watchdog after `max_hold`."""
  cwd = str(tmp_path)
  held = await fs_locks.reserve_cwd_for_mutation(cwd, max_hold=0.02)
  assert held is not None
  # No release() call — simulates the hook that was supposed to call it
  # never firing. The watchdog must free the lock anyway.
  waiter = await asyncio.wait_for(
    fs_locks.reserve_cwd_for_mutation(cwd, acquire_timeout=1.0), timeout=2.0,
  )
  assert held.is_held is False
  assert waiter is not None
  waiter.release()


@pytest.mark.asyncio
async def test_cwd_reservation_gives_up_after_acquire_timeout(tmp_path):
  """A turn must never hang forever behind another chat's reservation — a
  bounded acquire_timeout gives up and returns None (proceed unprotected)
  rather than blocking indefinitely."""
  cwd = str(tmp_path)
  held = await fs_locks.reserve_cwd_for_mutation(cwd, max_hold=10.0)
  assert held is not None
  try:
    result = await asyncio.wait_for(
      fs_locks.reserve_cwd_for_mutation(cwd, acquire_timeout=0.05),
      timeout=2.0,
    )
    assert result is None
  finally:
    held.release()
