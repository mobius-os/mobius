#!/usr/bin/env python3
"""Immutable root repair target used only by an external recovery worker.

This module is deliberately stdlib-only and is launched by the baked entrypoint
before any path below /data is imported.  It is not a recovery user interface or
an agent runner: it is a small, bearer-authenticated capability endpoint for the
separate recovery service.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROTOCOL = "mobius-recovery-target/v1"
DEFAULT_PORT = 18002
BUILD_REVISION_PATH = Path("/app/recovery-target/BUILD_REVISION")
CAP_NET_ADMIN = 12
CAP_NET_RAW = 13
CAP_SYS_PTRACE = 19
CAP_SYS_ADMIN = 21
_BLOCKED_CAPABILITIES = (
  CAP_NET_ADMIN, CAP_NET_RAW, CAP_SYS_PTRACE, CAP_SYS_ADMIN,
)
_CAPABILITY_VERSION_3 = 0x20080522
_PR_CAPBSET_READ = 23
_PR_CAPBSET_DROP = 24
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_IS_SET = 1
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_SYS_OPENAT2 = 437
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_BENEATH = 0x08
_TOKEN_DIGEST_DOMAIN = b"mobius-recovery-target/v1 bearer\x00"
# An 8 MiB decoded payload expands to about 10.67 MiB as base64 before the JSON
# envelope is counted. Keep the wire budget large enough for the advertised
# file/stdin boundary while still rejecting unbounded request bodies.
MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_LIST_ENTRIES = 10_000
MAX_LIST_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 900.0
MAX_ENV_ITEMS = 128
MAX_ENV_BYTES = 256 * 1024
MAX_CONCURRENT_EXEC = 2
MAX_TARGET_LIFETIME_SECONDS = 24 * 60 * 60
_EXEC_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_EXEC)
_ACTIVE_SUPERVISORS: set[int] = set()
_ACTIVE_SUPERVISORS_LOCK = threading.Lock()
_STARTUP_TOKEN_DIGEST: bytes | None = None
_TARGET_EXPIRES_AT: int | None = None
_TARGET_EXPIRED = threading.Event()
_TARGET_REVOCATION_LOCK = threading.Lock()
_TARGET_SHUTDOWN_STARTED = False
_BUILD_REVISION = "unknown"
_DATA_ROOT = Path(os.environ.get("DATA_DIR", "/data"))
# Convenience file operations are deliberately narrower than root exec. They
# run inside target PID1 and therefore must never act as a confused deputy for
# /proc memory. Recovery data, baked app sources, and scratch paths are
# sufficient for inspection; only stopped-instance data and scratch are
# writable. openat2 enforces these roots against symlink and mount races.
_FS_READ_ROOTS = (_DATA_ROOT, Path("/app"), Path("/tmp"))
_FS_WRITE_ROOTS = (_DATA_ROOT, Path("/tmp"))


class _CapabilityHeader(ctypes.Structure):
  _fields_ = [
    ("version", ctypes.c_uint32),
    ("pid", ctypes.c_int),
  ]


class _CapabilityData(ctypes.Structure):
  _fields_ = [
    ("effective", ctypes.c_uint32),
    ("permitted", ctypes.c_uint32),
    ("inheritable", ctypes.c_uint32),
  ]


class _OpenHow(ctypes.Structure):
  _fields_ = [
    ("flags", ctypes.c_uint64),
    ("mode", ctypes.c_uint64),
    ("resolve", ctypes.c_uint64),
  ]


class RequestError(Exception):
  def __init__(
    self,
    code: str,
    message: str,
    status: HTTPStatus = HTTPStatus.BAD_REQUEST,
  ) -> None:
    super().__init__(message)
    self.code = code
    self.message = message
    self.status = status


def _validate_token(value: bytes | bytearray | memoryview) -> None:
  if len(value) < 32 or len(value) > 512:
    raise RuntimeError(
      "MOBIUS_RECOVERY_TARGET_TOKEN must contain 32-512 printable ASCII bytes"
    )
  if any(byte < 0x21 or byte > 0x7E for byte in value):
    raise RuntimeError(
      "MOBIUS_RECOVERY_TARGET_TOKEN must contain 32-512 printable ASCII bytes"
    )


def _token_digest(value: bytes | bytearray | memoryview) -> bytes:
  verifier = hashlib.sha256()
  verifier.update(_TOKEN_DIGEST_DOMAIN)
  verifier.update(value)
  return verifier.digest()


def _capability_state(
  libc: ctypes.CDLL,
) -> tuple[_CapabilityHeader, Any]:
  header = _CapabilityHeader(version=_CAPABILITY_VERSION_3, pid=0)
  data = (_CapabilityData * 2)()
  if libc.capget(ctypes.byref(header), ctypes.byref(data)) != 0:
    error = ctypes.get_errno()
    raise RuntimeError(f"could not inspect process capabilities: errno {error}")
  return header, data


def _drop_recovery_escape_capabilities() -> None:
  """Deny packet capture, ptrace, and mounts to target exec children."""
  libc = ctypes.CDLL(None, use_errno=True)
  capget = getattr(libc, "capget", None)
  capset = getattr(libc, "capset", None)
  prctl = getattr(libc, "prctl", None)
  if capget is None or capset is None or prctl is None:
    raise RuntimeError("recovery target requires Linux capability controls")

  # A root repair command must not capture a later Authorization header,
  # ptrace target PID1, or create a nested mount that bypasses filesystem-root
  # policy. None of these powers are needed to repair the stopped /data tree.
  # Remove them from every mutable set and the bounding set before listening.
  header, data = _capability_state(libc)
  for capability in _BLOCKED_CAPABILITIES:
    word = capability // 32
    mask = 1 << (capability % 32)
    data[word].effective &= ~mask
    data[word].permitted &= ~mask
    data[word].inheritable &= ~mask

    bounded = prctl(_PR_CAPBSET_READ, capability, 0, 0, 0)
    if bounded < 0:
      error = ctypes.get_errno()
      raise RuntimeError(
        f"could not inspect capability bounding set: errno {error}"
      )
    if bounded and prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0) != 0:
      error = ctypes.get_errno()
      raise RuntimeError(
        f"could not drop capability {capability} from bounding set: errno {error}"
      )

  if capset(ctypes.byref(header), ctypes.byref(data)) != 0:
    error = ctypes.get_errno()
    raise RuntimeError(f"could not restrict process capabilities: errno {error}")
  if prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
    error = ctypes.get_errno()
    raise RuntimeError(f"could not clear ambient capabilities: errno {error}")

  # Fail closed if the kernel ignored any operation. The bounding-set check is
  # essential because uid 0 regains capabilities from that set across exec.
  _, restricted = _capability_state(libc)
  for capability in _BLOCKED_CAPABILITIES:
    word = capability // 32
    mask = 1 << (capability % 32)
    if (
      restricted[word].effective & mask
      or restricted[word].permitted & mask
      or restricted[word].inheritable & mask
    ):
      raise RuntimeError(f"capability {capability} remains in process sets")
    if prctl(_PR_CAPBSET_READ, capability, 0, 0, 0) != 0:
      raise RuntimeError(f"capability {capability} remains in bounding set")
    if prctl(
      _PR_CAP_AMBIENT, _PR_CAP_AMBIENT_IS_SET, capability, 0, 0
    ) != 0:
      raise RuntimeError(f"capability {capability} remains in ambient set")


def _set_process_nondumpable() -> None:
  """Blocks sibling/child ptrace and /proc memory access to the bearer."""
  libc = ctypes.CDLL(None, use_errno=True)
  prctl = getattr(libc, "prctl", None)
  if prctl is None:
    raise RuntimeError("recovery target requires Linux prctl")
  # PR_SET_DUMPABLE = 4. Failure must stop recovery rather than expose the
  # in-memory capability to an arbitrary root command launched by this target.
  if prctl(4, 0, 0, 0, 0) != 0:
    error = ctypes.get_errno()
    raise RuntimeError(f"could not disable process dumpability: errno {error}")


def _set_child_subreaper() -> None:
  """Keep every orphaned repair process below this immutable PID1.

  PID 1 is already the final reparenting point in a container PID namespace,
  but setting and verifying the Linux subreaper bit makes that dependency
  explicit and gives the per-exec supervisor the same fail-closed primitive.
  """
  libc = ctypes.CDLL(None, use_errno=True)
  prctl = getattr(libc, "prctl", None)
  if prctl is None:
    raise RuntimeError("recovery target requires Linux prctl")
  if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    error = ctypes.get_errno()
    raise RuntimeError(f"could not become a child subreaper: errno {error}")
  enabled = ctypes.c_int(0)
  if prctl(
    _PR_GET_CHILD_SUBREAPER, ctypes.byref(enabled), 0, 0, 0,
  ) != 0:
    error = ctypes.get_errno()
    raise RuntimeError(f"could not verify child subreaper state: errno {error}")
  if enabled.value != 1:
    raise RuntimeError("kernel ignored child subreaper configuration")


def _assert_clean_initial_environment() -> None:
  """Proves the exec environment itself never received the bearer."""
  if "MOBIUS_RECOVERY_TARGET_TOKEN" in os.environ:
    raise RuntimeError(
      "MOBIUS_RECOVERY_TARGET_TOKEN must not reach the target environment"
    )
  try:
    initial_environment = Path("/proc/self/environ").read_bytes()
  except OSError as exc:
    raise RuntimeError("could not inspect recovery target environment") from exc
  if b"MOBIUS_RECOVERY_TARGET_TOKEN=" in initial_environment:
    raise RuntimeError("recovery target bearer is exposed through /proc")


def _read_startup_token_digest() -> bytes:
  """Consumes and wipes the bearer, retaining only a one-way verifier."""
  if "MOBIUS_RECOVERY_TARGET_TOKEN" in os.environ:
    raise RuntimeError(
      "MOBIUS_RECOVERY_TARGET_TOKEN must not reach the target environment"
    )
  raw_fd = os.environ.pop("MOBIUS_RECOVERY_TARGET_TOKEN_FD", "")
  if not raw_fd.isdecimal():
    raise RuntimeError("MOBIUS_RECOVERY_TARGET_TOKEN_FD is required")
  fd = int(raw_fd)
  if fd < 3:
    raise RuntimeError("MOBIUS_RECOVERY_TARGET_TOKEN_FD is invalid")
  value = bytearray(513)
  view = memoryview(value)
  token: memoryview | None = None
  length = 0
  try:
    while length < len(value):
      read = os.readv(fd, [view[length:]])
      if not read:
        break
      length += read
  except OSError as exc:
    raise RuntimeError("could not consume recovery target bearer") from exc
  finally:
    try:
      os.close(fd)
    except OSError:
      pass
  try:
    token = view[:length]
    _validate_token(token)
    return _token_digest(token)
  finally:
    if token is not None:
      token.release()
    view.release()
    for index in range(len(value)):
      value[index] = 0


def _read_target_expiry() -> int:
  """Consume a bounded absolute deadline for the root capability."""
  raw = os.environ.pop("MOBIUS_RECOVERY_TARGET_EXPIRES_AT", "")
  if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
    raise RuntimeError(
      "MOBIUS_RECOVERY_TARGET_EXPIRES_AT must be an epoch integer"
    )
  expires_at = int(raw)
  now = int(time.time())
  if expires_at <= now:
    raise RuntimeError("recovery target expiry must be in the future")
  if expires_at > now + MAX_TARGET_LIFETIME_SECONDS:
    raise RuntimeError(
      "recovery target expiry exceeds the maximum 24-hour lifetime"
    )
  return expires_at


def _target_is_expired() -> bool:
  expires_at = _TARGET_EXPIRES_AT
  if _TARGET_EXPIRED.is_set() or expires_at is None:
    return True
  if time.time() >= expires_at:
    _TARGET_EXPIRED.set()
    return True
  return False


def _require_active_target() -> None:
  if _target_is_expired():
    raise RequestError(
      "auth_expired",
      "recovery target capability has expired",
      HTTPStatus.UNAUTHORIZED,
    )


def _load_baked_build_revision() -> str:
  try:
    value = BUILD_REVISION_PATH.read_text(encoding="ascii").strip().lower()
  except OSError as exc:
    raise RuntimeError("baked recovery target identity is missing") from exc
  if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
    raise RuntimeError("baked recovery target identity is invalid")
  return value


def _initialize_startup_security(*, require_pid_one: bool = True) -> None:
  """Loads immutable identity + bearer before any request can be accepted."""
  global _BUILD_REVISION, _STARTUP_TOKEN_DIGEST, _TARGET_EXPIRES_AT
  if require_pid_one and os.getpid() != 1:
    raise RuntimeError(
      "recovery target must be container pid 1 so no parent retains its bearer"
    )
  if _STARTUP_TOKEN_DIGEST is not None:
    raise RuntimeError("recovery target startup security was already initialized")
  _assert_clean_initial_environment()
  _assert_fs_policy_supported()
  _drop_recovery_escape_capabilities()
  _set_process_nondumpable()
  _set_child_subreaper()
  _TARGET_EXPIRES_AT = _read_target_expiry()
  _STARTUP_TOKEN_DIGEST = _read_startup_token_digest()
  _BUILD_REVISION = _load_baked_build_revision()


def _startup_token_digest() -> bytes:
  if _STARTUP_TOKEN_DIGEST is None:
    raise RuntimeError("recovery target bearer is not initialized")
  return _STARTUP_TOKEN_DIGEST


def _absolute_path(value: Any, field: str = "path") -> Path:
  if not isinstance(value, str) or not value or "\x00" in value:
    raise RequestError("invalid_path", f"{field} must be a non-empty path")
  path = Path(value)
  if not path.is_absolute():
    raise RequestError("invalid_path", f"{field} must be absolute")
  return path


def _openat2(dir_fd: int, relative: Path, flags: int, mode: int = 0) -> int:
  """Open one path beneath dir_fd without symlink or mount escapes."""
  if relative.is_absolute():
    raise ValueError("openat2 path must be relative")
  raw_path = os.fsencode(str(relative) or ".")
  if b"\x00" in raw_path:
    raise OSError(errno.EINVAL, "path contains NUL")
  how = _OpenHow(
    flags=flags,
    mode=mode,
    resolve=_RESOLVE_BENEATH | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_XDEV,
  )
  libc = ctypes.CDLL(None, use_errno=True)
  result = libc.syscall(
    ctypes.c_long(_SYS_OPENAT2),
    ctypes.c_int(dir_fd),
    ctypes.c_char_p(raw_path),
    ctypes.byref(how),
    ctypes.c_size_t(ctypes.sizeof(how)),
  )
  if result < 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), os.fsdecode(raw_path))
  return int(result)


def _select_fs_root(path: Path, roots: tuple[Path, ...]) -> tuple[Path, Path]:
  for root in sorted(set(roots), key=lambda item: len(item.parts), reverse=True):
    if not root.is_absolute():
      continue
    try:
      relative = path.relative_to(root)
    except ValueError:
      continue
    return root, relative if relative.parts else Path(".")
  raise RequestError(
    "path_forbidden",
    "path is outside the recovery filesystem roots",
    HTTPStatus.FORBIDDEN,
  )


def _translate_policy_open_error(exc: OSError) -> None:
  if exc.errno in {errno.EXDEV, errno.ELOOP}:
    raise RequestError(
      "path_forbidden",
      "path escapes its recovery filesystem root",
      HTTPStatus.FORBIDDEN,
    ) from exc
  if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP}:
    raise RequestError(
      "path_policy_unavailable",
      "kernel-enforced recovery path policy is unavailable",
      HTTPStatus.INTERNAL_SERVER_ERROR,
    ) from exc
  raise exc


def _open_relative(root: Path, relative: Path, flags: int, mode: int = 0) -> int:
  root_flags = (
    getattr(os, "O_PATH", os.O_RDONLY)
    | os.O_DIRECTORY
    | os.O_CLOEXEC
    | getattr(os, "O_NOFOLLOW", 0)
  )
  root_fd = os.open(root, root_flags)
  try:
    try:
      return _openat2(root_fd, relative, flags | os.O_CLOEXEC, mode)
    except OSError as exc:
      _translate_policy_open_error(exc)
      raise AssertionError("unreachable")
  finally:
    os.close(root_fd)


def _open_fs_path(
  path: Path,
  roots: tuple[Path, ...],
  flags: int,
  mode: int = 0,
) -> int:
  root, relative = _select_fs_root(path, roots)
  return _open_relative(root, relative, flags, mode)


def _open_fs_parent(
  path: Path, roots: tuple[Path, ...],
) -> tuple[int, str]:
  root, relative = _select_fs_root(path, roots)
  name = relative.name
  if relative == Path(".") or name in {"", ".", ".."} or "/" in name:
    raise RequestError(
      "invalid_path", "path must name an entry below a recovery filesystem root"
    )
  parent_fd = _open_relative(
    root, relative.parent, os.O_RDONLY | os.O_DIRECTORY,
  )
  return parent_fd, name


def _assert_fs_policy_supported() -> None:
  """Fail target startup unless the kernel can enforce race-free roots."""
  if any(not root.is_absolute() for root in (*_FS_READ_ROOTS, *_FS_WRITE_ROOTS)):
    raise RuntimeError("recovery filesystem roots must be absolute")
  try:
    fd = _open_relative(Path("/tmp"), Path("."), os.O_RDONLY | os.O_DIRECTORY)
  except (OSError, RequestError) as exc:
    raise RuntimeError(
      "kernel-enforced recovery filesystem policy is unavailable"
    ) from exc
  else:
    os.close(fd)


def _bounded_int(
  value: Any,
  *,
  field: str,
  default: int,
  minimum: int,
  maximum: int,
) -> int:
  if value is None:
    return default
  if isinstance(value, bool) or not isinstance(value, int):
    raise RequestError("invalid_request", f"{field} must be an integer")
  if value < minimum or value > maximum:
    raise RequestError(
      "invalid_request", f"{field} must be between {minimum} and {maximum}"
    )
  return value


def _decode_base64(value: Any, field: str) -> bytes:
  if not isinstance(value, str):
    raise RequestError("invalid_request", f"{field} must be base64 text")
  try:
    decoded = base64.b64decode(value, validate=True)
  except (binascii.Error, ValueError) as exc:
    raise RequestError("invalid_request", f"{field} is not valid base64") from exc
  if len(decoded) > MAX_FILE_BYTES:
    raise RequestError(
      "payload_too_large",
      f"decoded {field} exceeds {MAX_FILE_BYTES} bytes",
      HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    )
  return decoded


def _process_record(pid: int) -> tuple[int, int] | None:
  """Return ``(ppid, starttime)`` from proc without trusting process names."""
  try:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    tail = raw[raw.rindex(")") + 2:].split()
    return int(tail[1]), int(tail[19])
  except (OSError, ValueError, IndexError):
    return None


def _process_snapshot() -> dict[int, tuple[int, int]]:
  snapshot: dict[int, tuple[int, int]] = {}
  try:
    entries = os.scandir("/proc")
  except OSError:
    return snapshot
  with entries:
    for entry in entries:
      if not entry.name.isdecimal():
        continue
      pid = int(entry.name)
      record = _process_record(pid)
      if record is not None:
        snapshot[pid] = record
  return snapshot


def _descendant_snapshot(
  root_pid: int,
) -> dict[int, tuple[int, int, int]]:
  """Return descendants as ``pid -> (ppid, starttime, depth)``."""
  processes = _process_snapshot()
  descendants: dict[int, tuple[int, int, int]] = {}
  frontier = {root_pid}
  depth = 1
  while frontier:
    found = {
      pid for pid, (ppid, _starttime) in processes.items()
      if ppid in frontier and pid != root_pid and pid not in descendants
    }
    for pid in found:
      ppid, starttime = processes[pid]
      descendants[pid] = (ppid, starttime, depth)
    frontier = found
    depth += 1
  return descendants


def _signal_recorded_process(pid: int, starttime: int, signum: int) -> None:
  current = _process_record(pid)
  if current is None or current[1] != starttime:
    return
  try:
    os.kill(pid, signum)
  except ProcessLookupError:
    pass


def _stop_and_kill_descendants(root_pid: int) -> None:
  """Freeze then kill a whole process tree, including new sessions.

  The exec supervisor is a child subreaper, so double-forked and ``setsid``
  processes remain descendants of this one stable root. Freezing ancestors
  before the final scan closes the fork-while-cleaning race.
  """
  descendants = _descendant_snapshot(root_pid)
  for pid, (_ppid, starttime, depth) in sorted(
    descendants.items(), key=lambda item: item[1][2],
  ):
    _signal_recorded_process(pid, starttime, signal.SIGSTOP)
  # A child could fork between the first snapshot and SIGSTOP delivery.
  descendants.update(_descendant_snapshot(root_pid))
  for pid, (_ppid, starttime, depth) in sorted(
    descendants.items(), key=lambda item: item[1][2], reverse=True,
  ):
    _signal_recorded_process(pid, starttime, signal.SIGKILL)


def _reap_all_children(deadline: float) -> None:
  while time.monotonic() < deadline:
    reaped = False
    while True:
      try:
        pid, _status = os.waitpid(-1, os.WNOHANG)
      except ChildProcessError:
        return
      if pid == 0:
        break
      reaped = True
    if not reaped:
      time.sleep(0.01)


def _retire_untracked_target_children() -> None:
  """Kill/reap only children not owned by another concurrent exec."""
  with _ACTIVE_SUPERVISORS_LOCK:
    active = set(_ACTIVE_SUPERVISORS)
    snapshot = _process_snapshot()
    roots = {
      pid: starttime
      for pid, (ppid, starttime) in snapshot.items()
      if ppid == os.getpid() and pid not in active
    }
    for pid in roots:
      _stop_and_kill_descendants(pid)
    for pid, starttime in roots.items():
      _signal_recorded_process(pid, starttime, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    for pid in roots:
      while time.monotonic() < deadline:
        try:
          waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
          break
        if waited == pid:
          break
        time.sleep(0.01)


def _request_supervisor_termination(process: subprocess.Popen[bytes]) -> None:
  if process.poll() is not None:
    return
  try:
    os.kill(process.pid, signal.SIGTERM)
  except ProcessLookupError:
    pass


def _force_kill_supervisor(process: subprocess.Popen[bytes]) -> None:
  if process.poll() is not None:
    return
  _stop_and_kill_descendants(process.pid)
  try:
    os.kill(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass


def _retire_active_supervisors(*, force: bool) -> None:
  """Stop every in-flight root command when the capability expires."""
  with _ACTIVE_SUPERVISORS_LOCK:
    active = list(_ACTIVE_SUPERVISORS)
  for pid in active:
    record = _process_record(pid)
    if record is None:
      continue
    _ppid, starttime = record
    if force:
      _stop_and_kill_descendants(pid)
      _signal_recorded_process(pid, starttime, signal.SIGKILL)
    else:
      _signal_recorded_process(pid, starttime, signal.SIGTERM)


def _revoke_target(server: Any) -> None:
  """Revoke once, close the listener, and leave PID1 parked without restart."""
  global _STARTUP_TOKEN_DIGEST, _TARGET_SHUTDOWN_STARTED
  with _TARGET_REVOCATION_LOCK:
    _TARGET_EXPIRED.set()
    # Only a one-way digest survives startup, but discard even that verifier
    # at expiry. Every request checks the sticky expiry event before auth.
    _STARTUP_TOKEN_DIGEST = None
    if _TARGET_SHUTDOWN_STARTED:
      return
    _TARGET_SHUTDOWN_STARTED = True

  _retire_active_supervisors(force=False)
  try:
    # Called by a timer or request thread, never the serve_forever thread.
    server.shutdown()
  finally:
    # Graceful supervisor termination owns normal tree cleanup. This forced
    # pass closes the small race where a hostile child delays that cleanup.
    _retire_active_supervisors(force=True)


def _read_supervisor_config(fd: int) -> dict[str, Any]:
  payload = bytearray()
  try:
    while len(payload) <= MAX_REQUEST_BYTES:
      chunk = os.read(fd, min(64 * 1024, MAX_REQUEST_BYTES + 1 - len(payload)))
      if not chunk:
        break
      payload.extend(chunk)
  finally:
    os.close(fd)
  if len(payload) > MAX_REQUEST_BYTES:
    raise RuntimeError("exec supervisor configuration is too large")
  try:
    value = json.loads(payload.decode("utf-8"))
  except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise RuntimeError("exec supervisor configuration is invalid") from exc
  if not isinstance(value, dict):
    raise RuntimeError("exec supervisor configuration is invalid")
  return value


def _supervisor_exit_code(returncode: int) -> int:
  if returncode < 0:
    return min(255, 128 - returncode)
  return min(255, returncode)


def _exec_supervisor_main(raw_fd: str) -> int:
  """Own one repair process tree until every descendant has been reaped."""
  if not raw_fd.isdecimal() or int(raw_fd) < 3:
    print("recovery exec supervisor received an invalid descriptor", file=sys.stderr)
    return 125
  try:
    _set_child_subreaper()
    config = _read_supervisor_config(int(raw_fd))
    argv = config["argv"]
    cwd = config["cwd"]
    env = config["env"]
    if not isinstance(argv, list) or not argv or not isinstance(cwd, str):
      raise RuntimeError("exec supervisor configuration is invalid")
    if not isinstance(env, dict):
      raise RuntimeError("exec supervisor configuration is invalid")
  except (KeyError, OSError, RuntimeError) as exc:
    print(f"recovery exec supervisor failed to initialize: {exc}", file=sys.stderr)
    return 125

  termination_signal: list[int | None] = [None]

  def terminate(signum: int, _frame: object) -> None:
    termination_signal[0] = signum

  for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, terminate)

  child: subprocess.Popen[bytes] | None = None
  returncode = 125
  try:
    child = subprocess.Popen(argv, cwd=cwd, env=env)
    while True:
      if termination_signal[0] is not None:
        returncode = 128 + int(termination_signal[0])
        break
      polled = child.poll()
      if polled is not None:
        returncode = _supervisor_exit_code(polled)
        break
      time.sleep(0.01)
  except OSError as exc:
    print(f"recovery exec supervisor could not start command: {exc}", file=sys.stderr)
  finally:
    # This is intentionally unconditional after the direct command exits.
    # Background/setsid/double-fork children are never durable recovery state.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
      descendants = _descendant_snapshot(os.getpid())
      if not descendants:
        break
      _stop_and_kill_descendants(os.getpid())
      _reap_all_children(min(deadline, time.monotonic() + 0.25))
    _stop_and_kill_descendants(os.getpid())
    _reap_all_children(deadline)
  return returncode


def _run_exec(body: dict[str, Any]) -> dict[str, Any]:
  _require_active_target()
  argv = body.get("argv")
  if (
    not isinstance(argv, list)
    or not argv
    or len(argv) > 256
    or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
  ):
    raise RequestError(
      "invalid_request", "argv must be a non-empty list of non-empty strings"
    )
  cwd_value = body.get("cwd", "/data")
  cwd = _absolute_path(cwd_value, "cwd")
  if not cwd.is_dir():
    raise RequestError("invalid_cwd", "cwd must name an existing directory")

  timeout_raw = body.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
  if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
    raise RequestError("invalid_request", "timeout_seconds must be a number")
  timeout = float(timeout_raw)
  if not 0.1 <= timeout <= MAX_TIMEOUT_SECONDS:
    raise RequestError(
      "invalid_request",
      f"timeout_seconds must be between 0.1 and {MAX_TIMEOUT_SECONDS:g}",
    )

  requested_env = body.get("env", {})
  if not isinstance(requested_env, dict) or len(requested_env) > MAX_ENV_ITEMS:
    raise RequestError(
      "invalid_request", f"env must contain at most {MAX_ENV_ITEMS} entries"
    )
  env = {
    "HOME": "/root",
    "LANG": os.environ.get("LANG", "C.UTF-8"),
    "PATH": os.environ.get(
      "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ),
    "DATA_DIR": os.environ.get("DATA_DIR", "/data"),
  }
  requested_env_bytes = 0
  for key, value in requested_env.items():
    if (
      not isinstance(key, str)
      or not key
      or "=" in key
      or "\x00" in key
      or not isinstance(value, str)
      or "\x00" in value
    ):
      raise RequestError("invalid_request", "env keys and values must be strings")
    key_bytes = len(key.encode("utf-8"))
    value_bytes = len(value.encode("utf-8"))
    if key_bytes > 256 or value_bytes > 64 * 1024:
      raise RequestError("invalid_request", "env key or value is too large")
    requested_env_bytes += key_bytes + value_bytes
    if requested_env_bytes > MAX_ENV_BYTES:
      raise RequestError(
        "invalid_request",
        f"env exceeds {MAX_ENV_BYTES} aggregate UTF-8 bytes",
      )
    env[key] = value

  if "stdin" in body and "stdin_base64" in body:
    raise RequestError(
      "invalid_request", "stdin and stdin_base64 are mutually exclusive"
    )
  if "stdin_base64" in body:
    stdin = _decode_base64(body["stdin_base64"], "stdin_base64")
  else:
    stdin_value = body.get("stdin", "")
    if not isinstance(stdin_value, str):
      raise RequestError("invalid_request", "stdin must be a string")
    stdin = stdin_value.encode("utf-8")
    if len(stdin) > MAX_FILE_BYTES:
      raise RequestError(
        "payload_too_large",
        f"stdin exceeds {MAX_FILE_BYTES} bytes",
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
      )

  if not _EXEC_SLOTS.acquire(blocking=False):
    raise RequestError(
      "exec_busy",
      "the recovery target is already running the maximum number of commands",
      HTTPStatus.SERVICE_UNAVAILABLE,
    )
  started = time.monotonic()
  process: subprocess.Popen[bytes] | None = None
  config_read_fd: int | None = None
  config_write_fd: int | None = None
  try:
    try:
      config_read_fd, config_write_fd = os.pipe()
      supervisor_env = {
        "HOME": "/root",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get(
          "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "DATA_DIR": os.environ.get("DATA_DIR", "/data"),
      }
      with _ACTIVE_SUPERVISORS_LOCK:
        process = subprocess.Popen(
          [
            sys.executable, "-I", str(Path(__file__).resolve()),
            "--exec-supervisor", str(config_read_fd),
          ],
          cwd="/",
          env=supervisor_env,
          stdin=subprocess.PIPE,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          start_new_session=True,
          pass_fds=(config_read_fd,),
        )
        _ACTIVE_SUPERVISORS.add(process.pid)
      os.close(config_read_fd)
      config_read_fd = None
      config_payload = json.dumps({
        "argv": argv,
        "cwd": str(cwd),
        "env": env,
      }, separators=(",", ":")).encode("utf-8")
      config_offset = 0
      while config_offset < len(config_payload):
        config_offset += os.write(
          config_write_fd, config_payload[config_offset:],
        )
      os.close(config_write_fd)
      config_write_fd = None
    except OSError as exc:
      raise RequestError(
        "exec_failed", str(exc), HTTPStatus.UNPROCESSABLE_ENTITY
      ) from exc
    assert process.stdin and process.stdout and process.stderr
    for stream in (process.stdin, process.stdout, process.stderr):
      os.set_blocking(stream.fileno(), False)

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdin_view = memoryview(stdin)
    stdin_offset = 0
    if stdin:
      selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
      process.stdin.close()

    output = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    timed_out = False
    termination_requested_at: float | None = None
    process_exited_at: float | None = None
    while selector.get_map():
      if _target_is_expired() and termination_requested_at is None:
        termination_requested_at = time.monotonic()
        _request_supervisor_termination(process)
      remaining = timeout - (time.monotonic() - started)
      if remaining <= 0:
        timed_out = True
        if termination_requested_at is None:
          termination_requested_at = time.monotonic()
          _request_supervisor_termination(process)
        remaining = 0.1
      events = selector.select(min(max(remaining, 0.01), 0.25))
      for key, _mask in events:
        stream = key.fileobj
        kind = key.data
        if kind == "stdin":
          try:
            written = os.write(stream.fileno(), stdin_view[stdin_offset:])
            stdin_offset += written
          except BrokenPipeError:
            stdin_offset = len(stdin_view)
          if stdin_offset >= len(stdin_view):
            selector.unregister(stream)
            stream.close()
          continue
        try:
          chunk = os.read(stream.fileno(), 64 * 1024)
        except BlockingIOError:
          continue
        if not chunk:
          selector.unregister(stream)
          stream.close()
          continue
        target = output[kind]
        room = MAX_OUTPUT_BYTES - len(target)
        if room > 0:
          target.extend(chunk[:room])
        if len(chunk) > room:
          truncated = True
          if termination_requested_at is None:
            termination_requested_at = time.monotonic()
            _request_supervisor_termination(process)

      if process.poll() is not None:
        if process_exited_at is None:
          process_exited_at = time.monotonic()
        # Pipes may still contain a final kernel-buffered chunk. Keep draining
        # briefly, but never let an escaped descriptor holder defeat the API's
        # timeout after its per-exec supervisor has exited.
        if process.stdin in [item.fileobj for item in selector.get_map().values()]:
          selector.unregister(process.stdin)
          process.stdin.close()
        if time.monotonic() - process_exited_at >= 0.5:
          for item in list(selector.get_map().values()):
            stream = item.fileobj
            selector.unregister(stream)
            stream.close()
          break
      elif (
        termination_requested_at is not None
        and time.monotonic() - termination_requested_at >= 3.0
      ):
        _force_kill_supervisor(process)
    try:
      exit_code = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
      # A supervisor that has closed its pipes but has not exited is not a
      # completed repair command. Kill it before reporting a target failure so
      # callers can never mistake an uncontained process for a clean result.
      _force_kill_supervisor(process)
      try:
        exit_code = process.wait(timeout=1)
      except subprocess.TimeoutExpired as final_exc:
        raise RequestError(
          "exec_cleanup_failed",
          "repair process supervisor could not be retired",
          HTTPStatus.INTERNAL_SERVER_ERROR,
        ) from final_exc
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return {
      "exit_code": exit_code,
      "stdout_base64": base64.b64encode(output["stdout"]).decode("ascii"),
      "stderr_base64": base64.b64encode(output["stderr"]).decode("ascii"),
      "truncated": truncated,
      "timed_out": timed_out,
      "duration_ms": elapsed_ms,
    }
  finally:
    for fd in (config_read_fd, config_write_fd):
      if fd is not None:
        try:
          os.close(fd)
        except OSError:
          pass
    if process is not None and process.poll() is None:
      _request_supervisor_termination(process)
      try:
        process.wait(timeout=2)
      except subprocess.TimeoutExpired:
        _force_kill_supervisor(process)
        try:
          process.wait(timeout=1)
        except subprocess.TimeoutExpired:
          pass
    if process is not None:
      with _ACTIVE_SUPERVISORS_LOCK:
        _ACTIVE_SUPERVISORS.discard(process.pid)
    _retire_untracked_target_children()
    _EXEC_SLOTS.release()


def _read_file(body: dict[str, Any]) -> dict[str, Any]:
  _require_active_target()
  path = _absolute_path(body.get("path"))
  offset = _bounded_int(
    body.get("offset"), field="offset", default=0, minimum=0, maximum=2**63 - 1
  )
  limit = _bounded_int(
    body.get("limit"),
    field="limit",
    default=MAX_FILE_BYTES,
    minimum=1,
    maximum=MAX_FILE_BYTES,
  )
  fd: int | None = None
  try:
    fd = _open_fs_path(path, _FS_READ_ROOTS, os.O_RDONLY)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
      raise RequestError("not_a_file", "path is not a regular file")
    os.lseek(fd, offset, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
      chunk = os.read(fd, remaining)
      if not chunk:
        break
      chunks.append(chunk)
      remaining -= len(chunk)
    data = b"".join(chunks)
    return {
      "path": str(path),
      "offset": offset,
      "data_base64": base64.b64encode(data).decode("ascii"),
      "eof": offset + len(data) >= st.st_size,
      "size": st.st_size,
      "mode": stat.S_IMODE(st.st_mode),
      "uid": st.st_uid,
      "gid": st.st_gid,
    }
  except RequestError:
    raise
  except OSError as exc:
    raise RequestError(
      "fs_read_failed", str(exc), HTTPStatus.UNPROCESSABLE_ENTITY
    ) from exc
  finally:
    if fd is not None:
      os.close(fd)


def _write_file(body: dict[str, Any]) -> dict[str, Any]:
  _require_active_target()
  path = _absolute_path(body.get("path"))
  data = _decode_base64(body.get("data_base64"), "data_base64")
  mode = _bounded_int(
    body.get("mode"), field="mode", default=0o600, minimum=0, maximum=0o7777
  )
  atomic = body.get("atomic", True)
  if not isinstance(atomic, bool):
    raise RequestError("invalid_request", "atomic must be a boolean")
  parent_fd: int | None = None
  temp_name: str | None = None
  try:
    parent_fd, name = _open_fs_parent(path, _FS_WRITE_ROOTS)
    if atomic:
      for _attempt in range(10):
        candidate = f".{name}.{secrets.token_hex(8)}.tmp"
        try:
          fd = os.open(
            candidate,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            mode,
            dir_fd=parent_fd,
          )
        except FileExistsError:
          continue
        temp_name = candidate
        break
      else:
        raise OSError(errno.EEXIST, "could not allocate atomic write path")
      try:
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(data):
          offset += os.write(fd, data[offset:])
        os.fsync(fd)
      finally:
        os.close(fd)
      os.replace(
        temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
      )
      temp_name = None
      os.fsync(parent_fd)
    else:
      flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
      flags |= getattr(os, "O_NOFOLLOW", 0)
      fd = os.open(name, flags | os.O_CLOEXEC, mode, dir_fd=parent_fd)
      try:
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(data):
          offset += os.write(fd, data[offset:])
        os.fsync(fd)
      finally:
        os.close(fd)
    return {"path": str(path), "bytes_written": len(data), "mode": mode}
  except OSError as exc:
    raise RequestError(
      "fs_write_failed", str(exc), HTTPStatus.UNPROCESSABLE_ENTITY
    ) from exc
  finally:
    if temp_name is not None and parent_fd is not None:
      try:
        os.unlink(temp_name, dir_fd=parent_fd)
      except FileNotFoundError:
        pass
    if parent_fd is not None:
      os.close(parent_fd)


def _list_directory(body: dict[str, Any]) -> dict[str, Any]:
  _require_active_target()
  path = _absolute_path(body.get("path"))
  directory_fd: int | None = None
  try:
    directory_fd = _open_fs_path(
      path, _FS_READ_ROOTS, os.O_RDONLY | os.O_DIRECTORY,
    )
    entries: list[dict[str, Any]] = []
    encoded_bytes = len(str(path).encode("utf-8")) + 64
    with os.scandir(directory_fd) as iterator:
      for entry in iterator:
        if len(entries) >= MAX_LIST_ENTRIES:
          raise RequestError(
            "too_many_entries",
            f"directory contains more than {MAX_LIST_ENTRIES} entries",
            HTTPStatus.UNPROCESSABLE_ENTITY,
          )
        info = entry.stat(follow_symlinks=False)
        kind = (
          "symlink" if stat.S_ISLNK(info.st_mode)
          else "directory" if stat.S_ISDIR(info.st_mode)
          else "file" if stat.S_ISREG(info.st_mode)
          else "other"
        )
        item: dict[str, Any] = {
          "name": entry.name,
          "type": kind,
          "size": info.st_size,
          "mode": stat.S_IMODE(info.st_mode),
          "uid": info.st_uid,
          "gid": info.st_gid,
          "mtime_ns": info.st_mtime_ns,
        }
        if kind == "symlink":
          item["target"] = os.readlink(entry.name, dir_fd=directory_fd)
        encoded_bytes += len(
          json.dumps(item, separators=(",", ":")).encode("utf-8")
        ) + 1
        if encoded_bytes > MAX_LIST_RESPONSE_BYTES:
          raise RequestError(
            "response_too_large",
            f"directory metadata exceeds {MAX_LIST_RESPONSE_BYTES} bytes",
            HTTPStatus.UNPROCESSABLE_ENTITY,
          )
        entries.append(item)
    entries.sort(key=lambda item: item["name"])
    return {"path": str(path), "entries": entries}
  except RequestError:
    raise
  except OSError as exc:
    raise RequestError(
      "fs_list_failed", str(exc), HTTPStatus.UNPROCESSABLE_ENTITY
    ) from exc
  finally:
    if directory_fd is not None:
      os.close(directory_fd)


class _Handler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  server_version = "MobiusRecoveryTarget/1"

  def log_message(self, fmt: str, *args: object) -> None:
    print(f"recovery-target: {self.address_string()} {fmt % args}", flush=True)

  def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    self.send_response(status.value)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(raw)))
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Connection", "close")
    self.end_headers()
    self.wfile.write(raw)
    self.close_connection = True

  def _error(self, error: RequestError) -> None:
    self._send(
      error.status,
      {"error": {"code": error.code, "message": error.message}},
    )

  def _authorized(self) -> bool:
    if _target_is_expired():
      _revoke_target(self.server)
      self._send(
        HTTPStatus.UNAUTHORIZED,
        {
          "error": {
            "code": "auth_expired",
            "message": "recovery target capability has expired",
          }
        },
      )
      return False
    values = self.headers.get_all("Authorization", failobj=[]) or []
    supplied_buffer: bytearray | None = None
    authorized = False
    if len(values) == 1 and values[0].startswith("Bearer "):
      try:
        supplied_buffer = bytearray(values[0][7:], "ascii")
        _validate_token(supplied_buffer)
      except (UnicodeEncodeError, RuntimeError):
        pass
      else:
        supplied_digest = _token_digest(supplied_buffer)
        try:
          expected_digest = _startup_token_digest()
        except RuntimeError:
          expected_digest = b""
        authorized = hmac.compare_digest(supplied_digest, expected_digest)
      finally:
        if supplied_buffer is not None:
          for index in range(len(supplied_buffer)):
            supplied_buffer[index] = 0
    values.clear()
    # Close the compare-vs-deadline race: expiry is sticky, so even a wall-clock
    # adjustment cannot make a revoked capability valid again.
    if _target_is_expired():
      _revoke_target(self.server)
      self._send(
        HTTPStatus.UNAUTHORIZED,
        {
          "error": {
            "code": "auth_expired",
            "message": "recovery target capability has expired",
          }
        },
      )
      return False
    if not authorized:
      self._send(
        HTTPStatus.UNAUTHORIZED,
        {"error": {"code": "unauthorized", "message": "invalid bearer token"}},
      )
      return False
    return True

  def _body(self) -> dict[str, Any]:
    if self.headers.get("Transfer-Encoding"):
      raise RequestError(
        "invalid_framing", "Transfer-Encoding is not supported"
      )
    raw_length = self.headers.get("Content-Length")
    if raw_length is None or not raw_length.isdecimal():
      raise RequestError("invalid_framing", "a valid Content-Length is required")
    length = int(raw_length)
    if length > MAX_REQUEST_BYTES:
      raise RequestError(
        "payload_too_large",
        f"request exceeds {MAX_REQUEST_BYTES} bytes",
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
      )
    raw = self.rfile.read(length)
    if len(raw) != length:
      raise RequestError("invalid_framing", "request body ended early")
    try:
      value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
      raise RequestError("invalid_json", "request body must be a JSON object") from exc
    if not isinstance(value, dict):
      raise RequestError("invalid_json", "request body must be a JSON object")
    return value

  def do_GET(self) -> None:  # noqa: N802
    if not self._authorized():
      return
    if self.path != "/v1/health":
      self._error(RequestError("not_found", "endpoint not found", HTTPStatus.NOT_FOUND))
      return
    self._send(HTTPStatus.OK, {
      "protocol": PROTOCOL,
      "target": "mobius",
      "mode": "recovery",
      "build_sha": _BUILD_REVISION,
      "expires_at": _TARGET_EXPIRES_AT,
    })

  def do_POST(self) -> None:  # noqa: N802
    if not self._authorized():
      return
    handlers = {
      "/v1/exec": _run_exec,
      "/v1/fs/read": _read_file,
      "/v1/fs/write": _write_file,
      "/v1/fs/list": _list_directory,
    }
    operation = handlers.get(self.path)
    if operation is None:
      self._error(RequestError("not_found", "endpoint not found", HTTPStatus.NOT_FOUND))
      return
    try:
      result = operation(self._body())
    except RequestError as exc:
      self._error(exc)
      return
    except Exception:
      self._error(RequestError(
        "internal_error", "recovery target operation failed", HTTPStatus.INTERNAL_SERVER_ERROR
      ))
      return
    self._send(HTTPStatus.OK, result)


class _DualStackServer(ThreadingHTTPServer):
  address_family = socket.AF_INET6
  daemon_threads = True
  allow_reuse_address = True

  def server_bind(self) -> None:
    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    super().server_bind()


class _IPv4Server(ThreadingHTTPServer):
  daemon_threads = True
  allow_reuse_address = True


def _create_server(port: int) -> ThreadingHTTPServer:
  """Prefer one dual-stack socket, but keep recovery usable without IPv6."""
  try:
    return _DualStackServer(("::", port), _Handler)
  except OSError as exc:
    if exc.errno not in {
      errno.EAFNOSUPPORT,
      errno.EADDRNOTAVAIL,
      errno.ENOPROTOOPT,
      errno.EPROTONOSUPPORT,
    }:
      raise
    print(
      f"recovery-target: IPv6 unavailable ({exc}); falling back to IPv4",
      flush=True,
    )
    return _IPv4Server(("0.0.0.0", port), _Handler)


def _schedule_target_expiry(server: ThreadingHTTPServer) -> threading.Timer:
  expires_at = _TARGET_EXPIRES_AT
  if expires_at is None:
    raise RuntimeError("recovery target expiry is not initialized")
  delay = max(0.0, expires_at - time.time())
  timer = threading.Timer(delay, _revoke_target, args=(server,))
  timer.daemon = True
  timer.start()
  return timer


def _park_expired_target() -> None:
  """Keep an expired PID1 quiescent so restart policies cannot hot-loop."""
  stopped = threading.Event()

  def stop(_signum: int, _frame: object) -> None:
    stopped.set()

  for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, stop)
  while not stopped.wait(3600):
    pass


def main() -> None:
  if os.environ.get("MOBIUS_BOOT_MODE") != "recovery":
    raise SystemExit("recovery target refuses to run outside recovery boot mode")
  if os.geteuid() != 0:
    raise SystemExit("recovery target must run as root")
  try:
    _initialize_startup_security()
  except RuntimeError as exc:
    raise SystemExit(f"recovery target security initialization failed: {exc}") from exc
  raw_port = os.environ.get("MOBIUS_RECOVERY_TARGET_PORT", str(DEFAULT_PORT))
  try:
    port = int(raw_port)
  except ValueError as exc:
    raise SystemExit("MOBIUS_RECOVERY_TARGET_PORT must be an integer") from exc
  if not 1 <= port <= 65535:
    raise SystemExit("MOBIUS_RECOVERY_TARGET_PORT must be between 1 and 65535")
  # Every endpoint, including health, requires the bearer token. Managed
  # launchers therefore clear provider-level unauthenticated health checks and
  # probe /v1/health themselves before handing the worker to the owner.
  server = _create_server(port)
  print(
    f"Mobius recovery target {PROTOCOL} listening privately on [::]:{port}",
    flush=True,
  )
  expiry_timer = _schedule_target_expiry(server)
  try:
    server.serve_forever(poll_interval=0.25)
  finally:
    expiry_timer.cancel()
    server.server_close()
  if _TARGET_EXPIRED.is_set():
    print(
      "recovery-target: capability expired; listener closed and PID1 parked",
      flush=True,
    )
    _park_expired_target()


if __name__ == "__main__":
  if len(sys.argv) == 3 and sys.argv[1] == "--exec-supervisor":
    raise SystemExit(_exec_supervisor_main(sys.argv[2]))
  main()
