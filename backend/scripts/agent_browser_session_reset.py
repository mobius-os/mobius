#!/usr/bin/env python3
"""Reset one poisoned agent-browser session without touching other sessions.

An agent-browser client can be killed while its daemon is still evaluating a
page command. Later clients then queue behind that unfinished command, so even
``agent-browser close`` cannot recover the session. The browser profile is the
stable ownership boundary: Chromium exposes it as an exact ``--user-data-dir``
argument. Its daemon preserves the same exact ``AGENT_BROWSER_PROFILE`` value
in its environment, including after Chromium is reparented or exits.

This helper terminates only that validated browser/daemon pair. It deliberately
does not match process command lines by substring and never targets another
profile or every agent-browser session.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import sys
import time


PROC_ROOT = Path("/proc")


class SessionResetError(RuntimeError):
  """The requested profile could not be mapped to one safe process pair."""


@dataclass(frozen=True)
class ProcessIdentity:
  pid: int
  start_ticks: int


@dataclass(frozen=True)
class SessionProcesses:
  browser: ProcessIdentity | None
  daemon: ProcessIdentity | None


def _process_state(pid: int, proc_root: Path = PROC_ROOT) -> tuple[int, int]:
  raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
  # comm can contain spaces and parentheses. Everything after the final ") "
  # begins at proc(5) field 3: state, ppid, ... starttime (field 22).
  fields = raw.rsplit(") ", 1)[1].split()
  return int(fields[1]), int(fields[19])


def _cmdline(pid: int, proc_root: Path = PROC_ROOT) -> tuple[str, ...]:
  raw = (proc_root / str(pid) / "cmdline").read_bytes()
  return tuple(
    part.decode("utf-8", errors="replace")
    for part in raw.split(b"\0")
    if part
  )


def _environ(pid: int, proc_root: Path = PROC_ROOT) -> dict[bytes, bytes]:
  values: dict[bytes, bytes] = {}
  for raw in (proc_root / str(pid) / "environ").read_bytes().split(b"\0"):
    key, separator, value = raw.partition(b"=")
    if separator:
      values[key] = value
  return values


def _identity(pid: int, proc_root: Path = PROC_ROOT) -> ProcessIdentity:
  _, start_ticks = _process_state(pid, proc_root)
  return ProcessIdentity(pid=pid, start_ticks=start_ticks)


def _profile_processes(profile: str, proc_root: Path = PROC_ROOT) -> tuple[int, ...]:
  profile_arg = f"--user-data-dir={os.path.abspath(profile)}"
  matches: list[int] = []
  for entry in proc_root.iterdir():
    if not entry.name.isdigit():
      continue
    try:
      if profile_arg in _cmdline(int(entry.name), proc_root):
        matches.append(int(entry.name))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
      continue
  return tuple(matches)


def find_session_processes(
  profile: str,
  proc_root: Path = PROC_ROOT,
) -> SessionProcesses | None:
  """Return the one validated daemon/browser pair that owns ``profile``."""

  profile_arg = f"--user-data-dir={os.path.abspath(profile)}"
  browsers: list[ProcessIdentity] = []
  daemons: list[ProcessIdentity] = []
  for entry in proc_root.iterdir():
    if not entry.name.isdigit():
      continue
    pid = int(entry.name)
    try:
      args = _cmdline(pid, proc_root)
      if not args:
        continue
      executable = Path(args[0]).name.lower()
      if (
        profile_arg in args
        and "chrome" in executable
        and not any(arg.startswith("--type=") for arg in args)
      ):
        browsers.append(_identity(pid, proc_root))
      elif executable.startswith("agent-browser"):
        environment = _environ(pid, proc_root)
        if environment.get(b"AGENT_BROWSER_PROFILE", b"").decode(
          "utf-8", errors="surrogateescape",
        ) == os.path.abspath(profile):
          daemons.append(_identity(pid, proc_root))
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
      continue

  if not browsers and not daemons:
    return None
  if len(browsers) > 1 or len(daemons) > 1:
    raise SessionResetError(
      "profile ownership is ambiguous "
      f"({len(browsers)} browser roots, {len(daemons)} daemons); refusing to guess"
    )
  return SessionProcesses(
    browser=browsers[0] if browsers else None,
    daemon=daemons[0] if daemons else None,
  )


def _still_same_process(
  process: ProcessIdentity,
  proc_root: Path = PROC_ROOT,
) -> bool:
  try:
    return _identity(process.pid, proc_root) == process
  except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
    return False


def terminate_session_processes(
  processes: SessionProcesses,
  *,
  wait_seconds: float = 1.0,
  proc_root: Path = PROC_ROOT,
) -> None:
  """Hard-stop the exact poisoned pair, guarding against PID reuse."""

  # Stop the supervisor first so it cannot launch a replacement Chrome during
  # shutdown. The browser identity was captured before that reparenting and is
  # guarded by its start time, so it remains safe to terminate second.
  ordered = tuple(
    process
    for process in (processes.daemon, processes.browser)
    if process is not None and _still_same_process(process, proc_root)
  )
  if not ordered:
    return
  # The daemon RPC path is poisoned at this boundary. A graceful signal can let
  # its supervisor launch a replacement Chrome during shutdown, recreating the
  # profile race. Kill only the identities verified above, then prove they
  # disappeared.
  for process in ordered:
    try:
      os.kill(process.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass

  deadline = time.monotonic() + wait_seconds
  while time.monotonic() < deadline:
    if not any(_still_same_process(process, proc_root) for process in ordered):
      return
    time.sleep(0.05)
  raise SessionResetError("browser session processes did not exit after SIGKILL")


def reset_profile(profile: str) -> bool:
  """Reset the active session for ``profile``; return whether one existed."""

  processes = find_session_processes(profile)
  if processes is None:
    return False
  terminate_session_processes(processes)
  # Chromium's renderers can outlive their root for a fraction of a second.
  # Starting a replacement during that handoff races the profile singleton and
  # correctly makes Chrome refuse the launch. Require the exact profile to be
  # quiet before the caller removes stale singleton symlinks and restarts.
  deadline = time.monotonic() + 2.0
  while time.monotonic() < deadline:
    if not _profile_processes(profile):
      return True
    time.sleep(0.05)
  raise SessionResetError("browser profile remained active after session reset")


def main(argv: list[str]) -> int:
  if len(argv) != 2:
    print(f"Usage: {Path(argv[0]).name} <agent-browser-profile>", file=sys.stderr)
    return 2
  try:
    reset_profile(argv[1])
  except SessionResetError as exc:
    print(f"agent-browser session reset: {exc}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv))
