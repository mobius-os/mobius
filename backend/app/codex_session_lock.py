"""Cross-process ownership boundary for Codex rollout storage.

Every Codex launcher holds a shared advisory lock for its complete process
lifetime. Retention takes the exclusive side non-blockingly, so it can never
unlink an old rollout while a web turn, Reflection, or a settings probe may be
resuming or writing it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path


# Codex SDK waits can occupy every default asyncio worker. Lock acquisition is
# part of starting/closing that SDK, so it needs a tiny independent executor or
# it can deadlock behind the work it is meant to protect.
_LOCK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
  max_workers=8,
  thread_name_prefix="mobius-codex-session-lock",
)


@dataclass
class CodexSessionLock:
  fd: int

  def release(self) -> None:
    if self.fd < 0:
      return
    fd, self.fd = self.fd, -1
    try:
      fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
      os.close(fd)


def _open_lock(data_dir: str | Path) -> int:
  home = Path(data_dir) / "cli-auth" / "codex"
  home.mkdir(parents=True, exist_ok=True)
  # Lock the existing directory inode itself. Startup retention must remain
  # able to unlink stale rollouts at block/inode ENOSPC; creating a new lock
  # file at that moment would defeat the recovery path it protects.
  return os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def acquire_codex_session_activity(data_dir: str | Path) -> CodexSessionLock:
  """Acquire the shared launcher side, waiting out a short active sweep."""
  fd = _open_lock(data_dir)
  try:
    fcntl.flock(fd, fcntl.LOCK_SH)
  except BaseException:
    os.close(fd)
    raise
  return CodexSessionLock(fd)


async def acquire_codex_session_activity_async(
  data_dir: str | Path,
) -> CodexSessionLock:
  """Acquire without blocking the event loop or leaking on cancellation."""
  task = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(
    _LOCK_EXECUTOR,
    acquire_codex_session_activity,
    data_dir,
  ))
  deferred_cancel: asyncio.CancelledError | None = None
  while not task.done():
    try:
      await asyncio.shield(task)
    except asyncio.CancelledError as exc:
      deferred_cancel = deferred_cancel or exc
  ownership = task.result()
  if deferred_cancel is not None:
    ownership.release()
    raise deferred_cancel
  return ownership


def try_acquire_codex_session_sweep(
  data_dir: str | Path,
) -> CodexSessionLock | None:
  """Acquire exclusive retention ownership, or return when any launcher runs."""
  fd = _open_lock(data_dir)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    os.close(fd)
    return None
  except BaseException:
    os.close(fd)
    raise
  return CodexSessionLock(fd)
