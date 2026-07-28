#!/usr/bin/env python3
"""Secure executors for reviewed scoped-authority app jobs.

The app manifest is normalized into one filesystem ``JobAccess`` contract.
Bubblewrap and Landlock are replaceable enforcement mechanisms for that
contract; neither is allowed to reinterpret app permissions. Selection uses
real primitive probes and fails closed when this host cannot provide a secure
executor.

Landlock filesystem rules are delegated to util-linux ``setpriv``.  The small
helper in this file supplies only the two protections setpriv does not:
Landlock's process/abstract-socket scopes and a seccomp denial for pathname
UNIX sockets and direct sibling-process inspection.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


MOBIUS_UID = 1000
MOBIUS_GID = 1000
LANDLOCK_MIN_ABI = 6

_READ_DIR = "execute,read-file,read-dir"
_READ_FILE = "read-file"
_WRITE_DIR = (
  "read-file,read-dir,write-file,remove-dir,remove-file,make-char,make-dir,"
  "make-reg,make-sock,make-fifo,make-block,make-sym,refer,truncate"
)
_DEVICE_FILE = "read-file,write-file"
_RUNTIME_READ_ROOTS = (
  "/app", "/bin", "/etc", "/lib", "/lib64", "/opt", "/sys", "/usr", "/var",
)
_RUNTIME_DEVICES = ("/dev/null", "/dev/random", "/dev/urandom")
_PROC_READ_FILES = (
  "/proc/cpuinfo", "/proc/filesystems", "/proc/loadavg", "/proc/meminfo",
  "/proc/stat", "/proc/uptime", "/proc/version",
)


@dataclass(frozen=True)
class JobAccess:
  """The reviewed filesystem contract shared by every secure executor."""

  source_read: Path
  storage_write: Path
  extra_read: tuple[Path, ...] = ()
  extra_write: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ExecutorProbe:
  executor: str
  available: bool
  detail: str


@dataclass(frozen=True)
class LaunchPlan:
  executor: str
  command: list[str]
  env: dict[str, str]


def _privilege_prefix(*, parent_death_signal: bool = False) -> list[str] | None:
  setpriv = shutil.which("setpriv")
  if not setpriv:
    return None
  prefix = [setpriv]
  if os.geteuid() == 0:
    prefix += [
      "--reuid", str(MOBIUS_UID), "--regid", str(MOBIUS_GID),
      "--clear-groups",
    ]
  elif os.geteuid() != MOBIUS_UID or os.getegid() != MOBIUS_GID:
    return None
  if parent_death_signal:
    prefix += ["--pdeathsig", "SIGKILL"]
  return prefix


def probe_bubblewrap() -> ExecutorProbe:
  """Exercise the namespace setup that the real executor needs."""

  bwrap = shutil.which("bwrap")
  prefix = _privilege_prefix()
  if not bwrap:
    return ExecutorProbe("bubblewrap", False, "bwrap is not installed")
  if prefix is None:
    return ExecutorProbe(
      "bubblewrap", False, "cannot run jobs as the mobius data owner",
    )
  try:
    result = subprocess.run(
      [
        *prefix, bwrap,
        "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
        "/bin/true",
      ],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.DEVNULL,
      stderr=subprocess.PIPE,
      text=True,
      timeout=5,
      check=False,
    )
  except subprocess.TimeoutExpired:
    return ExecutorProbe("bubblewrap", False, "namespace probe timed out")
  except OSError as exc:
    return ExecutorProbe("bubblewrap", False, f"probe failed: {exc}")
  if result.returncode == 0:
    return ExecutorProbe("bubblewrap", True, "namespace probe passed")
  detail = result.stderr.strip().splitlines()
  reason = detail[-1] if detail else f"probe exited {result.returncode}"
  return ExecutorProbe("bubblewrap", False, reason)


def _bubblewrap_plan(
  access: JobAccess,
  command: list[str],
  env: dict[str, str],
  sandbox_home: Path,
) -> LaunchPlan:
  bwrap = shutil.which("bwrap")
  prefix = _privilege_prefix()
  if not bwrap or prefix is None:
    raise RuntimeError("Bubblewrap was selected without a usable launcher")
  data_root = access.source_read.parent.parent
  apps_root = data_root / "apps"
  args = [
    *prefix, bwrap,
    "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--ro-bind", "/", "/",
    "--tmpfs", str(data_root), "--tmpfs", "/home", "--tmpfs", "/root",
    "--tmpfs", "/run", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    "--bind", str(sandbox_home), str(sandbox_home),
    "--dir", str(apps_root),
    "--ro-bind", str(access.source_read), str(access.source_read),
    "--bind", str(access.storage_write), str(access.storage_write),
  ]
  created_parents = {apps_root}
  for path in (*access.extra_read, *access.extra_write):
    parent = path.parent
    if parent.is_relative_to(data_root) and parent not in created_parents:
      args += ["--dir", str(parent)]
      created_parents.add(parent)
  for path in access.extra_read:
    args += ["--ro-bind", str(path), str(path)]
  for path in access.extra_write:
    args += ["--bind", str(path), str(path)]
  # The masked data root is otherwise a writable tmpfs at its top level.
  # Bubblewrap's remount is deliberately non-recursive, so the explicit
  # storage and extra-write bind mounts above remain writable.
  args += ["--remount-ro", str(data_root)]
  args += ["--chdir", str(access.source_read), *command]
  child_env = dict(env)
  child_env.update({
    "HOME": str(sandbox_home),
    "TMPDIR": str(sandbox_home),
    "PYTHONPYCACHEPREFIX": str(sandbox_home / "pycache"),
  })
  return LaunchPlan("bubblewrap", args, child_env)


def landlock_abi() -> int:
  """Return the running kernel's Landlock ABI on supported architectures."""

  if platform.machine() not in {"x86_64", "aarch64"}:
    return 0
  libc = ctypes.CDLL(None, use_errno=True)
  result = libc.syscall(444, 0, 0, 1)  # LANDLOCK_CREATE_RULESET_VERSION
  return int(result) if result >= 0 else 0


def _landlock_rule(rights: str, path: Path | str) -> str:
  return f"path-beneath:{rights}:{Path(path).resolve(strict=True)}"


def _raw_landlock_rule(rights: str, path: str) -> str:
  """Keep /proc/self resolution in the setpriv child, not this supervisor."""

  return f"path-beneath:{rights}:{path}"


def _landlock_setpriv(
  access: JobAccess,
  sandbox_home: Path,
  command: list[str],
) -> list[str] | None:
  prefix = _privilege_prefix(parent_death_signal=True)
  if prefix is None:
    return None
  args = [*prefix, "--nnp", "--landlock-access", "fs"]
  for raw in _RUNTIME_READ_ROOTS:
    path = Path(raw)
    if path.exists():
      args += ["--landlock-rule", _landlock_rule(_READ_DIR, path)]
  for raw in _RUNTIME_DEVICES:
    path = Path(raw)
    if path.exists():
      args += ["--landlock-rule", _landlock_rule(_DEVICE_FILE, path)]
  for raw in ("/proc/self", "/proc/thread-self"):
    if Path(raw).exists():
      args += ["--landlock-rule", _raw_landlock_rule(_READ_DIR, raw)]
  for raw in _PROC_READ_FILES:
    path = Path(raw)
    if path.exists():
      args += ["--landlock-rule", _landlock_rule(_READ_FILE, path)]
  args += [
    "--landlock-rule", _landlock_rule(_READ_DIR, access.source_read),
    "--landlock-rule", _landlock_rule(_WRITE_DIR, access.storage_write),
    "--landlock-rule", _landlock_rule(_WRITE_DIR, sandbox_home),
  ]
  for path in access.extra_read:
    rights = _READ_DIR if path.is_dir() else _READ_FILE
    args += ["--landlock-rule", _landlock_rule(rights, path)]
  for path in access.extra_write:
    rights = _WRITE_DIR if path.is_dir() else _DEVICE_FILE
    args += ["--landlock-rule", _landlock_rule(rights, path)]
  return [*args, *command]


def _helper_command(command: list[str]) -> list[str]:
  return [
    sys.executable, str(Path(__file__).resolve()),
    "--restrict-process", *command,
  ]


def probe_landlock() -> ExecutorProbe:
  """Probe the fallback primitives: scopes, seccomp and a filesystem denial."""

  abi = landlock_abi()
  if abi < LANDLOCK_MIN_ABI:
    return ExecutorProbe(
      "landlock", False,
      f"kernel ABI {abi or 'unavailable'}; ABI {LANDLOCK_MIN_ABI}+ required",
    )
  setpriv = shutil.which("setpriv")
  if not setpriv:
    return ExecutorProbe("landlock", False, "setpriv is not installed")
  prefix = _privilege_prefix(parent_death_signal=True)
  if prefix is None:
    return ExecutorProbe(
      "landlock", False, "cannot run jobs as the mobius data owner",
    )
  descriptor, denied_path = tempfile.mkstemp(prefix="mobius-landlock-probe-")
  try:
    os.write(descriptor, b"must be denied\n")
    os.close(descriptor)
    descriptor = -1
    # Root-launched cron drops to mobius inside setpriv. Keep the probe file
    # ordinarily readable by that uid so a successful `cat` proves Landlock
    # was not actually enforcing the rule, rather than merely hitting mode 600.
    os.chmod(denied_path, 0o644)
    command = [
      *prefix,
      "--nnp",
      "--landlock-access", "fs",
      "--landlock-rule", _landlock_rule(_READ_DIR, "/usr"),
      "/usr/bin/sh", "-c",
      'if /usr/bin/cat "$1"; then exit 91; else exit 0; fi',
      "landlock-probe", denied_path,
    ]
    # Keep the helper first: it installs process/socket restrictions and then
    # execs setpriv, which installs the filesystem rules and drops privileges.
    command = _helper_command(["--probe-unix-denial", *command])
    try:
      result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
      )
    except subprocess.TimeoutExpired:
      return ExecutorProbe("landlock", False, "enforcement probe timed out")
    except OSError as exc:
      return ExecutorProbe("landlock", False, f"probe failed: {exc}")
  finally:
    if descriptor >= 0:
      os.close(descriptor)
    Path(denied_path).unlink(missing_ok=True)
  if result.returncode == 0:
    return ExecutorProbe("landlock", True, f"ABI {abi} enforcement probe passed")
  detail = result.stderr.strip().splitlines()
  reason = detail[-1] if detail else f"probe exited {result.returncode}"
  return ExecutorProbe("landlock", False, f"ABI {abi}: {reason}")


def _landlock_plan(
  access: JobAccess,
  command: list[str],
  env: dict[str, str],
  sandbox_home: Path,
) -> LaunchPlan:
  restricted = _landlock_setpriv(access, sandbox_home, command)
  if restricted is None:
    raise RuntimeError("Landlock was selected without a usable launcher")
  child_env = dict(env)
  child_env.update({
    "HOME": str(sandbox_home),
    "TMPDIR": str(sandbox_home),
    "PYTHONPYCACHEPREFIX": str(sandbox_home / "pycache"),
  })
  return LaunchPlan("landlock", _helper_command(restricted), child_env)


def select_executor(
  access: JobAccess,
  command: list[str],
  env: dict[str, str],
  sandbox_home: Path,
) -> tuple[LaunchPlan | None, tuple[ExecutorProbe, ...]]:
  """Choose the strongest working executor; never run without a boundary."""

  bubblewrap = probe_bubblewrap()
  if bubblewrap.available:
    probes = (bubblewrap,)
    return _bubblewrap_plan(access, command, env, sandbox_home), probes
  landlock = probe_landlock()
  probes = (bubblewrap, landlock)
  if landlock.available:
    return _landlock_plan(access, command, env, sandbox_home), probes
  return None, probes


# The helper is intentionally small.  setpriv owns filesystem policy; these
# calls only close the gaps between Landlock ABI 6-8 and Bubblewrap's process
# namespace.
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_LANDLOCK_SCOPE_SIGNAL = 1 << 1
_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_EQ = 4


class _RulesetAttr(ctypes.Structure):
  _fields_ = [
    ("handled_access_fs", ctypes.c_uint64),
    ("handled_access_net", ctypes.c_uint64),
    ("scoped", ctypes.c_uint64),
  ]


class _ScmpArgCmp(ctypes.Structure):
  _fields_ = [
    ("arg", ctypes.c_uint),
    ("op", ctypes.c_int),
    ("datum_a", ctypes.c_uint64),
    ("datum_b", ctypes.c_uint64),
  ]


def _checked(result: int, operation: str) -> int:
  if result < 0:
    error = ctypes.get_errno()
    raise OSError(error, f"{operation}: {os.strerror(error)}")
  return int(result)


def _apply_process_scope() -> None:
  if landlock_abi() < LANDLOCK_MIN_ABI:
    raise RuntimeError(f"Landlock ABI {LANDLOCK_MIN_ABI}+ unavailable")
  libc = ctypes.CDLL(None, use_errno=True)
  parent = os.getppid()
  if parent == 1:
    raise RuntimeError("sandbox supervisor already exited")
  if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
    _checked(-1, "prctl(PR_SET_PDEATHSIG)")
  if os.getppid() != parent:
    os.kill(os.getpid(), signal.SIGKILL)
  attr = _RulesetAttr(
    0, 0,
    _LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | _LANDLOCK_SCOPE_SIGNAL,
  )
  ruleset = _checked(
    libc.syscall(
      _LANDLOCK_CREATE_RULESET,
      ctypes.byref(attr),
      ctypes.sizeof(attr),
      0,
    ),
    "landlock_create_ruleset",
  )
  try:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
      _checked(-1, "prctl(PR_SET_NO_NEW_PRIVS)")
    _checked(
      libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset, 0),
      "landlock_restrict_self",
    )
  finally:
    os.close(ruleset)


def _apply_seccomp() -> None:
  try:
    lib = ctypes.CDLL("libseccomp.so.2")
  except OSError as exc:
    raise RuntimeError("libseccomp.so.2 is unavailable") from exc
  lib.seccomp_init.argtypes = [ctypes.c_uint32]
  lib.seccomp_init.restype = ctypes.c_void_p
  lib.seccomp_release.argtypes = [ctypes.c_void_p]
  lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
  lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
  lib.seccomp_rule_add_array.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ctypes.POINTER(_ScmpArgCmp),
  ]
  lib.seccomp_rule_add_array.restype = ctypes.c_int
  lib.seccomp_load.argtypes = [ctypes.c_void_p]
  lib.seccomp_load.restype = ctypes.c_int

  context = lib.seccomp_init(_SCMP_ACT_ALLOW)
  if not context:
    raise RuntimeError("seccomp_init failed")
  deny = _SCMP_ACT_ERRNO | errno.EPERM
  try:
    socket_nr = lib.seccomp_syscall_resolve_name(b"socket")
    if socket_nr < 0:
      raise RuntimeError("seccomp cannot resolve socket")
    comparison = _ScmpArgCmp(0, _SCMP_CMP_EQ, socket.AF_UNIX, 0)
    result = lib.seccomp_rule_add_array(
      context, deny, socket_nr, 1, ctypes.byref(comparison),
    )
    if result != 0:
      raise OSError(-result, f"seccomp socket rule: {os.strerror(-result)}")

    for name in (
      b"ptrace", b"process_vm_readv", b"process_vm_writev",
      b"pidfd_getfd", b"kcmp", b"perf_event_open",
    ):
      syscall = lib.seccomp_syscall_resolve_name(name)
      if syscall < 0:
        continue
      result = lib.seccomp_rule_add_array(context, deny, syscall, 0, None)
      if result != 0:
        raise OSError(
          -result, f"seccomp {name.decode()} rule: {os.strerror(-result)}",
        )
    result = lib.seccomp_load(context)
    if result != 0:
      raise OSError(-result, f"seccomp_load: {os.strerror(-result)}")
  finally:
    lib.seccomp_release(context)


def _restrict_process(command: list[str]) -> int:
  if not command:
    return 2
  _apply_process_scope()
  _apply_seccomp()
  if command[:1] == ["--probe-unix-denial"]:
    try:
      socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError:
      command = command[1:]
    else:
      raise RuntimeError("AF_UNIX socket creation was not denied")
  os.execvpe(command[0], command, os.environ)
  return 127


if __name__ == "__main__":
  if sys.argv[1:2] != ["--restrict-process"]:
    raise SystemExit(2)
  try:
    raise SystemExit(_restrict_process(sys.argv[2:]))
  except BaseException as exc:
    print(f"secure executor setup failed: {exc}", file=sys.stderr)
    raise SystemExit(125)
