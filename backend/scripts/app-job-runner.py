#!/usr/bin/env python3
"""Run one app job under a revocable process-group lease."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
  sys.path.insert(0, str(_BACKEND_DIR))
from app import cron_tz


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
TOKEN_FILE = DATA_DIR / "service-token.txt"

# Cron discards this supervisor's stdout, so every FAILURE must leave
# a durable line — a silent early exit (bad path, dead token, missing
# job-context) is otherwise indistinguishable from a job that never
# fired. Successes stay silent: every-minute jobs would bury the
# failures under thousands of ok-lines a day. Self-rotates because
# cron never restarts the container for us.
SUPERVISOR_LOG = DATA_DIR / "cron-logs" / "app-jobs.log"
SUPERVISOR_LOG_CAP = 2 * 1024 * 1024
READY_WAIT_SECONDS = 90
WALL_CLOCK_STATE_DIR = DATA_DIR / "run" / "app-wall-clock"


def _log(app_id: object, message: str) -> None:
  from datetime import datetime, timezone
  stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
  try:
    SUPERVISOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
      if SUPERVISOR_LOG.stat().st_size > SUPERVISOR_LOG_CAP:
        SUPERVISOR_LOG.replace(SUPERVISOR_LOG.with_suffix(".log.1"))
    except OSError:
      pass
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as handle:
      handle.write(f"[{stamp}] app={app_id} {message}\n")
  except OSError:
    pass


def _start_ticks(pid: int) -> int:
  tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
  return int(tail[19])


def _wait_for_ready(timeout_seconds: int = READY_WAIT_SECONDS) -> bool:
  """Wait only for the platform startup dependency bootstrap jobs require.

  A bootstrap install runs during FastAPI lifespan, while the app-job runner
  needs the backend to mint a scoped token and return job context.  `/api/ready`
  is the platform's existing readiness contract; polling it here avoids a
  startup ordering race without adding a second scheduler or retry system.
  """
  deadline = time.monotonic() + max(0, timeout_seconds)
  while True:
    try:
      request = urllib.request.Request(f"{API_BASE_URL}/api/ready")
      with urllib.request.urlopen(request, timeout=2) as response:
        if response.status == 200:
          return True
    except Exception:
      pass
    remaining = deadline - time.monotonic()
    if remaining <= 0:
      return False
    time.sleep(min(1, remaining))


def _atomic_json(path: Path, value: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lease-", suffix=".tmp")
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(value, handle, sort_keys=True)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def _acquire_app_run_lock(app_id: int):
  """Take the one nonblocking execution slot owned by ``app_id``.

  Cron and the run-now endpoint both enter through this process, so this lock
  prevents a slow job from multiplying at the next schedule tick. The empty
  lock file stays in ``/data/run`` after release: unlinking it could let a new
  process lock a different inode while another process still holds this one.
  Closing the returned handle releases the lock.
  """
  lock_dir = DATA_DIR / "run" / "app-job-locks"
  lock_dir.mkdir(parents=True, exist_ok=True)
  handle = (lock_dir / f"{app_id}.lock").open("a", encoding="utf-8")
  try:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    handle.close()
    return None
  return handle


def _claim_wall_clock_run(
  app_id: int,
  job: Path,
  tz_name: str,
  zone_cron: str,
) -> bool:
  """Atomically claim today's due wall-clock occurrence.

  cron invokes this gate every minute. The durable state prevents the repeated
  hour at a fall-back transition (and concurrent cron processes) from
  launching the same app schedule twice.
  """
  due_date = cron_tz.due_wall_clock_date(zone_cron, tz_name)
  if due_date is None:
    return False
  WALL_CLOCK_STATE_DIR.mkdir(parents=True, exist_ok=True)
  lock_path = WALL_CLOCK_STATE_DIR / f"{app_id}.lock"
  state_path = WALL_CLOCK_STATE_DIR / f"{app_id}.json"
  identity = {
    "schema": 1,
    "app_id": app_id,
    "job": str(job),
    "timezone": tz_name,
    "zone_cron": zone_cron,
    "local_date": due_date.isoformat(),
  }
  with lock_path.open("a", encoding="utf-8") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
      previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
      previous = None
    if previous == identity:
      return False
    _atomic_json(state_path, identity)
    return True


def _app_is_live(app_id: int, token: str | None = None) -> bool:
  token = (token or os.environ.get("APP_TOKEN", "")).strip()
  if not token:
    return False
  try:
    request = urllib.request.Request(
      f"{API_BASE_URL}/api/apps/{app_id}",
      headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
      return response.status == 200
  except Exception:
    return False


def _job_context(app_id: int, token: str) -> dict | None:
  try:
    request = urllib.request.Request(
      f"{API_BASE_URL}/api/apps/{app_id}/job-context",
      headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
      value = json.load(response)
    return value if isinstance(value, dict) else None
  except Exception:
    return None


def _job_matches_context(resolved: Path, context: dict) -> bool:
  """Bind a scheduled script to the exact app whose authority it receives.

  Missing, malformed, or mismatched identity fails closed before the app token
  reaches a child process. During a platform-update window, an older backend
  may omit ``source_dir``; skipping that run is safer than retaining a
  permanent compatibility path around this capability boundary.
  """
  source_dir = context.get("source_dir")
  if not isinstance(source_dir, str) or not source_dir:
    return False
  try:
    expected = Path(source_dir).resolve(strict=True)
  except (OSError, RuntimeError):
    return False
  return expected == resolved.parent


def _mint_app_token(app_id: int) -> str | None:
  """Exchange the owner service credential for one short-lived app token."""
  try:
    owner_token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    body = json.dumps({"app_id": app_id}).encode("utf-8")
    request = urllib.request.Request(
      f"{API_BASE_URL}/api/auth/app-job-token",
      data=body,
      method="POST",
      headers={
        "Authorization": f"Bearer {owner_token}",
        "Content-Type": "application/json",
      },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
      value = json.load(response)
    token = value.get("token") if isinstance(value, dict) else None
    return token.strip() if isinstance(token, str) and token.strip() else None
  except Exception:
    return None


def _job_env(app_token: str) -> dict[str, str]:
  """Allowlist job environment; never inherit owner/service credentials."""
  allowed = {
    "PATH", "LANG", "LC_ALL", "TZ", "HOME",
    "DATA_DIR", "API_BASE_URL", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
  }
  env = {
    key: value for key, value in os.environ.items()
    if key in allowed
  }
  env["DATA_DIR"] = str(DATA_DIR)
  env["API_BASE_URL"] = API_BASE_URL
  env["APP_TOKEN"] = app_token
  return env


def _execute_job(
  app_id: int,
  resolved: Path,
  *,
  wait_for_ready: bool,
  run_lock_fd: int,
) -> int:
  """Execute one already-validated job."""
  # API launches already create a session; cron launches do not.
  try:
    if os.getsid(0) != os.getpid():
      os.setsid()
  except OSError:
    _log(app_id, "failed: setsid")
    return 3
  pid = os.getpid()
  lease = (
    DATA_DIR / "run" / "app-jobs" / str(app_id) / f"{uuid.uuid4().hex}.json"
  )
  lease_value = {
    "schema": 1,
    "app_id": app_id,
    "pid": pid,
    "start_ticks": _start_ticks(pid),
    "job": str(resolved),
  }
  _atomic_json(lease, lease_value)
  try:
    if wait_for_ready and not _wait_for_ready():
      _log(app_id, "failed: timed out waiting for platform readiness")
      return 4
    app_token = _mint_app_token(app_id)
    if not app_token:
      _log(app_id, "failed: could not mint app token (backend down or bad service token)")
      return 4
    # Publication-before-check closes uninstall races: if uninstall already
    # won, this fails; if it follows, it sees and terminates this process group.
    if not _app_is_live(app_id, app_token):
      # _app_is_live folds timeouts and backend errors into False, so
      # this line covers outages too — don't read it as proof of
      # uninstall without checking the backend was up at this stamp.
      _log(app_id, "skipped: app not live (uninstalled/tombstoned) or backend unreachable")
      return 4
    context = _job_context(app_id, app_token)
    if context is None:
      _log(app_id, "failed: job-context fetch")
      return 4
    if not _job_matches_context(resolved, context):
      _log(app_id, f"rejected: job does not belong to app: {resolved}")
      return 4
    child_env = _job_env(app_token)
    job_state = DATA_DIR / "apps" / str(app_id) / "job-state"
    job_state.mkdir(parents=True, exist_ok=True)
    child_env["APP_JOB_STATE_DIR"] = str(job_state)
    command = ["bash", str(resolved), str(app_id)]
    # Uninstall sends TERM to this entire process group. Keep the supervisor
    # alive to retain its lease while a TERM-ignoring child needs the existing
    # KILL fallback; exec resets the child's caught handler to the default.
    previous_sigterm = signal.signal(signal.SIGTERM, lambda *_args: None)
    try:
      child = subprocess.Popen(
        command,
        cwd=str(resolved.parent),
        env=child_env,
        # If the supervisor crashes, the actual job keeps the single-flight
        # lock until it and any inheriting descendants exit.
        pass_fds=(run_lock_fd,),
      )
      rc = child.wait()
    finally:
      signal.signal(signal.SIGTERM, previous_sigterm)
    if rc != 0:
      _log(app_id, f"job exited rc={rc}: {resolved}")
    return rc
  finally:
    lease.unlink(missing_ok=True)
    try:
      lease.parent.rmdir()
    except OSError:
      pass


def run() -> int:
  argv = sys.argv[1:]
  wait_for_ready = argv[:1] == ["--wait-for-ready"]
  if wait_for_ready:
    argv = argv[1:]
  wall_clock = None
  if argv[:1] == ["--wall-clock"]:
    if len(argv) < 3:
      _log("?", "rejected: incomplete wall-clock argv")
      return 2
    wall_clock = (argv[1], argv[2])
    argv = argv[3:]
  if len(argv) != 2 or not re.fullmatch(r"[0-9]+", argv[0]):
    _log(argv[0] if argv else "?", "rejected: bad argv")
    return 2
  app_id = int(argv[0])
  job = Path(argv[1])
  if job.is_symlink():
    _log(app_id, f"rejected: symlinked job {job}")
    return 2
  try:
    apps_root = (DATA_DIR / "apps").resolve(strict=True)
    resolved = job.resolve(strict=True)
  except (OSError, RuntimeError):
    _log(app_id, f"rejected: unresolvable job {job}")
    return 2
  if (
    resolved.parent.parent != apps_root
    or not resolved.is_file()
  ):
    _log(app_id, f"rejected: job outside apps root {resolved}")
    return 2
  if wall_clock is not None:
    tz_name, zone_cron = wall_clock
    try:
      if not _claim_wall_clock_run(app_id, resolved, tz_name, zone_cron):
        return 0
    except (OSError, ValueError) as exc:
      _log(app_id, f"rejected: invalid wall-clock schedule ({exc})")
      return 2

  run_lock = _acquire_app_run_lock(app_id)
  if run_lock is None:
    _log(app_id, "skipped: another job for this app is still running")
    return 0
  try:
    return _execute_job(
      app_id,
      resolved,
      wait_for_ready=wait_for_ready,
      run_lock_fd=run_lock.fileno(),
    )
  finally:
    run_lock.close()


if __name__ == "__main__":
  try:
    raise SystemExit(run())
  except SystemExit:
    raise
  except BaseException as exc:
    # A crash in the supervisor itself (lease publication, /proc read,
    # Popen) must not die silently under cron. Outside the child-wait window,
    # SIGTERM keeps its default action; _execute_job ignores it only while it
    # must retain the lease through uninstall's TERM/KILL process-group grace.
    _log(sys.argv[1] if len(sys.argv) > 1 else "?", f"crashed: {exc!r}")
    raise
