"""Process allocator tuning for memory-tight self-hosted Mobius instances."""

from __future__ import annotations

import ctypes
import os


# glibc's mallopt(3) constant. It deliberately is not exposed by Python's
# ``ctypes`` module, and is stable in glibc since M_ARENA_MAX was introduced.
_M_ARENA_MAX = -8
_status = {
  "arena_cap": None,
  "applied": False,
  "source": "not_attempted",
}


def allocator_status() -> dict:
  return dict(_status)


def limit_glibc_arenas(max_arenas: int = 2) -> bool:
  """Cap glibc's per-thread malloc arenas before worker threads start.

  On a many-core host glibc may otherwise create an arena for each busy Python
  thread (up to 8 × CPU count). Long-lived chat, DB, watcher, and executor
  threads then retain separate 64 MiB mappings after burst allocations are
  freed. Capping the arena count trades a little allocator concurrency for a
  much lower steady-state RSS. Mobius's single uvicorn worker has modest thread
  concurrency, so two shared arenas are the useful side of that trade.

  ``mallopt`` is glibc-specific. Managed images using another libc, and unusual
  platforms where the symbol is absent, simply keep their allocator default.
  This must be called during ``app.main`` import, before lifespan starts the
  writer/watcher/executor threads.
  """
  # An operator-provided glibc setting was applied before Python started and
  # is authoritative; do not silently replace deployment-specific tuning.
  if "MALLOC_ARENA_MAX" in os.environ:
    raw = os.environ.get("MALLOC_ARENA_MAX")
    try:
      cap = int(raw) if raw is not None else None
    except ValueError:
      cap = None
    valid = cap is not None and cap >= 1
    _status.update(
      arena_cap=cap,
      applied=valid,
      source="environment" if valid else "environment_invalid",
    )
    return False
  if isinstance(max_arenas, bool) or not isinstance(max_arenas, int):
    _status.update(arena_cap=None, applied=False, source="invalid")
    return False
  if max_arenas < 1:
    _status.update(arena_cap=max_arenas, applied=False, source="invalid")
    return False
  try:
    libc = ctypes.CDLL(None)
    mallopt = libc.mallopt
    mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
    mallopt.restype = ctypes.c_int
    applied = mallopt(_M_ARENA_MAX, max_arenas) == 1
    _status.update(
      arena_cap=max_arenas,
      applied=applied,
      source="mallopt" if applied else "mallopt_rejected",
    )
    return applied
  # Allocator tuning is an optimization, never a boot dependency. ctypes can
  # surface platform-specific loader/call exceptions beyond its documented
  # AttributeError/OSError set, so fail open on every foreign-function error.
  except Exception:  # noqa: BLE001 - portability boundary; must not block boot
    _status.update(
      arena_cap=max_arenas, applied=False, source="unsupported",
    )
    return False
