"""Owner-managed outbound Connect runners for this Möbius instance.

The Connect UI accepts the exact one-line pairing command produced by another
Möbius instance.  This module parses that command as data (never through a
shell), downloads that instance's runner, and starts it in an isolated HOME.
Each saved profile is independently revocable and contains its bearer only in
the runner's own mode-0600 config file.

Runner processes deliberately outlive a backend restart.  Their durable
profile record is the lifecycle owner: every ``_RECONCILE_SECONDS`` the
supervisor relaunches any ``active`` profile whose runner it neither owns nor
finds alive, while a runner that exits on its own is recorded as ``ended``
(exit 0) or ``error`` and is never restarted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import select
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from app import auth, connect_runner, models
from app.config import get_settings
from app.database import SessionLocal
from app.storage_io import atomic_write


log = logging.getLogger(__name__)

_MAX_RUNNER_BYTES = 2 * 1024 * 1024
_PAIR_TIMEOUT_SECONDS = 35
_RECONCILE_SECONDS = 5
_ID_RE = re.compile(r"^o_[a-f0-9]{16}$")
_COMMAND_RE = re.compile(
  r"^\s*curl\s+-fsSL\s+"
  r"(?P<base>[^\s'\"]+)/api/connect/i/"
  r"(?P<code>[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}-"
  r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4})"
  r"\s*\|\s*sh\s*$",
)
_lock = threading.RLock()
_owned_processes: dict[str, subprocess.Popen] = {}


class OutboundConnectError(RuntimeError):
  """A safe, owner-facing outbound Connect failure."""


def _now() -> float:
  return time.time()


def _profiles_dir() -> Path:
  root = Path(get_settings().data_dir) / "shared" / "connect" / "outbound"
  root.mkdir(parents=True, exist_ok=True)
  try:
    root.chmod(0o700)
  except OSError:
    pass
  return root


def _profile_dir(profile_id: str) -> Path:
  if not _ID_RE.fullmatch(profile_id or ""):
    raise LookupError(profile_id)
  return _profiles_dir() / profile_id


def _meta_path(profile_id: str) -> Path:
  return _profile_dir(profile_id) / "profile.json"


def _pid_path(profile_id: str) -> Path:
  return _profile_dir(profile_id) / "runner.pid"


def _runner_path(profile_id: str) -> Path:
  return _profile_dir(profile_id) / "home" / ".mobius-connect" / "runner.py"


def _runner_config_path(profile_id: str) -> Path:
  return _profile_dir(profile_id) / "home" / ".mobius-connect" / "config.json"


def _atomic_json(path: Path, value: dict) -> None:
  atomic_write(path, json.dumps(value, indent=2), mode=0o600)


def _read_json(path: Path) -> dict | None:
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  return value if isinstance(value, dict) else None


def parse_pairing_command(command: str) -> tuple[str, str]:
  """Extract a trusted origin and code from Connect's exact copyable line."""
  match = _COMMAND_RE.fullmatch(str(command or ""))
  if match is None:
    raise OutboundConnectError(
      "Paste the complete command from the other person’s Connect app.",
    )
  try:
    base_url = connect_runner._validated_base_url(match.group("base"))
  except ValueError as exc:
    raise OutboundConnectError(str(exc)) from exc
  return base_url, match.group("code")


def _download_runner(base_url: str) -> bytes:
  request = urllib.request.Request(
    base_url + "/api/connect/runner", headers={"Accept": "text/x-python"},
  )
  try:
    with connect_runner._open_url(
      request, timeout=30, context=ssl.create_default_context(),
    ) as response:
      source = response.read(_MAX_RUNNER_BYTES + 1)
  except (OSError, ValueError, urllib.error.URLError) as exc:
    raise OutboundConnectError(
      "Couldn’t reach the other person’s Möbius.",
    ) from exc
  if len(source) > _MAX_RUNNER_BYTES:
    raise OutboundConnectError("That Connect runner is unexpectedly large.")
  if not source.startswith(b"#!/usr/bin/env python3") or b"Mobius Connect runner" not in source[:4096]:
    raise OutboundConnectError("That address did not return a Connect runner.")
  return source


# The outbound runner executes commands the REMOTE instance sends us. It must
# not inherit this backend's environment: SECRET_KEY, SSO secrets and DB
# credentials would otherwise be readable by the far side, letting it mint our
# tokens and impersonate this instance. Pass only a minimal, non-secret env --
# the same allowlist convention service_environment uses.
_RUNNER_ENV_ALLOWLIST = (
  "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def _runner_env(profile_id: str) -> dict[str, str]:
  home = _profile_dir(profile_id) / "home"
  home.mkdir(parents=True, exist_ok=True)
  try:
    home.chmod(0o700)
  except OSError:
    pass
  env = {k: os.environ[k] for k in _RUNNER_ENV_ALLOWLIST if k in os.environ}
  env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
  env["HOME"] = str(home)
  env["XDG_CONFIG_HOME"] = str(home / ".config")
  # Tell the far side this runner is supervised here, so its Connect does not
  # offer an install command that would add a second, unsupervised service.
  env[connect_runner.SUPERVISOR_ENV] = "mobius"
  meta = _read_json(_meta_path(profile_id)) or {}
  if meta.get("agent") is True:
    env.update(_agent_env(profile_id))
  return env


# Owner-approved agent access through one outbound connection: commands that
# arrive through it get an agent bearer, so ``mapi`` drives this instance's
# API like an in-product agent. It is an agent principal (never owner input),
# and every request re-checks that this connection still exists with agent
# access on, so revoking either ends it at once.
AGENT_CLAIM = "connect_agent"
_AGENT_BEARER_LIFETIME = 3650  # days; revocation is the per-request check


def _agent_env(profile_id: str) -> dict[str, str]:
  with SessionLocal() as db:
    owner = db.query(models.Owner).first()
  if owner is None:
    return {}
  token = auth.create_access_token(
    {
      "sub": owner.username,
      "agent_chat": f"connect-agent-{profile_id}",
      AGENT_CLAIM: profile_id,
    },
    expires_delta=timedelta(days=_AGENT_BEARER_LIFETIME),
    token_epoch=owner.token_epoch,
  )
  return {"AGENT_TOKEN": token, "API_BASE_URL": get_settings().api_base_url}


def agent_access_active(profile_id: str) -> bool:
  if not _ID_RE.fullmatch(str(profile_id)):
    return False
  meta = _read_json(_meta_path(profile_id))
  # Agent access lives exactly as long as the connection it rides on.
  return bool(meta) and meta.get("agent") is True and meta.get("status") in {
    "connecting", "active",
  }


def _set_agent_access(profile_id: str, enabled: bool) -> dict:
  with _lock:
    meta = _read_json(_meta_path(profile_id))
    if meta is None:
      raise LookupError(profile_id)
    meta["agent"] = enabled
    _atomic_json(_meta_path(profile_id), meta)
    # Relaunch so the runner's environment gains or loses the bearer.
    if meta.get("status") == "active":
      _stop_process_tree(profile_id)
      _owned_processes.pop(profile_id, None)
      _launch(profile_id)
    return _public_profile(meta)


async def set_agent_access(profile_id: str, enabled: bool) -> dict:
  return await asyncio.to_thread(_set_agent_access, profile_id, enabled)


def _saved_profiles() -> list[tuple[str, dict]]:
  profiles = []
  for path in sorted(_profiles_dir().glob("o_*/profile.json")):
    meta = _read_json(path)
    profile_id = str((meta or {}).get("id") or "")
    if meta and _ID_RE.fullmatch(profile_id):
      profiles.append((profile_id, meta))
  return profiles


def _needs_relaunch(profile_id: str, meta: dict) -> bool:
  """An active profile whose runner this backend neither owns nor finds alive."""
  return (
    meta.get("status") == "active"
    and profile_id not in _owned_processes
    and not _process_alive(profile_id)
  )


def _download_relaunch_runners() -> dict[str, bytes]:
  """Fetch the current runner for every supervised runner about to relaunch.

  Supervised runners have no service manager to update them, so each launch
  takes the release the connected Möbius serves now. This runs before the
  supervisor lock: an unreachable Möbius must not stall pairing or revocation.
  A profile whose Möbius cannot be reached relaunches its saved runner.
  """
  sources = {}
  for profile_id, meta in _saved_profiles():
    if not _needs_relaunch(profile_id, meta):
      continue
    try:
      sources[profile_id] = _download_runner(str(meta.get("base_url") or ""))
    except OutboundConnectError as exc:
      log.warning("keeping saved Connect runner for %s: %s", profile_id, exc)
  return sources


def _install_runner(profile_id: str, source: bytes) -> None:
  """Save the runner this profile launches, privately and atomically."""
  runner = _runner_path(profile_id)
  runner.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  if runner.is_file() and runner.read_bytes() == source:
    return
  atomic_write(runner, source, mode=0o700)


def _write_pid(profile_id: str, pid: int) -> None:
  atomic_write(_pid_path(profile_id), str(pid), mode=0o600)


def _read_pid(profile_id: str) -> int | None:
  try:
    return int(_pid_path(profile_id).read_text(encoding="ascii").strip())
  except (OSError, ValueError):
    return None


def _process_alive(profile_id: str) -> bool:
  pid = _read_pid(profile_id)
  if not pid or pid == os.getpid():
    return False
  try:
    os.kill(pid, 0)
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
  except (OSError, ProcessLookupError):
    return False
  return str(_runner_path(profile_id)).encode() in cmdline.split(b"\0")


def _launch(profile_id: str, *args: str) -> subprocess.Popen:
  runner = _runner_path(profile_id)
  if not runner.is_file():
    raise OutboundConnectError("The saved Connect runner is missing.")
  log_path = _profile_dir(profile_id) / "runner.log"
  with log_path.open("ab") as log_file:
    process = subprocess.Popen(
      [sys.executable, str(runner), *args],
      cwd=str(_profile_dir(profile_id)),
      env=_runner_env(profile_id),
      stdin=subprocess.DEVNULL,
      stdout=log_file,
      stderr=log_file,
      start_new_session=True,
    )
  _owned_processes[profile_id] = process
  try:
    _write_pid(profile_id, process.pid)
  except BaseException as persistence_error:
    # Popen and its durable identity form one launch transaction.  Callers
    # must never receive a failed launch while its runner still grants access.
    try:
      _stop_process_tree(profile_id)
    except BaseException:
      log.exception("outbound Connect launch cleanup failed for %s", profile_id)
    # A newly created, unreaped Popen still pins its PID, so this fallback
    # cannot signal a reused PID even if the broader tree cleanup failed.
    if process.poll() is None:
      try:
        process.kill()
        process.wait(timeout=3)
      except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
      raise OutboundConnectError(
        "The Connect runner started, but its process record could not be saved "
        "and the runner could not be stopped.",
      ) from persistence_error
    _owned_processes.pop(profile_id, None)
    try:
      _pid_path(profile_id).unlink(missing_ok=True)
    except OSError:
      pass
    raise
  return process


def _public_profile(meta: dict) -> dict:
  profile_id = str(meta.get("id") or "")
  owned = _owned_processes.get(profile_id)
  online = _ID_RE.fullmatch(profile_id) is not None and (
    (owned is not None and owned.poll() is None) or _process_alive(profile_id)
  )
  status = "active" if online else str(meta.get("status") or "ended")
  parsed = urllib.parse.urlsplit(str(meta.get("base_url") or ""))
  return {
    "id": profile_id,
    "label": meta.get("label") or "Shared access",
    "target": parsed.hostname or "Another Möbius",
    "status": status,
    "online": online,
    "agent": meta.get("agent") is True,
    "created_at": meta.get("created_at"),
  }


def list_profiles() -> list[dict]:
  with _lock:
    profiles = [_public_profile(meta) for _profile_id, meta in _saved_profiles()]
  return sorted(profiles, key=lambda item: float(item.get("created_at") or 0), reverse=True)


def _await_pairing(process: subprocess.Popen, profile_id: str) -> dict | None:
  """Return the runner's saved config once paired, or None if it gave up."""
  deadline = time.monotonic() + _PAIR_TIMEOUT_SECONDS
  while time.monotonic() < deadline:
    config = _read_json(_runner_config_path(profile_id))
    # A multi-connection runner saves a connections list; an older one saves a
    # single connection at the top level. Normalize both to the first entry.
    conns = connect_runner._connections(config) if config else []
    if conns and conns[0].get("token") and conns[0].get("host_id"):
      return conns[0]
    if process.poll() is not None:
      return None
    time.sleep(0.1)
  return None


def _discard(profile_id: str) -> None:
  _stop_process_tree(profile_id)
  _owned_processes.pop(profile_id, None)
  shutil.rmtree(_profile_dir(profile_id))


def _create_profile(label: str, command: str, agent: bool = False) -> dict:
  base_url, code = parse_pairing_command(command)
  source = _download_runner(base_url)
  profile_id = "o_" + secrets.token_hex(8)
  meta = {
    "id": profile_id,
    "label": label,
    "base_url": base_url,
    "created_at": _now(),
    "status": "connecting",
    "agent": agent,
  }
  with _lock:
    _profile_dir(profile_id).mkdir(parents=True, mode=0o700)
  try:
    with _lock:
      _install_runner(profile_id, source)
      _atomic_json(_meta_path(profile_id), meta)
      process = _launch(profile_id, "--pair", code, "--url", base_url)

    config = _await_pairing(process, profile_id)
    if config is None:
      raise OutboundConnectError(
        "The other Möbius did not accept that command. It may have expired or already been used.",
      )
    try:
      saved_base = connect_runner._validated_base_url(str(config.get("url") or ""))
    except ValueError:
      saved_base = ""
    if saved_base != base_url:
      raise OutboundConnectError("The Connect runner saved an unexpected address.")
    meta["status"] = "active"
    with _lock:
      _atomic_json(_meta_path(profile_id), meta)
    return _public_profile(meta)
  except Exception:
    with _lock:
      _discard(profile_id)
    raise


async def create_profile(label: str, command: str, agent: bool = False) -> dict:
  return await asyncio.to_thread(_create_profile, label, command, agent)


def _process_identity(pid: int) -> tuple[int, int] | None:
  """Return parent pid and kernel start time, which distinguishes PID reuse."""
  try:
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = text[text.rfind(")") + 2:].split()
    return int(fields[1]), int(fields[19])
  except (OSError, ValueError, IndexError):
    return None


def _descendant_processes(root_pid: int) -> dict[int, tuple[int, int]]:
  processes = {}
  for stat in Path("/proc").glob("[0-9]*/stat"):
    pid = int(stat.parent.name)
    identity = _process_identity(pid)
    if identity is not None:
      processes[pid] = identity
  descendants = {}
  frontier = [root_pid]
  while frontier:
    parent = frontier.pop()
    children = {pid: identity for pid, identity in processes.items() if identity[0] == parent}
    descendants.update(children)
    frontier.extend(children)
  return descendants


def _stop_process_tree(profile_id: str) -> None:
  # Remote commands start independent sessions. Pin every discovered process
  # with a pidfd so PID reuse, including after SIGTERM, cannot target a stranger.
  owned = _owned_processes.get(profile_id)
  pid = owned.pid if owned is not None else _read_pid(profile_id)
  if not pid or (owned is not None and owned.poll() is not None):
    return
  identity = _process_identity(pid)
  if identity is None or (owned is None and not _process_alive(profile_id)):
    return
  processes = _descendant_processes(pid)
  processes[pid] = identity
  handles = []
  root_handle = None
  try:
    for target, expected in processes.items():
      try:
        handle = os.pidfd_open(target)
      except ProcessLookupError:
        continue
      if _process_identity(target) != expected:
        os.close(handle)
        continue
      handles.append(handle)
      if target == pid:
        root_handle = handle
    if root_handle is None or _process_identity(pid) != identity:
      return
    for handle in handles:
      try:
        signal.pidfd_send_signal(handle, signal.SIGTERM)
      except ProcessLookupError:
        pass
    pending = set(handles)
    poller = select.poll()
    for handle in pending:
      poller.register(handle, select.POLLIN)
    deadline = time.monotonic() + 3
    while pending:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        break
      for handle, _event in poller.poll(max(1, int(remaining * 1000))):
        pending.discard(handle)
        poller.unregister(handle)
    for handle in pending:
      try:
        signal.pidfd_send_signal(handle, signal.SIGKILL)
      except ProcessLookupError:
        pass
    owned = _owned_processes.get(profile_id)
    if owned is not None:
      owned.wait(timeout=3)
  finally:
    for handle in handles:
      os.close(handle)


def _disconnect_remote(config: dict) -> None:
  try:
    base_url = connect_runner._validated_base_url(str(config.get("url") or ""))
  except ValueError as exc:
    raise OutboundConnectError(
      "The saved Connect address is invalid, so access was kept.",
    ) from exc
  token = str(config.get("token") or "")
  if not token:
    raise OutboundConnectError(
      "The saved Connect credential is missing, so access was kept.",
    )
  request = urllib.request.Request(
    base_url + "/api/connect/disconnect",
    data=b"{}",
    method="POST",
    headers={
      "Authorization": "Bearer " + token,
      "Content-Type": "application/json",
    },
  )
  try:
    with connect_runner._open_url(
      request, timeout=30, context=ssl.create_default_context(),
    ) as response:
      response.read(64 * 1024)
  except urllib.error.HTTPError as exc:
    if exc.code not in (401, 403, 404):
      raise OutboundConnectError(
        "Couldn’t confirm revocation with the other Möbius, so access was kept.",
      ) from exc
  except (OSError, ValueError, urllib.error.URLError) as exc:
    raise OutboundConnectError(
      "Couldn’t confirm revocation with the other Möbius, so access was kept.",
    ) from exc


def _notify_remote(profile_id: str) -> bool:
  """Tell the other Möbius its host is gone; whether it confirmed."""
  config = _read_json(_runner_config_path(profile_id))
  # An outbound profile owns exactly one remote connection. Ambiguous or
  # unreadable credentials are never sent to an arbitrary instance.
  connections = connect_runner._connections(config) if config else []
  if len(connections) != 1:
    return False
  try:
    _disconnect_remote(connections[0])
  except OutboundConnectError as exc:
    log.warning("outbound Connect %s: %s", profile_id, exc)
    return False
  return True


def _revoke_profile(profile_id: str) -> bool:
  """End this access here, then tell the other Möbius when it can be reached.

  Access is this Möbius's own runner, so stopping it and deleting its
  credentials is the revocation; it never waits on the other instance. The
  runner is stopped before the remote notice so nothing new can start while
  the notice is in flight. Returns whether the other Möbius confirmed.
  """
  with _lock:
    meta = _read_json(_meta_path(profile_id))
    if meta is None:
      raise LookupError(profile_id)
    already_revoked = meta.get("status") == "revoked"
    _stop_process_tree(profile_id)
    _owned_processes.pop(profile_id, None)
    # Supervision relaunches only "active" profiles; this keeps it from
    # restarting the runner while the remote notice is in flight.
    _atomic_json(_meta_path(profile_id), {**meta, "status": "revoking"})
  notified = already_revoked or _notify_remote(profile_id)
  with _lock:
    _discard(profile_id)
  return notified


async def revoke_profile(profile_id: str) -> bool:
  return await asyncio.to_thread(_revoke_profile, profile_id)


def _reconcile_once() -> None:
  fresh_runners = _download_relaunch_runners()
  with _lock:
    for profile_id, meta in _saved_profiles():
      owned = _owned_processes.get(profile_id)
      if owned is not None:
        result = owned.poll()
        if result is None:
          continue
        _owned_processes.pop(profile_id, None)
        # A remotely requested disconnect cleanly exits only after removing
        # all local runner artifacts.  Record that observed transition so a
        # later local removal does not need credentials that no longer exist.
        runner_uninstalled = result == 0 and all(
          not artifact.exists()
          for artifact in (
            _runner_config_path(profile_id),
            _runner_path(profile_id),
            _runner_path(profile_id).with_name("runner.pid"),
          )
        )
        if runner_uninstalled:
          meta["status"] = "revoked"
        else:
          meta["status"] = "ended" if result == 0 else "error"
        _atomic_json(_meta_path(profile_id), meta)
        continue
      # Re-checked under the lock: pairing or revocation may have changed
      # this profile while its runner was downloading.
      if not _needs_relaunch(profile_id, meta):
        continue
      source = fresh_runners.get(profile_id)
      if source is not None:
        try:
          _install_runner(profile_id, source)
        except OSError as exc:
          log.warning("keeping saved Connect runner for %s: %s", profile_id, exc)
      try:
        _launch(profile_id)
      except (OSError, OutboundConnectError):
        meta["status"] = "error"
        _atomic_json(_meta_path(profile_id), meta)


async def supervise_outbound_connects() -> None:
  while True:
    try:
      await asyncio.to_thread(_reconcile_once)
    except Exception as exc:
      log.error("outbound Connect reconciliation failed: %s", exc, exc_info=True)
    await asyncio.sleep(_RECONCILE_SECONDS)
