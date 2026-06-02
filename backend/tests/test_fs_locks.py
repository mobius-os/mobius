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
