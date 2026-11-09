#!/usr/bin/env python3
"""Run the container boot, and rerun it in place for a planned restart.

A planned restart used to end the container and rely on the host to start it
again. That made every restart depend on the host's restart policy: Railway's
default ``ON_FAILURE`` never revives the clean exit a planned stop reports, so
an instance whose service was not explicitly set to ``ALWAYS`` stayed down
until someone redeployed it. A restart that never leaves the container works
on every host and restart setting, and reports no stop to the host at all.

This frozen, root-owned process sits between ``tini`` and ``entrypoint.sh``:

* ``SIGUSR1`` is the planned-restart request. Only root can send it to this
  process; the entrypoint's restart poller sends it after the restart ledger
  has accepted the exact authenticated request. The supervisor stops the
  current boot, clears every process that boot left behind, and runs the same
  command again, which begins a fresh boot exactly as a new container would.
* Stop signals (``SIGTERM``, ``SIGINT``, ``SIGHUP``, ``SIGQUIT``) belong to the
  host. They are forwarded, and the supervisor then exits with the boot's own
  status, so ``tini -e 143`` still reports a host stop as a clean exit.
* Any other end of the boot (a crash, a failed start, the restart path's
  ``SIGKILL`` backstop) is never retried here. It ends the container with its
  real status, so the host's own restart policy keeps owning crash recovery and
  an unexpected crash can never look like a planned restart.

The supervisor is a child subreaper: processes the boot orphans reparent to it
rather than escaping to the outer init, so "everything the old boot left
behind" is exactly this process's descendants. That keeps the cleanup inside
the boot's own process tree even if the container shares a host PID namespace.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from pathlib import Path

PLANNED_RESTART_SIGNAL = signal.SIGUSR1
HOST_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
# The entrypoint's restart poller reads this to find the process it signals.
SUPERVISOR_PID_ENV = "MOBIUS_SUPERVISOR_PID"
# Leftovers get a polite SIGTERM, then SIGKILL. Uvicorn's own graceful window
# has already passed by the time the boot's main process exits.
LEFTOVER_TERM_GRACE_SECONDS = 5.0
LEFTOVER_KILL_GRACE_SECONDS = 5.0
_PR_SET_CHILD_SUBREAPER = 36
_PYTHON_IGNORED_SIGNALS = (signal.SIGPIPE, signal.SIGXFSZ)
_PROC = Path("/proc")


def _log(message: str) -> None:
  print(f"container-supervisor: {message}", file=sys.stderr, flush=True)


def _become_child_subreaper() -> None:
  libc = ctypes.CDLL(None, use_errno=True)
  if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def _parent_pids() -> dict[int, int]:
  """Map every live, non-zombie process to its parent."""
  parents: dict[int, int] = {}
  for entry in _PROC.iterdir():
    if not entry.name.isdigit():
      continue
    try:
      stat = (entry / "stat").read_text()
    except OSError:
      continue
    # ``comm`` may contain spaces or parentheses; the fields after its last
    # closing parenthesis are fixed: state, ppid, ...
    fields = stat[stat.rfind(")") + 2:].split()
    if len(fields) < 2 or fields[0] == "Z":
      continue
    parents[int(entry.name)] = int(fields[1])
  return parents


def _descendants(root: int) -> list[int]:
  parents = _parent_pids()
  found: list[int] = []
  for pid in parents:
    ancestor = parents.get(pid)
    seen = {pid}
    while ancestor is not None and ancestor not in seen:
      if ancestor == root:
        found.append(pid)
        break
      seen.add(ancestor)
      ancestor = parents.get(ancestor)
  return found


def _reap_finished_children() -> None:
  while True:
    try:
      pid, _ = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
      return
    if pid == 0:
      return


def clear_leftover_processes() -> list[int]:
  """Stop every descendant of this supervisor; return any that survived."""
  me = os.getpid()
  for sig, grace in (
    (signal.SIGTERM, LEFTOVER_TERM_GRACE_SECONDS),
    (signal.SIGKILL, LEFTOVER_KILL_GRACE_SECONDS),
  ):
    remaining = _descendants(me)
    if not remaining:
      return []
    for pid in remaining:
      try:
        os.kill(pid, sig)
      except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
      _reap_finished_children()
      if not _descendants(me):
        return []
      time.sleep(0.05)
  _reap_finished_children()
  return _descendants(me)


def exit_status(wait_status: int) -> int:
  """Shell-style status: a signal death becomes ``128 + signal``."""
  if os.WIFSIGNALED(wait_status):
    return 128 + os.WTERMSIG(wait_status)
  return os.waitstatus_to_exitcode(wait_status)


class Supervisor:
  def __init__(self, command: list[str]) -> None:
    self.command = command
    self.child: int | None = None
    self.restart_requested = False
    self.stop_signal: int | None = None

  def _on_planned_restart(self, signum, frame) -> None:
    self.restart_requested = True
    self._signal_child(signal.SIGTERM)

  def _on_host_stop(self, signum, frame) -> None:
    self.stop_signal = signum
    self._signal_child(signum)

  def _signal_child(self, signum: int) -> None:
    if self.child is None:
      return
    try:
      os.kill(self.child, signum)
    except ProcessLookupError:
      pass

  def _start_boot(self) -> None:
    env = dict(os.environ)
    env[SUPERVISOR_PID_ENV] = str(os.getpid())
    # Python ignores SIGPIPE and SIGXFSZ at startup; the boot must start with
    # the same default signal handling it had directly under tini.
    self.child = os.posix_spawnp(
      self.command[0], self.command, env,
      setsigdef=_PYTHON_IGNORED_SIGNALS, setsigmask=(),
    )

  def _wait_for_boot(self) -> int:
    # As a subreaper this process also inherits the boot's orphans; reap them
    # while waiting so they never accumulate as zombies.
    while True:
      pid, status = os.waitpid(-1, 0)
      if pid == self.child:
        self.child = None
        return status

  def run(self) -> int:
    _become_child_subreaper()
    signal.signal(PLANNED_RESTART_SIGNAL, self._on_planned_restart)
    for signum in HOST_STOP_SIGNALS:
      signal.signal(signum, self._on_host_stop)
    while True:
      self.restart_requested = False
      if self.stop_signal is not None:
        return 128 + self.stop_signal
      self._start_boot()
      status = self._wait_for_boot()
      if not self.restart_requested or self.stop_signal is not None:
        return exit_status(status)
      survivors = clear_leftover_processes()
      if survivors:
        # Rebooting beside processes that ignore SIGKILL could double-start
        # services; end the container and let the host replace it instead.
        _log(f"{len(survivors)} process(es) survived cleanup; stopping the container.")
        return 1
      if self.stop_signal is not None:
        return 128 + self.stop_signal
      _log("planned restart: starting a fresh boot in this container.")


def main(argv: list[str]) -> int:
  if len(argv) < 2:
    _log("usage: container_supervisor.py COMMAND [ARG...]")
    return 2
  return Supervisor(argv[1:]).run()


if __name__ == "__main__":
  sys.exit(main(sys.argv))
