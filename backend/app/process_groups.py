"""Small, fail-closed helpers for isolated Unix process groups."""

from __future__ import annotations

import logging
import os
import signal
import time


def isolated_process_group_id(pid: object) -> int | None:
  """Return ``pid`` only when it provably leads a private process group."""
  if not isinstance(pid, int) or pid <= 1:
    return None
  try:
    pgid = os.getpgid(pid)
  except (OSError, ProcessLookupError):
    return None
  if pgid != pid or pgid == os.getpgrp():
    return None
  return pgid


def terminate_process_group(
  pgid: int | None,
  *,
  logger: logging.Logger,
  label: str,
  grace_seconds: float = 0.25,
) -> bool:
  """TERM then KILL one already-verified private process group."""
  if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
    return False
  try:
    os.killpg(pgid, signal.SIGTERM)
  except ProcessLookupError:
    return False
  except OSError as exc:
    logger.warning("%s SIGTERM failed pgid=%s: %s", label, pgid, exc)
    return False

  deadline = time.monotonic() + max(0.0, grace_seconds)
  while time.monotonic() < deadline:
    try:
      os.killpg(pgid, 0)
    except ProcessLookupError:
      return True
    except OSError:
      break
    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))

  try:
    os.killpg(pgid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  except OSError as exc:
    logger.warning("%s SIGKILL failed pgid=%s: %s", label, pgid, exc)
  return True
