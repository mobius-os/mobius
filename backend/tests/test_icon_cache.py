"""Unit tests for the icon byte-cache (RAM LRU + disk tier)."""

import asyncio

from app import icon_cache


def _run(coro):
  return asyncio.run(coro)


def _fresh_lru():
  # Each test starts from a clean LRU so a sibling's entry can't satisfy a hit.
  icon_cache._lru.clear()


def test_cold_miss_computes_once_then_serves_from_cache():
  """The compute callback runs exactly once across repeated requests for the
  same key — the whole point of the cache (no per-request Pillow resize)."""
  _fresh_lru()
  calls = {"n": 0}

  def compute():
    calls["n"] += 1
    return b"PNGBYTES"

  async def go():
    a = await icon_cache.get_or_compute(
      app_id=42, updated_us=1000, kind="embed", size=64, compute=compute
    )
    b = await icon_cache.get_or_compute(
      app_id=42, updated_us=1000, kind="embed", size=64, compute=compute
    )
    return a, b

  a, b = _run(go())
  assert a == b == b"PNGBYTES"
  assert calls["n"] == 1, "compute should run once, not per request"


def test_warm_disk_hit_survives_lru_clear():
  """After the in-process LRU is cleared (simulating a fresh worker process),
  the disk tier still satisfies the request without recomputing."""
  _fresh_lru()
  calls = {"n": 0}

  def compute():
    calls["n"] += 1
    return b"DISKBYTES"

  async def go():
    await icon_cache.get_or_compute(
      app_id=7, updated_us=2000, kind="standalone", size=192, compute=compute
    )
    icon_cache._lru.clear()  # drop the RAM tier; force a disk read
    return await icon_cache.get_or_compute(
      app_id=7, updated_us=2000, kind="standalone", size=192, compute=compute
    )

  out = _run(go())
  assert out == b"DISKBYTES"
  assert calls["n"] == 1, "disk tier should serve the second call"


def test_different_version_is_a_different_key():
  """A bumped updated_us recomputes (the icon changed), so a stale entry is
  never returned for a new version."""
  _fresh_lru()

  async def go():
    v1 = await icon_cache.get_or_compute(
      app_id=9, updated_us=100, kind="embed", size=64, compute=lambda: b"v1"
    )
    v2 = await icon_cache.get_or_compute(
      app_id=9, updated_us=200, kind="embed", size=64, compute=lambda: b"v2"
    )
    return v1, v2

  v1, v2 = _run(go())
  assert v1 == b"v1"
  assert v2 == b"v2"


def test_new_version_prunes_stale_disk_entry():
  """Writing a new version of the same (app, kind, size) removes the old disk
  file, so the cache directory doesn't leak a file per icon change."""
  _fresh_lru()
  cache_dir = icon_cache._cache_dir()

  async def go():
    await icon_cache.get_or_compute(
      app_id=11, updated_us=1, kind="embed", size=128, compute=lambda: b"old"
    )
    await icon_cache.get_or_compute(
      app_id=11, updated_us=2, kind="embed", size=128, compute=lambda: b"new"
    )

  _run(go())
  remaining = list(cache_dir.glob("11-embed-128-*"))
  assert len(remaining) == 1, f"stale versions not pruned: {remaining}"
  assert remaining[0].read_bytes() == b"new"
