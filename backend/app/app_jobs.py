"""Supervised lifecycle for manifest-declared app jobs.

Both cron and the run-now endpoint enter through ``app-job-runner.py``.  The
wrapper publishes a process-group lease before checking the live app row, so an
uninstall either sees and kills the group or wins first and makes the wrapper's
live check fail.  PID reuse is guarded by Linux ``/proc`` start ticks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from app.config import get_settings


def runner_script() -> Path:
  baked = Path("/app/scripts/app-job-runner.py")
  if baked.is_file():
    return baked
  return Path(__file__).resolve().parent.parent / "scripts" / "app-job-runner.py"


def runner_command(app_id: int, job_path: Path) -> list[str]:
  return [sys.executable, str(runner_script()), str(app_id), str(job_path)]


def launch_app_job(app_id: int, job_path: Path, source_dir: Path):
  """Launch the common wrapper detached from the API worker's pipes."""
  return subprocess.Popen(
    runner_command(app_id, job_path),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    cwd=str(source_dir),
    close_fds=True,
    start_new_session=True,
  )


def _start_ticks(pid: int) -> int | None:
  try:
    # comm may contain spaces and ')'; the fields after the final ')' start at
    # process state (field 3), making starttime (field 22) index 19 here.
    tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(tail[19])
  except (OSError, ValueError, IndexError):
    return None


def terminate_app_jobs(app_id: int, *, grace_seconds: float = 5.0) -> int:
  """TERM/KILL every verified process group leased by ``app_id``."""
  leases = Path(get_settings().data_dir) / "run" / "app-jobs" / str(app_id)
  if not leases.is_dir():
    return 0
  verified: list[tuple[int, int, Path]] = []
  signalled = 0
  for lease in leases.glob("*.json"):
    try:
      value = json.loads(lease.read_text(encoding="utf-8"))
      pid = int(value["pid"])
      ticks = int(value["start_ticks"])
    except (OSError, ValueError, TypeError, KeyError):
      lease.unlink(missing_ok=True)
      continue
    if _start_ticks(pid) != ticks:
      lease.unlink(missing_ok=True)
      continue
    verified.append((pid, ticks, lease))
    try:
      os.killpg(pid, signal.SIGTERM)
      signalled += 1
    except ProcessLookupError:
      lease.unlink(missing_ok=True)
    except PermissionError:
      continue

  deadline = time.monotonic() + max(0.0, grace_seconds)
  while verified and time.monotonic() < deadline:
    still_running = []
    for pid, ticks, lease in verified:
      if lease.exists() and _start_ticks(pid) == ticks:
        still_running.append((pid, ticks, lease))
      else:
        lease.unlink(missing_ok=True)
    verified = still_running
    if verified:
      time.sleep(0.05)
  for pid, ticks, lease in verified:
    if _start_ticks(pid) != ticks:
      lease.unlink(missing_ok=True)
      continue
    try:
      os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
      pass
    lease.unlink(missing_ok=True)
  try:
    leases.rmdir()
  except OSError:
    pass
  return signalled
