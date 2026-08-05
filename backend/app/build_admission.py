"""One cross-process lease for every Mobius-owned JavaScript build.

Four separate processes can start a native JS build on the same box: uvicorn's
mini-app Rolldown compile, the warm frontend watcher's Vite build,
``rebuild_shell.sh``, and the in-container app validator. Each peaks at
hundreds of megabytes of native memory, so two at once OOM a small instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from pathlib import Path


class BuildLeaseUnavailable(RuntimeError):
  """No lease became free within the wait budget."""


# One Vite build is capped at 180s, so a wait longer than that means a builder
# is stuck rather than merely busy. Failing then is better than hanging.
_LEASE_WAIT_SECS = 240.0
_LEASE_POLL_SECS = 0.1


@contextlib.contextmanager
def build_lease(*, blocking: bool = True, timeout: float = _LEASE_WAIT_SECS):
  """Serialize every Mobius-owned JS build across processes."""
  try:
    # Imported here so the stdlib-only app validator keeps working in a
    # developer checkout that has neither settings nor pydantic installed.
    from app.config import get_settings

    path = Path(get_settings().data_dir) / "run" / "build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
  except Exception:
    yield  # No reachable runtime directory means no competing Mobius process.
    return
  deadline = time.monotonic() + timeout
  try:
    while True:
      try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
      except BlockingIOError:
        if not blocking or time.monotonic() >= deadline:
          raise BuildLeaseUnavailable("another JS build holds the lease")
        time.sleep(_LEASE_POLL_SECS)
    yield
  finally:
    os.close(fd)  # Closing releases the lock, including on process death.


@contextlib.asynccontextmanager
async def build_lease_async(*, timeout: float = _LEASE_WAIT_SECS):
  """Hold the same lease from the event loop, polling instead of stalling it."""
  deadline = time.monotonic() + timeout
  with contextlib.ExitStack() as stack:
    while True:
      try:
        stack.enter_context(build_lease(blocking=False))
        break
      except BuildLeaseUnavailable:
        if time.monotonic() >= deadline:
          raise
        await asyncio.sleep(_LEASE_POLL_SECS)
    yield
