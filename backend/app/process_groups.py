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


# Every agent run's environment carries a unique run marker (RUN_MARKER_ENV,
# derived from its run token), and every command the agent starts inherits it. Providers start each tool command in its own
# session, so a command is NOT in the agent's process group: when the agent
# process dies abruptly (an out-of-memory kill, a crash) its commands are
# re-parented to container init and keep running. The inherited marker is the
# one ownership record that survives that, so it names exactly this run's
# processes and never another chat's or a later run's.
RUN_TOKEN_ENV = "MOBIUS_RUN_TOKEN"
# The non-secret twin every agent run carries, delegated helpers included (they
# never receive the run token). A shared helper host names each helper turn's
# commands with it, and a provider may persist it in session history, so it
# must reveal nothing: it is a one-way digest of the run token.
RUN_MARKER_ENV = "MOBIUS_RUN_MARKER"


def run_marker(run_token: str | None) -> str:
  """The public process marker for one run token ("" without a token)."""
  if not run_token:
    return ""
  import hashlib
  return hashlib.sha256(f"mobius-run-marker:{run_token}".encode()).hexdigest()[:32]


def _start_ticks(pid: int) -> int:
  """Process start time, which tells one incarnation of a PID from its reuse."""
  with open(f"/proc/{pid}/stat", "rb") as handle:
    fields = handle.read().rsplit(b") ", 1)[1].split()
  if fields[0] == b"Z":
    raise ProcessLookupError(pid)
  return int(fields[19])


def run_owned_processes(marker: str) -> list[tuple[int, int]]:
  """``(pid, start_ticks)`` of live processes carrying one run marker.

  Unreadable processes are not ours: another user's environment is private,
  and a process that hides its own (non-dumpable) was not started by a shell
  command. Missing processes are ordinary scan races.
  """
  if not marker:
    return []
  # The chat's browser has its own graceful owner (turn teardown closes it so
  # the profile flushes before anything is killed); never pre-empt it here.
  from app.browser_processes import BROWSERS, DAEMONS
  spared = BROWSERS | DAEMONS
  needle = f"{RUN_MARKER_ENV}={marker}".encode()
  own = os.getpid()
  owned: list[tuple[int, int]] = []
  try:
    entries = os.listdir("/proc")
  except OSError:
    return owned
  for entry in entries:
    if not entry.isdigit() or int(entry) == own:
      continue
    pid = int(entry)
    try:
      with open(f"/proc/{pid}/environ", "rb") as handle:
        if needle not in handle.read().split(b"\0"):
          continue
      with open(f"/proc/{pid}/cmdline", "rb") as handle:
        argv0 = handle.read().split(b"\0", 1)[0].decode("utf-8", "replace")
      if os.path.basename(argv0) in spared:
        continue
      owned.append((pid, _start_ticks(pid)))
    except (OSError, ProcessLookupError, ValueError, IndexError):
      continue
  return owned


def _signal_same_process(pid: int, ticks: int, sig: signal.Signals) -> bool:
  """Signal ``pid`` only if it is still the recorded incarnation."""
  fd = None
  try:
    if hasattr(os, "pidfd_open"):
      fd = os.pidfd_open(pid)
    if _start_ticks(pid) != ticks:
      return False
    if fd is not None:
      signal.pidfd_send_signal(fd, sig)
    else:
      os.kill(pid, sig)
    return True
  except (OSError, ProcessLookupError, ValueError, IndexError):
    return False
  finally:
    if fd is not None:
      os.close(fd)


def terminate_run_processes(
  marker: str | None,
  *,
  logger: logging.Logger,
  label: str,
  grace_seconds: float = 0.25,
) -> int:
  """TERM then KILL every process still carrying one ended run's marker."""
  if not marker:
    return 0
  owned = run_owned_processes(marker)
  if not owned:
    return 0
  signalled = [p for p in owned if _signal_same_process(*p, signal.SIGTERM)]
  survivors = signalled
  deadline = time.monotonic() + max(0.0, grace_seconds)
  while survivors and time.monotonic() < deadline:
    time.sleep(0.025)
    survivors = [p for p in survivors if _is_same_process(*p)]
  for pid, ticks in survivors:
    _signal_same_process(pid, ticks, signal.SIGKILL)
  if signalled:
    logger.info("%s: ended %d leftover run process(es)", label, len(signalled))
  return len(signalled)


def _is_same_process(pid: int, ticks: int) -> bool:
  try:
    return _start_ticks(pid) == ticks
  except (OSError, ProcessLookupError, ValueError, IndexError):
    return False


def terminate_agent_processes(
  pgid: int | None,
  *,
  run_marker: str | None,
  logger: logging.Logger,
  label: str,
  grace_seconds: float = 0.25,
) -> bool:
  """End everything one agent run started: its group, then its commands.

  The group holds the agent and its direct helpers; commands in their own
  sessions are found by the run marker they inherited (see RUN_MARKER_ENV).
  """
  group_found = terminate_process_group(
    pgid, logger=logger, label=label, grace_seconds=grace_seconds,
  )
  leftovers = terminate_run_processes(
    run_marker, logger=logger, label=label, grace_seconds=grace_seconds,
  )
  return group_found or leftovers > 0
