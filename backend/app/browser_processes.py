"""Exact, dependency-free browser ownership shared by capture and turn teardown.

No process-name substring kills or process-group guesses. Inventory includes
orphan Chrome and helpers, not only RPC daemons. Every signal revalidates the
PID start time; Linux pidfds bind delivery to that same process incarnation.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import time

PROC_ROOT = Path('/proc')
DAEMONS = frozenset({'agent-browser-linux-x64', 'agent-browser-linux-arm64'})
BROWSERS = frozenset({'chrome', 'chromium', 'chromium-browser',
                     'chrome_crashpad_handler', 'crashpad_handler'})


class SessionResetError(RuntimeError):
  """Cleanup could not establish or release exact ownership."""


@dataclass(frozen=True)
class ProcessIdentity:
  pid: int
  start_ticks: int


@dataclass(frozen=True)
class BrowserSessionTarget:
  session: str
  namespace: str | None = None
  socket_dir: str | None = None


@dataclass(frozen=True)
class BrowserSessionScan:
  targets: frozenset[BrowserSessionTarget]
  complete: bool
  processes: tuple[ProcessIdentity, ...] = ()

  @property
  def idle(self) -> bool:
    return self.complete and not self.targets and not self.processes


def _process_state(pid: int, proc_root: Path = PROC_ROOT) -> tuple[int, int]:
  fields = (proc_root / str(pid) / 'stat').read_text().rsplit(') ', 1)[1].split()
  if fields[0] == 'Z':
    raise ProcessLookupError(pid)
  return int(fields[1]), int(fields[19])


def _identity(pid: int, proc_root: Path = PROC_ROOT) -> ProcessIdentity:
  return ProcessIdentity(pid, _process_state(pid, proc_root)[1])


def _still_same_process(process: ProcessIdentity, proc_root: Path = PROC_ROOT) -> bool:
  try:
    return _identity(process.pid, proc_root) == process
  except (FileNotFoundError, ProcessLookupError):
    return False


def _profile_arg(args: tuple[str, ...]) -> str | None:
  for i, arg in enumerate(args):
    if arg.startswith('--user-data-dir='):
      return os.path.abspath(arg.split('=', 1)[1])
    if arg == '--user-data-dir' and i + 1 < len(args):
      return os.path.abspath(args[i + 1])
  return None


def scan_browser_processes(*, chat_id: str | None = None,
                           profile: str | None = None,
                           proc_root: Path = PROC_ROOT) -> BrowserSessionScan:
  """Select exact chat/profile owners and their browser descendants.

  Missing processes are normal races; unreadable or malformed candidates make
  the inventory incomplete. Chat ownership includes inherited environment so
  a double-forked crashpad remains attributable after its parent disappears.
  A foreign explicit CHAT_ID always wins over a coincidental profile match.
  """
  if not chat_id and not profile:
    return BrowserSessionScan(frozenset(), False)
  profile = os.path.abspath(profile) if profile else None
  try:
    entries = list(proc_root.iterdir())
  except OSError:
    return BrowserSessionScan(frozenset(), False)
  complete = True
  records = {}
  for entry in entries:
    if not entry.name.isdigit():
      continue
    pid = int(entry.name)
    try:
      parent, ticks = _process_state(pid, proc_root)
      args = tuple(x.decode('utf-8', errors='surrogateescape')
                   for x in (entry / 'cmdline').read_bytes().split(b'\0') if x)
      if not args:
        continue
      exe = Path(args[0]).name
      if exe not in DAEMONS | BROWSERS:
        continue
      env = {}
      for raw in (entry / 'environ').read_bytes().split(b'\0'):
        key, sep, value = raw.partition(b'=')
        if sep and key in (b'CHAT_ID', b'AGENT_BROWSER_PROFILE',
                           b'AGENT_BROWSER_SESSION', b'AGENT_BROWSER_NAMESPACE',
                           b'AGENT_BROWSER_SOCKET_DIR'):
          env[key.decode()] = value.decode('utf-8', errors='surrogateescape')
      # A PID recycled between reads is not a coherent ownership record.
      if _identity(pid, proc_root).start_ticks != ticks:
        complete = False
        continue
      records[pid] = (ProcessIdentity(pid, ticks), parent, exe, args, env)
    except (FileNotFoundError, ProcessLookupError):
      continue
    except (OSError, ValueError, IndexError):
      complete = False
  selected = set()
  foreign = set()
  for pid, (_, _, exe, args, env) in records.items():
    if chat_id and env.get('CHAT_ID') not in (None, '', chat_id):
      foreign.add(pid)
      continue
    if profile and not chat_id and (
      (_profile_arg(args) is not None and _profile_arg(args) != profile)
      or (env.get('AGENT_BROWSER_PROFILE') not in (None, '', profile))
    ):
      foreign.add(pid)
      continue
    profile_owned = profile and (
      _profile_arg(args) == profile or env.get('AGENT_BROWSER_PROFILE') == profile)
    if (chat_id and env.get('CHAT_ID') == chat_id) or profile_owned:
      selected.add(pid)
  # Preserve exact ancestry when children don't retain identifying environment.
  while True:
    # A PPID snapshot can outlive its parent while /proc is being scanned.
    # Do not attach an older helper to a newly recycled, now-owned parent PID.
    children = {pid for pid, (identity, parent, _, _, _) in records.items()
                if parent in selected and pid not in foreign
                and identity.start_ticks >= records[parent][0].start_ticks}
    if children <= selected:
      break
    selected.update(children)
  if profile and not chat_id:
    roots = [pid for pid in selected if records[pid][2] in BROWSERS
             and _profile_arg(records[pid][3]) == profile
             and not any(a.startswith('--type=') for a in records[pid][3])]
    daemons = [pid for pid in selected if records[pid][2] in DAEMONS]
    if len(roots) > 1 or len(daemons) > 1:
      raise SessionResetError('profile ownership is ambiguous; refusing to guess')
  targets = set()
  for pid in selected:
    _, _, exe, _, env = records[pid]
    if exe in DAEMONS and 'AGENT_BROWSER_SESSION' in env:
      targets.add(BrowserSessionTarget(env['AGENT_BROWSER_SESSION'],
                                      env.get('AGENT_BROWSER_NAMESPACE'),
                                      env.get('AGENT_BROWSER_SOCKET_DIR')))
  # Supervisors first, then Chrome roots/helpers. No detached successor can be
  # targeted by a later scan once the owner has handed off the chat lock.
  ordered = sorted(selected, key=lambda pid: (records[pid][2] not in DAEMONS, pid))
  return BrowserSessionScan(frozenset(targets), complete,
                            tuple(records[pid][0] for pid in ordered))


def terminate_processes(processes: tuple[ProcessIdentity, ...], *,
                        wait_seconds: float = 1.0,
                        proc_root: Path = PROC_ROOT) -> None:
  for process in processes:
    fd = None
    try:
      if proc_root == PROC_ROOT and hasattr(os, 'pidfd_open'):
        fd = os.pidfd_open(process.pid)
      if not _still_same_process(process, proc_root):
        continue
      if fd is not None:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
      else:
        os.kill(process.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    finally:
      if fd is not None:
        os.close(fd)
  deadline = time.monotonic() + wait_seconds
  while True:
    if not any(_still_same_process(p, proc_root) for p in processes):
      return
    if time.monotonic() >= deadline:
      raise SessionResetError('browser processes did not exit after SIGKILL')
    time.sleep(0.05)


def reset_browser_processes(*, chat_id: str | None = None,
                            profile: str | None = None) -> bool:
  """Bounded exact-owner cleanup, including helpers surviving root exit."""
  existed = False
  for _ in range(3):
    scan = scan_browser_processes(chat_id=chat_id, profile=profile)
    if not scan.complete:
      raise SessionResetError('browser process ownership inventory is incomplete')
    if not scan.processes:
      return existed
    existed = True
    terminate_processes(scan.processes)
  scan = scan_browser_processes(chat_id=chat_id, profile=profile)
  if not scan.idle:
    raise SessionResetError('browser ownership remained active after reset')
  return existed


def reset_profile(profile: str) -> bool:
  return reset_browser_processes(profile=profile)
