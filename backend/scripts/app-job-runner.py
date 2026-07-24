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
import urllib.request
import uuid
from pathlib import Path


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
TOKEN_FILE = DATA_DIR / "service-token.txt"
MOBIUS_UID = 1000
MOBIUS_GID = 1000

# Cron discards this supervisor's stdout, so every FAILURE must leave
# a durable line — a silent early exit (bad path, dead token, missing
# job-context) is otherwise indistinguishable from a job that never
# fired. Successes stay silent: every-minute jobs would bury the
# failures under thousands of ok-lines a day. Self-rotates because
# cron never restarts the container for us.
SUPERVISOR_LOG = DATA_DIR / "cron-logs" / "app-jobs.log"
SUPERVISOR_LOG_CAP = 2 * 1024 * 1024


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


def _sandboxed_command(
  app_id: int, resolved: Path, context: dict,
) -> list[str] | None:
  """Confine declared background agents away from owner/platform state.

  Legacy ordinary app jobs retain their historical process authority. A
  manifest that explicitly requests ``background_agent`` gets the narrower
  contract advertised by the Store: source (read-only), own numeric storage,
  declared shared-memory access, and configured provider auth only.
  """
  contract = context.get("capability_contract")
  background = contract.get("background") if isinstance(contract, dict) else None
  if not isinstance(background, dict) or background.get("agent") is not True:
    return ["bash", str(resolved), str(app_id)]
  bwrap = shutil.which("bwrap")
  if not bwrap:
    return None
  if os.geteuid() == 0:
    setpriv = shutil.which("setpriv")
    if not setpriv:
      return None
    privilege_prefix = [
      setpriv,
      "--reuid", str(MOBIUS_UID), "--regid", str(MOBIUS_GID),
      "--clear-groups",
    ]
  elif os.geteuid() == MOBIUS_UID and os.getegid() == MOBIUS_GID:
    # Run-now is launched by uvicorn, which already runs as mobius. Repeating
    # setpriv --clear-groups without root authority fails even though no drop is
    # needed; launch the same unprivileged Bubblewrap boundary directly.
    privilege_prefix = []
  else:
    return None
  storage = DATA_DIR / "apps" / str(app_id)
  storage.mkdir(parents=True, exist_ok=True)
  command = [
    # The supervisor is root, but durable app/shared state must be written by
    # the same user that owns /data and runs pm-commit. Drop privileges before
    # Bubblewrap: its --uid/--gid mode requires an explicit user namespace,
    # which cannot mount /proc in our nested production container. Starting
    # bwrap as mobius lets it create the supported unprivileged namespace and
    # prevents root-owned mode-0600 Memory traces at the source.
    *privilege_prefix,
    bwrap,
    "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--ro-bind", "/", "/",
    "--tmpfs", str(DATA_DIR),
    "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run",
    "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    "--dir", str(DATA_DIR / "apps"),
    "--ro-bind", str(resolved.parent), str(resolved.parent),
    "--bind", str(storage), str(storage),
  ]
  data = contract.get("data") if isinstance(contract.get("data"), dict) else {}
  shared_level = data.get("shared_memory", "none")
  if shared_level in ("read", "write"):
    shared = DATA_DIR / "shared" / "memory"
    if shared_level == "write":
      shared.mkdir(parents=True, exist_ok=True)
    if shared.is_dir() and not shared.is_symlink():
      command += ["--dir", str(DATA_DIR / "shared")]
      command += [
        "--bind" if shared_level == "write" else "--ro-bind",
        str(shared), str(shared),
      ]
  # The owner-reviewed background-agent capability grants access to connected
  # provider credentials, while the app's own settings may select a provider
  # at runtime (Memory is one such app). job-context deliberately excludes app
  # storage settings, so restricting mounts to the system primary/fallback
  # silently breaks a valid app-level override. Mount every supported provider
  # directory that actually exists; the masked /data tree still exposes no
  # other owner/platform state and ordinary app jobs never take this path.
  auth_root = DATA_DIR / "cli-auth"
  auth_mounts = []
  for provider in ("claude", "codex"):
    auth = auth_root / provider
    if auth.is_dir() and not auth.is_symlink():
      auth_mounts.append(auth)
  if auth_mounts:
    command += ["--dir", str(auth_root)]
    for auth in auth_mounts:
      command += ["--bind", str(auth), str(auth)]
  command += [
    "--chdir", str(resolved.parent),
    "bash", str(resolved), str(app_id),
  ]
  return command


def run() -> int:
  if len(sys.argv) != 3 or not re.fullmatch(r"[0-9]+", sys.argv[1]):
    _log(sys.argv[1] if len(sys.argv) > 1 else "?", "rejected: bad argv")
    return 2
  app_id = int(sys.argv[1])
  job = Path(sys.argv[2])
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
  _atomic_json(lease, {
    "schema": 1,
    "app_id": app_id,
    "pid": pid,
    "start_ticks": _start_ticks(pid),
    "job": str(resolved),
  })
  try:
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
    command = _sandboxed_command(app_id, resolved, context)
    if command is None:
      _log(app_id, "failed: sandbox unavailable for background agent")
      return 5
    child_env = _job_env(app_token)
    job_state = DATA_DIR / "apps" / str(app_id) / "job-state"
    job_state.mkdir(parents=True, exist_ok=True)
    child_env["APP_JOB_STATE_DIR"] = str(job_state)
    if isinstance(context.get("capability_contract"), dict):
      background = context["capability_contract"].get("background")
      if isinstance(background, dict) and background.get("agent") is True:
        # /tmp is the namespace's writable tmpfs. /tmp/home is created by bwrap
        # as root and is not writable after the deliberate uid drop above.
        child_env["HOME"] = "/tmp"
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
