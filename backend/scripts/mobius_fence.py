#!/usr/bin/env python3
"""Run one program, such as an app's AI agent, inside a Möbius room.

Usage:
  mobius_fence.py --home DIR [--rw PATH]... [--ro PATH]... [--cwd PATH]
                  [--door HOST:PORT]... [--keep-env NAME]... -- CMD...
  mobius_fence.py --check      # can this host build rooms? (JSON)

A room is built only from unprivileged Linux features that work in an
ordinary container (no namespaces, no root), and everything started inside
inherits it and can only narrow it further:

- Walls (Landlock): system folders are read-only, ``--home`` and ``--rw`` are
  writable, ``--ro`` is readable. Nothing else on disk can be opened. In a
  ``--rw`` folder the program may make only ordinary files and folders: code
  outside the room reads what it leaves there, so a symlink planted there must
  not point that code at a file the room itself cannot open. The
  program cannot signal or inspect processes outside the room, and
  ``no_new_privs`` stops ``sudo``.
- Doors (Landlock ports + seccomp user notification): a connect() succeeds
  only to an exact ``--door`` address. Landlock alone matches ports, not
  hosts, so a supervisor *outside* the room checks each connect() and performs
  it itself on the program's own socket: the checked address is the one used.
  Unix sockets, UDP, io_uring and TCP Fast Open (ways around the doors) are
  refused. If the supervisor dies, every connect() fails.

The environment is an allow-list: a few basics plus the ``--keep-env`` names.
Exit status 125 means the room could not be built and the program never ran.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import ipaddress
import json
import os
import resource
import signal
import socket
import struct
import sys
import threading

EXIT_ROOM_UNAVAILABLE = 125
MIN_LANDLOCK_ABI = 4

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_SYS_PIDFD_GETFD = 438
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_RULE_NET_PORT = 2
_PR_SET_NO_NEW_PRIVS = 38
_MSG_FASTOPEN = 0x20000000

FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1, 2, 4, 8
FS_REFER, FS_TRUNCATE, FS_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
FS_MAKE_SPECIAL = (
  (1 << 6) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12)
)  # char, sock, fifo, block, sym
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1, 2
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
READ_EXEC = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR

SYSTEM_READ_PATHS = (
  "/usr",
  "/bin",
  "/sbin",
  "/lib",
  "/lib32",
  "/lib64",
  "/etc",
  "/opt",
  "/proc",
  "/sys",
)
BASE_ENV = (
  "PATH",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "TERM",
  "TZ",
  "USER",
  "LOGNAME",
  "SHELL",
)


class RoomUnavailable(RuntimeError):
  """The kernel or libraries cannot build the room on this host."""


class _RulesetAttr(ctypes.Structure):
  _fields_ = [
    ("fs", ctypes.c_uint64),
    ("net", ctypes.c_uint64),
    ("scoped", ctypes.c_uint64),
  ]


class _PathBeneath(ctypes.Structure):
  _pack_ = 1
  _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _NetPort(ctypes.Structure):
  _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


class _ArgCmp(ctypes.Structure):
  _fields_ = [
    ("arg", ctypes.c_uint),
    ("op", ctypes.c_int),
    ("datum_a", ctypes.c_uint64),
    ("datum_b", ctypes.c_uint64),
  ]


class _SeccompData(ctypes.Structure):
  _fields_ = [
    ("nr", ctypes.c_int),
    ("arch", ctypes.c_uint32),
    ("ip", ctypes.c_uint64),
    ("args", ctypes.c_uint64 * 6),
  ]


class _Notif(ctypes.Structure):
  _fields_ = [
    ("id", ctypes.c_uint64),
    ("pid", ctypes.c_uint32),
    ("flags", ctypes.c_uint32),
    ("data", _SeccompData),
  ]


class _NotifResp(ctypes.Structure):
  _fields_ = [
    ("id", ctypes.c_uint64),
    ("val", ctypes.c_int64),
    ("error", ctypes.c_int32),
    ("flags", ctypes.c_uint32),
  ]


def _iowr(nr: int, size: int) -> int:
  return (3 << 30) | (size << 16) | (ord("!") << 8) | nr


_NOTIF_RECV = _iowr(0, ctypes.sizeof(_Notif))
_NOTIF_SEND = _iowr(1, ctypes.sizeof(_NotifResp))
_NOTIF_ID_VALID = (1 << 30) | (8 << 16) | (ord("!") << 8) | 2
_libc = ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
  version = _libc.syscall(
    _SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1)
  )
  return version if version > 0 else 0


def _seccomp_library():
  try:
    lib = ctypes.CDLL(ctypes.util.find_library("seccomp") or "libseccomp.so.2")
  except OSError:
    return None
  lib.seccomp_init.restype = ctypes.c_void_p
  lib.seccomp_init.argtypes = [ctypes.c_uint32]
  lib.seccomp_rule_add_array.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.c_uint,
    ctypes.POINTER(_ArgCmp),
  ]
  lib.seccomp_load.argtypes = [ctypes.c_void_p]
  lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
  lib.seccomp_notify_fd.argtypes = [ctypes.c_void_p]
  return lib


def availability() -> dict:
  abi, seccomp = landlock_abi(), _seccomp_library() is not None
  reasons = []
  if abi < MIN_LANDLOCK_ABI:
    reasons.append(
      f"Landlock ABI {abi} is below {MIN_LANDLOCK_ABI} (needs Linux 6.7 or later)."
    )
  if not seccomp:
    reasons.append("libseccomp is not installed.")
  return {
    "available": not reasons,
    "landlock_abi": abi,
    "seccomp": seccomp,
    "reasons": reasons,
  }


def normal_host(host: str) -> str:
  try:
    ip = ipaddress.ip_address(host)
  except ValueError:
    return host
  if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
    return str(ip.ipv4_mapped)
  return str(ip)


def _parse_sockaddr(raw: bytes) -> tuple[str | None, int | None]:
  family = struct.unpack_from("H", raw, 0)[0] if len(raw) >= 2 else None
  if family == socket.AF_INET and len(raw) >= 8:
    return socket.inet_ntop(socket.AF_INET, raw[4:8]), struct.unpack_from("!H", raw, 2)[
      0
    ]
  if family == socket.AF_INET6 and len(raw) >= 24:
    return socket.inet_ntop(socket.AF_INET6, raw[8:24]), struct.unpack_from(
      "!H", raw, 2
    )[0]
  return None, None


# --- walls and filters (applied in the child, before exec) ---------------------


def _add_path(ruleset: int, path: str, access: int, handled: int) -> None:
  try:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
  except FileNotFoundError:
    return
  try:
    if not os.path.isdir(path):
      access &= FILE_RIGHTS
    if (
      _libc.syscall(
        _SYS_LANDLOCK_ADD_RULE,
        ruleset,
        _LANDLOCK_RULE_PATH_BENEATH,
        ctypes.byref(_PathBeneath(access & handled, fd)),
        0,
      )
      != 0
    ):
      raise RoomUnavailable(
        f"landlock rule for {path} failed: errno {ctypes.get_errno()}"
      )
  finally:
    os.close(fd)


def _apply_walls(
  *, home: str, rw: list[str], ro: list[str], ports: set[int], abi: int
) -> None:
  handled = ((1 << 13) - 1) | FS_REFER | FS_TRUNCATE | (FS_IOCTL_DEV if abi >= 5 else 0)
  attr = _RulesetAttr(
    handled,
    NET_BIND_TCP | NET_CONNECT_TCP,
    (SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL) if abi >= 6 else 0,
  )
  size = ctypes.sizeof(attr) if abi >= 6 else 16
  ruleset = _libc.syscall(
    _SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), 0
  )
  if ruleset < 0:
    raise RoomUnavailable(f"landlock_create_ruleset failed: errno {ctypes.get_errno()}")
  try:
    for path in (*SYSTEM_READ_PATHS, *ro):
      _add_path(ruleset, path, READ_EXEC, handled)
    _add_path(
      ruleset,
      "/dev",
      FS_READ_FILE | FS_WRITE_FILE | FS_READ_DIR | FS_IOCTL_DEV,
      handled,
    )
    _add_path(ruleset, home, handled, handled)
    for path in rw:
      _add_path(ruleset, path, handled & ~FS_MAKE_SPECIAL, handled)
    for port in sorted(ports):
      if (
        _libc.syscall(
          _SYS_LANDLOCK_ADD_RULE,
          ruleset,
          _LANDLOCK_RULE_NET_PORT,
          ctypes.byref(_NetPort(NET_CONNECT_TCP, port)),
          0,
        )
        != 0
      ):
        raise RoomUnavailable(f"landlock port rule {port} failed")
    if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
      raise RoomUnavailable("prctl(no_new_privs) failed")
    if _libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
      raise RoomUnavailable(
        f"landlock_restrict_self failed: errno {ctypes.get_errno()}"
      )
  finally:
    os.close(ruleset)


def _apply_seccomp() -> int:
  """Refuse the ways around the doors, then hand every connect() to the guard."""
  lib = _seccomp_library()
  if lib is None:
    raise RoomUnavailable("libseccomp is not installed")
  resolve = lambda name: lib.seccomp_syscall_resolve_name(name.encode())  # noqa: E731
  deny, eq, masked_eq = 0x00050000 | errno.EPERM, 4, 7
  ctx = lib.seccomp_init(0x7FFF0000)

  def rule(name: str, *conds: _ArgCmp) -> None:
    arr = (_ArgCmp * len(conds))(*conds) if conds else None
    if lib.seccomp_rule_add_array(ctx, deny, resolve(name), len(conds), arr) != 0:
      raise RoomUnavailable(f"seccomp rule {name} failed")

  rule("socket", _ArgCmp(0, eq, socket.AF_UNIX, 0))
  for family in (socket.AF_INET, socket.AF_INET6):
    rule(
      "socket", _ArgCmp(0, eq, family, 0), _ArgCmp(1, masked_eq, 0xF, socket.SOCK_DGRAM)
    )
  for name in (
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
  ):
    rule(name)
  for name, arg in (("sendto", 3), ("sendmsg", 2)):
    rule(name, _ArgCmp(arg, masked_eq, _MSG_FASTOPEN, _MSG_FASTOPEN))
  if lib.seccomp_load(ctx) != 0:
    raise RoomUnavailable("seccomp_load failed")
  notify = lib.seccomp_init(0x7FFF0000)
  if (
    lib.seccomp_rule_add_array(notify, 0x7FC00000, resolve("connect"), 0, None) != 0
    or lib.seccomp_load(notify) != 0
  ):
    raise RoomUnavailable("seccomp notify filter failed")
  listener = lib.seccomp_notify_fd(notify)
  if listener < 0:
    raise RoomUnavailable("no seccomp notification listener")
  return listener


# --- the supervisor (outside the room) ------------------------------------------


def _guard(listener: int, doors: set[tuple[str, int]]) -> None:
  """Answer every connect() from the room: an exact door, or EACCES."""
  notif, resp = _Notif(), _NotifResp()
  while True:
    ctypes.memset(ctypes.byref(notif), 0, ctypes.sizeof(notif))
    if _libc.ioctl(listener, _NOTIF_RECV, ctypes.byref(notif)) != 0:
      if ctypes.get_errno() == errno.EINTR:
        continue
      return
    fd, ptr, length = (int(notif.data.args[i]) for i in range(3))
    error = -errno.EACCES
    try:
      with open(f"/proc/{notif.pid}/mem", "rb", buffering=0) as mem:
        host, port = _parse_sockaddr(os.pread(mem.fileno(), min(length, 128), ptr))
      with open(f"/proc/{notif.pid}/status") as status:
        tgid = int(next(line for line in status if line.startswith("Tgid:")).split()[1])
      # The request must still be pending, or the memory read may describe
      # another program reusing the thread id.
      if (
        _libc.ioctl(listener, _NOTIF_ID_VALID, ctypes.byref(ctypes.c_uint64(notif.id)))
        != 0
      ):
        continue
      if host is not None and (normal_host(host), port) in doors:
        pidfd = os.pidfd_open(tgid)
        local = _libc.syscall(_SYS_PIDFD_GETFD, pidfd, fd, 0)
        os.close(pidfd)
        if local >= 0:
          sock = socket.socket(fileno=local)
          error = -sock.connect_ex((host, port))
          sock.detach()
          os.close(local)
    except (OSError, StopIteration, ValueError):
      pass
    resp.id, resp.val, resp.error, resp.flags = notif.id, 0, error, 0
    _libc.ioctl(listener, _NOTIF_SEND, ctypes.byref(resp))


def room_environment(home: str, keep: list[str]) -> dict[str, str]:
  names = set(BASE_ENV) | set(keep)
  env = {k: v for k, v in os.environ.items() if k in names}
  scratch = os.path.join(home, ".tmp")
  env.update(
    {
      "TMPDIR": scratch,
      "TMP": scratch,
      "TEMP": scratch,
      "HOME": home,
      "MOBIUS_ROOM": "1",
    }
  )
  return env


def run_in_room(
  command: list[str],
  *,
  home: str,
  rw: list[str] = (),
  ro: list[str] = (),
  cwd: str | None = None,
  doors: set[tuple[str, int]] = frozenset(),
  keep_env: list[str] = (),
) -> int:
  """Start ``command`` in the room and supervise its doors until it exits."""
  state = availability()
  paths = [os.path.realpath(p) for p in (home, *rw, *ro)]
  missing = [p for p in paths if not os.path.isdir(p)]
  if not state["available"] or missing:
    reason = " ".join(state["reasons"]) or f"room folder {missing[0]} does not exist"
    print(f"mobius-fence: refused to run, room unavailable: {reason}", file=sys.stderr)
    return EXIT_ROOM_UNAVAILABLE
  home = paths[0]
  os.makedirs(os.path.join(home, ".tmp"), exist_ok=True)
  doors = {(normal_host(h), int(p)) for h, p in doors}
  parent_end, child_end = socket.socketpair()
  pid = os.fork()
  if pid == 0:
    try:
      parent_end.close()
      _apply_walls(
        home=home,
        rw=list(map(os.path.realpath, rw)),
        ro=list(map(os.path.realpath, ro)),
        ports={p for _, p in doors},
        abi=state["landlock_abi"],
      )
      listener = _apply_seccomp()
      socket.send_fds(child_end, [b"x"], [listener])
      os.closerange(3, max(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 4096))
      os.chdir(os.path.realpath(cwd) if cwd else home)
      os.execvpe(command[0], command, room_environment(home, list(keep_env)))
    except (RoomUnavailable, OSError) as exc:
      print(f"mobius-fence: refused to run, room unavailable: {exc}", file=sys.stderr)
    os._exit(EXIT_ROOM_UNAVAILABLE)
  child_end.close()
  try:
    _msg, fds, _flags, _addr = socket.recv_fds(parent_end, 1, 1)
  except OSError:
    fds = []
  parent_end.close()
  if not fds:
    _, status = os.waitpid(pid, 0)
    return (
      os.waitstatus_to_exitcode(status)
      if os.WIFEXITED(status)
      else EXIT_ROOM_UNAVAILABLE
    )
  devnull = os.open(
    os.devnull, os.O_RDWR
  )  # the supervisor must not hold the program's pipes
  os.dup2(devnull, 0)
  os.dup2(devnull, 1)
  os.close(devnull)
  threading.Thread(target=_guard, args=(fds[0], doors), daemon=True).start()
  for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, lambda s, _f: os.kill(pid, s))
  while True:
    try:
      _, status = os.waitpid(pid, 0)
      break
    except InterruptedError:
      continue
  code = os.waitstatus_to_exitcode(status)
  return 128 - code if code < 0 else code


def _host_port(value: str) -> tuple[str, int]:
  host, _, port = value.rpartition(":")
  return host.strip("[]"), int(port)


def main(argv: list[str]) -> int:
  split = argv.index("--") if "--" in argv else len(argv)
  own, command = argv[:split], argv[split + 1 :]
  parser = argparse.ArgumentParser(prog="mobius-fence")
  parser.add_argument("--home")
  parser.add_argument("--rw", action="append", default=[])
  parser.add_argument("--ro", action="append", default=[])
  parser.add_argument("--cwd")
  parser.add_argument("--door", action="append", default=[], type=_host_port)
  parser.add_argument("--keep-env", action="append", default=[])
  parser.add_argument("--check", action="store_true")
  args = parser.parse_args(own)
  if args.check:
    print(json.dumps(availability()))
    return 0
  if not args.home or not command:
    parser.error("--home and a command after -- are required")
  return run_in_room(
    command,
    home=args.home,
    rw=args.rw,
    ro=args.ro,
    cwd=args.cwd,
    doors=set(args.door),
    keep_env=args.keep_env,
  )


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
