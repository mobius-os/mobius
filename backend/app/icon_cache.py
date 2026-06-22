"""Server-side cache of downscaled app-icon PNG bytes.

Every app-icon request — the embedded mini-app rendering its own brand logo at
`size=64`/`128`, the standalone PWA asking for `icon-192.png`, the store grid
thumbnails — used to run a fresh Pillow LANCZOS resize + PNG re-encode on the
sync handler thread. With no server-side cache of the downscaled bytes, a
screen that shows a dozen icons fired a dozen synchronous resizes that
serialized through the framework threadpool (each also holding a sync DB
session), which the owner saw as icons trickling in one-by-one inside a
mini-app. The pixels are deterministic, so recomputing them per request is
pure waste.

This module memoizes the FINAL encoded bytes keyed on everything that can
change the output: the app id, the app's `updated_at` (any icon / name /
background change bumps it — it is the same validator the ETag already keys
on), a `kind` discriminator (the embedded `/api/apps/{id}/icon` variant vs the
composited standalone variant render differently from the same stored PNG),
and the pixel size. A changed icon advances `updated_at`, which changes the
key, so a stale entry is never served; an unchanged icon is computed exactly
once across the process lifetime and every host.

Two tiers:

- An in-process LRU (RAM) — warm hits return bytes with zero syscalls.
- A disk tier under `<data_dir>/compiled/icons` — survives a restart and is
  shared across worker processes, so a cold process warms from a sibling's
  earlier compute instead of recomputing. The filename folds the full key, so
  the directory accumulates one file per (app, version, kind, size); stale
  versions are pruned opportunistically when a new version of the same
  (app, kind, size) lands.

`get_or_compute` is the only entry point. It runs the (CPU-bound) `compute`
callback off the event loop via `run_in_threadpool`, so a cold miss never
blocks the other sync routes sharing the threadpool — the original
serialization bug — and warm hits skip the threadpool entirely.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from starlette.concurrency import run_in_threadpool

from app.config import get_settings

# In-process LRU. Icons are tens of KB downscaled; 256 entries (a few MB)
# comfortably covers every app's every size for a typical instance while
# bounding memory. The lock guards the OrderedDict's move-to-end + eviction,
# which are not atomic under concurrent awaits resuming on the same loop.
_LRU_MAX = 256
_lru: "OrderedDict[str, bytes]" = OrderedDict()
_lru_lock = asyncio.Lock()


def _cache_dir() -> Path:
  return Path(get_settings().data_dir) / "compiled" / "icons"


def _key(app_id: int, updated_us: int, kind: str, size: int | None) -> str:
  """A filesystem-safe, collision-resistant key for one rendered variant.

  `size=None` (the embedded route's full-res passthrough) is folded as `full`
  so it never collides with a numeric size, and the whole tuple is hashed so a
  weird `kind` string can't escape the cache directory."""
  raw = f"{app_id}:{updated_us}:{kind}:{size if size is not None else 'full'}"
  digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
  return f"{app_id}-{kind}-{size if size is not None else 'full'}-{digest}"


def _prune_stale_versions(app_id: int, kind: str, size: int | None, keep: str) -> None:
  """Delete disk entries for the same (app, kind, size) but a different
  version key. Bounds the cache directory at one file per logical variant
  instead of leaking a file on every icon change. Best-effort: a racing
  reader/writer or a permission hiccup is swallowed — the cache is a perf
  optimization, never a correctness dependency."""
  prefix = f"{app_id}-{kind}-{size if size is not None else 'full'}-"
  try:
    for entry in _cache_dir().iterdir():
      name = entry.name
      if name.startswith(prefix) and name != keep:
        try:
          entry.unlink()
        except OSError:
          pass
  except OSError:
    pass


async def get_or_compute(
  *,
  app_id: int,
  updated_us: int,
  kind: str,
  size: int | None,
  compute: Callable[[], bytes],
) -> bytes:
  """Return the cached rendered PNG bytes for this variant, computing them
  exactly once on a cold miss.

  Lookup order: RAM LRU → disk → `compute()`. The compute runs in a worker
  thread so a cold miss never blocks the shared sync-route threadpool. The
  result is written back to both tiers.
  """
  key = _key(app_id, updated_us, kind, size)

  async with _lru_lock:
    hit = _lru.get(key)
    if hit is not None:
      _lru.move_to_end(key)
      return hit

  path = _cache_dir() / key
  # Disk read is cheap (tens of KB) and tolerant of the file vanishing under a
  # concurrent prune — fall through to compute on any failure.
  try:
    data = await run_in_threadpool(path.read_bytes)
  except OSError:
    data = None

  if data is None:
    data = await run_in_threadpool(compute)
    await run_in_threadpool(_write_disk, path, data)
    _prune_stale_versions(app_id, kind, size, key)

  async with _lru_lock:
    _lru[key] = data
    _lru.move_to_end(key)
    while len(_lru) > _LRU_MAX:
      _lru.popitem(last=False)

  return data


def _write_disk(path: Path, data: bytes) -> None:
  """Atomically publish `data` at `path`. A temp-file + rename means a
  concurrent reader sees either the old file or the whole new file, never a
  half-written one. Best-effort — a write failure just means the next request
  recomputes."""
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
  except OSError:
    try:
      tmp.unlink()
    except (OSError, NameError, UnboundLocalError):
      pass
