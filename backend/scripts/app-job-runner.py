#!/usr/bin/env python3
"""Run one app job under a revocable process-group lease."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
  sys.path.insert(0, str(_SCRIPT_DIR))
from app_job_sandbox import JobAccess, select_executor


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
TOKEN_FILE = DATA_DIR / "service-token.txt"
CURRENT_CAPABILITY_CONTRACT_SCHEMA = 3
SUPPORTED_CAPABILITY_CONTRACT_SCHEMAS = frozenset({1, 2, 3})
PLATFORM_JOB_AUTHORITY = "platform"
SCOPED_JOB_AUTHORITY = "scoped"
LEGACY_PLATFORM_JOB_AUTHORITY = "app_job_process"
LEGACY_SCOPED_JOB_AUTHORITY = "scoped_system_job"

# Cron discards this supervisor's stdout, so every FAILURE must leave
# a durable line — a silent early exit (bad path, dead token, missing
# job-context) is otherwise indistinguishable from a job that never
# fired. Successes stay silent: every-minute jobs would bury the
# failures under thousands of ok-lines a day. Self-rotates because
# cron never restarts the container for us.
SUPERVISOR_LOG = DATA_DIR / "cron-logs" / "app-jobs.log"
SUPERVISOR_LOG_CAP = 2 * 1024 * 1024
READY_WAIT_SECONDS = 90


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


def _job_access(
  app_id: int, resolved: Path, context: dict,
) -> JobAccess:
  """Resolve the reviewed data contract once for every sandbox backend."""
  contract = context["capability_contract"]
  storage = DATA_DIR / "apps" / str(app_id)
  storage.mkdir(parents=True, exist_ok=True)
  read_only: list[Path] = []
  read_write: list[Path] = []
  data = contract.get("data") if isinstance(contract.get("data"), dict) else {}
  shared_level = data.get("shared_memory", "none")
  if shared_level in ("read", "write"):
    shared = DATA_DIR / "shared" / "memory"
    if shared_level == "write":
      shared.mkdir(parents=True, exist_ok=True)
    if shared.is_dir() and not shared.is_symlink():
      (read_write if shared_level == "write" else read_only).append(shared)
  # Scoped authority grants access to connected provider credentials. The
  # app's own settings may select a provider at runtime (Memory is one such
  # app). The job context deliberately excludes app storage settings, so
  # restricting mounts to the system primary/fallback
  # silently breaks a valid app-level override. Mount every supported provider
  # directory that actually exists; the masked /data tree still exposes no
  # other owner/platform state and ordinary app jobs never take this path.
  auth_root = DATA_DIR / "cli-auth"
  for provider in ("claude", "codex"):
    auth = auth_root / provider
    if auth.is_dir() and not auth.is_symlink():
      read_write.append(auth)
  return JobAccess(
    source_read=resolved.parent,
    storage_write=storage,
    extra_read=tuple(read_only),
    extra_write=tuple(read_write),
  )


def _job_authority(context: dict) -> str | None:
  """Resolve trusted job authority without weakening modern receipts.

  A null contract is a legitimate pre-contract platform-authority state.
  Schemas 1 and 2 retain the old boolean/authority pair; schema 3 carries the
  manifest's explicit authority directly. Reject contradictory, incomplete,
  or unknown receipts instead of silently granting platform process authority.
  """
  if "capability_contract" not in context:
    return None
  contract = context["capability_contract"]
  if contract is None:
    return PLATFORM_JOB_AUTHORITY
  if not isinstance(contract, dict):
    return None

  schema = contract.get("schema")
  if (
    type(schema) is not int
    or schema not in SUPPORTED_CAPABILITY_CONTRACT_SCHEMAS
    or "background" not in contract
  ):
    return None
  background = contract["background"]
  if background is None:
    return PLATFORM_JOB_AUTHORITY
  if not isinstance(background, dict):
    return None

  authority = background.get("authority")
  if schema == CURRENT_CAPABILITY_CONTRACT_SCHEMA:
    if "agent" in background:
      return None
    if authority in (PLATFORM_JOB_AUTHORITY, SCOPED_JOB_AUTHORITY):
      return authority
    return None

  agent = background.get("agent")
  if agent is True and authority == LEGACY_SCOPED_JOB_AUTHORITY:
    return SCOPED_JOB_AUTHORITY
  if agent is False and authority == LEGACY_PLATFORM_JOB_AUTHORITY:
    return PLATFORM_JOB_AUTHORITY
  return None


def run() -> int:
  argv = sys.argv[1:]
  wait_for_ready = argv[:1] == ["--wait-for-ready"]
  if wait_for_ready:
    argv = argv[1:]
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
  sandbox_home: Path | None = None
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
    executor = "process"
    authority = _job_authority(context)
    if authority is None:
      _log(app_id, "failed: invalid capability contract for app job")
      return 4
    if authority == SCOPED_JOB_AUTHORITY:
      sandbox_home = Path(tempfile.mkdtemp(prefix=f"mobius-job-{app_id}-"))
      if os.geteuid() == 0:
        os.chown(sandbox_home, 1000, 1000)
      launch, probes = select_executor(
        _job_access(app_id, resolved, context),
        command,
        child_env,
        sandbox_home,
      )
      if launch is None:
        reasons = "; ".join(
          f"{probe.executor}: {probe.detail}" for probe in probes
        )
        _log(
          app_id,
          f"failed: no supported secure background-job executor ({reasons})",
        )
        return 5
      command = launch.command
      child_env = launch.env
      executor = launch.executor
      if executor == "landlock":
        rejected = next(
          probe.detail for probe in probes
          if probe.executor == "bubblewrap"
        )
        _log(
          app_id,
          f"sandbox: selected landlock; bubblewrap unavailable ({rejected})",
        )
    lease_value["executor"] = executor
    _atomic_json(lease, lease_value)
    child = subprocess.Popen(
      command,
      cwd=str(resolved.parent),
      env=child_env,
    )
    rc = child.wait()
    if rc != 0:
      _log(app_id, f"job exited rc={rc}: {resolved}")
    return rc
  finally:
    if sandbox_home is not None:
      shutil.rmtree(sandbox_home, ignore_errors=True)
    lease.unlink(missing_ok=True)
    try:
      lease.parent.rmdir()
    except OSError:
      pass


if __name__ == "__main__":
  try:
    raise SystemExit(run())
  except SystemExit:
    raise
  except BaseException as exc:
    # A crash in the supervisor itself (lease publication, /proc read,
    # Popen) must not die silently under cron. SIGTERM is deliberately
    # not trapped: a handler would buy one log line at the cost of
    # masking the kill semantics uninstall/shutdown rely on.
    _log(sys.argv[1] if len(sys.argv) > 1 else "?", f"crashed: {exc!r}")
    raise
