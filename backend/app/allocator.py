"""Process allocator tuning for memory-tight self-hosted Mobius instances."""

from __future__ import annotations

import ctypes
import os


# glibc's mallopt(3) constant. It deliberately is not exposed by Python's
# ``ctypes`` module, and is stable in glibc since M_ARENA_MAX was introduced.
_M_ARENA_MAX = -8


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
    return False
  if isinstance(max_arenas, bool) or not isinstance(max_arenas, int):
    return False
  if max_arenas < 1:
    return False
  try:
    libc = ctypes.CDLL(None)
    mallopt = libc.mallopt
    mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
    mallopt.restype = ctypes.c_int
    return mallopt(_M_ARENA_MAX, max_arenas) == 1
  # Allocator tuning is an optimization, never a boot dependency. ctypes can
  # surface platform-specific loader/call exceptions beyond its documented
  # AttributeError/OSError set, so fail open on every foreign-function error.
  except Exception:  # noqa: BLE001 - portability boundary; must not block boot
    return False
