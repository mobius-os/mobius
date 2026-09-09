"""Small, fail-closed helpers for isolated Unix process groups."""

from __future__ import annotations

import logging
import os
import signal
import time

BACKGROUND_PROCESS_NICE = 5
# Kernel oom_score_adj (max 1000). The container's own value is inherited by
# every process, so the kernel otherwise kills whichever process is largest —
# usually the server, taking every chat down with it (2026-09-03 incident).
# Marking agent groups as the preferred victim makes an out-of-memory event
# cost the one chat that grew, which then shows an ordinary resumable error.
AGENT_OOM_SCORE_ADJ = 1000


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


def lower_process_group_priority(
  pgid: int | None,
  *,
  logger: logging.Logger,
  label: str,
) -> bool:
  """Give one proven-private process group background priority.

  Background means two things the kernel decides separately: CPU (nice) and
  which process dies first when the container runs out of memory
  (``oom_score_adj``). Setting the group leader before it creates most
  descendants also makes both values the inherited default for later
  children. Failure is intentionally non-fatal: isolation and cleanup remain
  useful even on a runtime without ``setpriority`` or ``/proc`` support.
  """
  if not isinstance(pgid, int) or isolated_process_group_id(pgid) != pgid:
    return False
  try:
    os.setpriority(
      os.PRIO_PGRP,
      pgid,
      BACKGROUND_PROCESS_NICE,
    )
  except (AttributeError, OSError) as exc:
    logger.warning(
      "%s priority adjustment failed pgid=%s: %s",
      label,
      pgid,
      exc,
    )
    return False
  for pid in _process_group_members(pgid):
    try:
      with open(f"/proc/{pid}/oom_score_adj", "w") as handle:
        handle.write(str(AGENT_OOM_SCORE_ADJ))
    except OSError as exc:
      logger.warning(
        "%s OOM preference failed pid=%s pgid=%s: %s", label, pid, pgid, exc,
      )
  return True


def _process_group_members(pgid: int) -> list[int]:
  """Current members of ``pgid``; the leader alone when /proc is unavailable."""
  members: list[int] = []
  try:
    entries = os.listdir("/proc")
  except OSError:
    return [pgid]
  for entry in entries:
    if not entry.isdigit():
      continue
    pid = int(entry)
    try:
      if os.getpgid(pid) == pgid:
        members.append(pid)
    except (OSError, ProcessLookupError):
      continue
  return members or [pgid]


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
