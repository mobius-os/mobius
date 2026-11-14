import logging
import signal
from uuid import uuid4

from app import process_groups


def test_isolated_process_group_id_refuses_shared_group(monkeypatch):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4000)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 4000)

  assert process_groups.isolated_process_group_id(4321) is None


def test_lower_process_group_priority_targets_proven_private_group(monkeypatch):
  calls = []
  monkeypatch.setattr(process_groups.os, "getpgid", lambda pid: pid)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda which, who, priority: calls.append((which, who, priority)),
  )

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is True
  assert calls == [
    (
      process_groups.os.PRIO_PGRP,
      4321,
      process_groups.BACKGROUND_PROCESS_NICE,
    ),
  ]


def test_lower_process_group_priority_refuses_unverified_group(monkeypatch):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4000)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda *_args: (_ for _ in ()).throw(
      AssertionError("unverified group must not be adjusted"),
    ),
  )

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is False


def test_lower_process_group_priority_refuses_missing_group(monkeypatch):
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda *_args: (_ for _ in ()).throw(
      AssertionError("missing group must not be adjusted"),
    ),
  )

  assert process_groups.lower_process_group_priority(
    None,
    logger=logging.getLogger(__name__),
    label="test",
  ) is False


def test_lower_process_group_priority_is_nonfatal(monkeypatch, caplog):
  monkeypatch.setattr(process_groups.os, "getpgid", lambda pid: pid)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "setpriority",
    lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
  )

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is False
  assert "test priority adjustment failed pgid=4321: denied" in caplog.text


def test_terminate_process_group_has_sigkill_backstop(monkeypatch):
  calls = []
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(
    process_groups.os,
    "killpg",
    lambda pgid, sig: calls.append((pgid, sig)),
  )

  assert process_groups.terminate_process_group(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
    grace_seconds=0,
  ) is True
  assert calls == [
    (4321, signal.SIGTERM),
    (4321, signal.SIGKILL),
  ]


def test_background_group_is_preferred_oom_victim(monkeypatch, tmp_path):
  """An OOM event must cost the agent that grew, never the server."""
  written = []
  monkeypatch.setattr(process_groups.os, "getpgid", lambda pid: 4321)
  monkeypatch.setattr(process_groups.os, "getpgrp", lambda: 9999)
  monkeypatch.setattr(process_groups.os, "setpriority", lambda *_args: None)
  monkeypatch.setattr(process_groups.os, "listdir", lambda _path: ["4321", "4322", "self"])

  real_open = open

  def fake_open(path, mode="r", *args, **kwargs):
    if str(path).startswith("/proc/") and str(path).endswith("/oom_score_adj"):
      written.append(str(path))
      return real_open(tmp_path / "sink", "w")
    return real_open(path, mode, *args, **kwargs)

  monkeypatch.setattr("builtins.open", fake_open)

  assert process_groups.lower_process_group_priority(
    4321,
    logger=logging.getLogger(__name__),
    label="test",
  ) is True
  assert written == ["/proc/4321/oom_score_adj", "/proc/4322/oom_score_adj"]
  assert process_groups.AGENT_OOM_SCORE_ADJ == 1000


def test_an_exited_member_is_skipped_not_reported(caplog):
  """Every helper host logged "OOM preference failed … Permission denied":
  Claude Code's short startup commands had exited but were not yet reaped,
  and the kernel gives such a process's /proc files to root. The live group
  still became the preferred victim; the warning was a false alarm."""
  import os
  import subprocess
  import time

  leader = subprocess.Popen(["sh", "-c", "true & exec sleep 30"], start_new_session=True)
  try:
    def child_state():
      for pid in process_groups._process_group_members(leader.pid):
        if pid != leader.pid:
          with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0]
      return None

    # The child appears while `true` may still be running; wait for it to exit
    # into the unreaped state this case is about.
    deadline = time.monotonic() + 5
    while (state := child_state()) != "Z":
      assert time.monotonic() < deadline, f"the child never became an unreaped zombie ({state})"
      time.sleep(0.05)

    with caplog.at_level(logging.WARNING):
      assert process_groups.lower_process_group_priority(
        leader.pid, logger=logging.getLogger(__name__), label="test",
      ) is True

    assert "OOM preference failed" not in caplog.text
    with open(f"/proc/{leader.pid}/oom_score_adj") as handle:
      assert int(handle.read()) == process_groups.AGENT_OOM_SCORE_ADJ
    assert os.getpriority(os.PRIO_PROCESS, leader.pid) >= process_groups.BACKGROUND_PROCESS_NICE
  finally:
    leader.kill()
    leader.wait()


def _command_in_own_session(run_token):
  """A tool command as providers start it: its own session, the run's env."""
  import os
  import subprocess
  env = dict(os.environ, **{process_groups.RUN_MARKER_ENV: run_token})
  return subprocess.Popen(
    ["sleep", "60"], env=env, start_new_session=True,
  )


def _gone(proc, timeout=2.0):
  import subprocess
  try:
    proc.wait(timeout=timeout)
    return True
  except subprocess.TimeoutExpired:
    return False


def test_run_commands_outside_the_agent_group_are_ended():
  """An abruptly killed agent leaves commands in their own sessions; the
  inherited run marker still finds and ends them."""
  marker = f"owned-{uuid4().hex}"
  mine = _command_in_own_session(marker)
  try:
    assert process_groups.terminate_agent_processes(
      None,
      run_marker=marker,
      logger=logging.getLogger(__name__),
      label="test",
      grace_seconds=0.2,
    ) is True
    assert _gone(mine)
  finally:
    mine.kill()


def test_another_runs_commands_are_never_touched():
  other = _command_in_own_session(f"other-{uuid4().hex}")
  try:
    assert process_groups.terminate_run_processes(
      f"absent-{uuid4().hex}", logger=logging.getLogger(__name__), label="test",
    ) == 0
    assert other.poll() is None
  finally:
    other.kill()
    other.wait()


def test_no_run_marker_ends_nothing_beyond_the_group(monkeypatch):
  monkeypatch.setattr(
    process_groups, "run_owned_processes",
    lambda _token: (_ for _ in ()).throw(AssertionError("must not scan")),
  )
  assert process_groups.terminate_run_processes(
    None, logger=logging.getLogger(__name__), label="test",
  ) == 0


def test_a_reused_pid_is_not_signalled(monkeypatch):
  """Identity is re-checked at delivery: a PID reused by another process
  after the scan must not be signalled."""
  sent = []
  monkeypatch.setattr(process_groups, "run_owned_processes", lambda _t: [(4242, 7)])
  monkeypatch.setattr(process_groups, "_start_ticks", lambda _pid: 8)
  monkeypatch.setattr(process_groups.os, "kill", lambda *a: sent.append(a))
  monkeypatch.setattr(process_groups.os, "pidfd_open", lambda _pid: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
  assert process_groups.terminate_run_processes(
    "run-under-test", logger=logging.getLogger(__name__), label="test",
  ) == 0
  assert sent == []


def test_the_chats_browser_is_left_to_its_own_graceful_owner(tmp_path):
  """Turn teardown closes the browser so its profile flushes; the run sweep
  must not kill it first."""
  import os
  import subprocess
  import sys
  # The sweep recognises the browser by its executable name. Stand in with
  # Python, which runs under any name: multicall coreutils builds (uutils)
  # refuse to run `sleep` as `chrome`.
  chrome = tmp_path / "chrome"
  chrome.symlink_to(sys.executable)
  marker = f"browser-{uuid4().hex}"
  env = dict(os.environ, **{process_groups.RUN_MARKER_ENV: marker})
  browser = subprocess.Popen(
    [str(chrome), "-c", "import time; time.sleep(60)"], env=env, start_new_session=True,
  )
  try:
    assert process_groups.terminate_run_processes(
      marker, logger=logging.getLogger(__name__), label="test",
    ) == 0
    assert browser.poll() is None
  finally:
    browser.kill()
    browser.wait()


def test_run_marker_is_a_stable_one_way_digest():
  marker = process_groups.run_marker("secret-run-token")
  assert marker == process_groups.run_marker("secret-run-token")
  assert "secret-run-token" not in marker and len(marker) == 32
  assert process_groups.run_marker(None) == ""


# --- storage guard: stopping what fills the volume ---------------------------
#
# The real scan and real signals run against a small process tree shaped like
# a live turn: this test process stands in for the server, which launches the
# runtime as the leader of its own session; the runtime starts an MCP server
# in that session and each tool command in a session of its own.


def _scoped_to(monkeypatch, *pids):
  """Let stop_agent_commands see only these test processes.

  A live instance's own agent commands (this test run may share the
  container) are never in view.
  """
  ours = set(pids)
  real = process_groups._marked_processes
  monkeypatch.setattr(
    process_groups, "_marked_processes",
    lambda: {pid: row for pid, row in real().items() if pid in ours},
  )


# Runs the Python interpreter under each argv[0] rather than a renamed
# ``sleep``: multi-call coreutils builds refuse to run under a name they do
# not recognise.
_SLEEP = "import time; time.sleep(60)"
# The interpreter path is passed in: under a borrowed argv[0] such as
# ``node``, the runtime's own ``sys.executable`` resolves to that program.
_RUNTIME = f"""
import subprocess, sys, time
command_name, python = sys.argv[1:]
mcp = subprocess.Popen(["python3.12", "-c", {_SLEEP!r}], executable=python)
command = subprocess.Popen(
  [command_name, "-c", {_SLEEP!r}], executable=python, start_new_session=True,
)
print(mcp.pid, command.pid, flush=True)
time.sleep(60)
"""


class _Turn:
  """A runtime launched as ``runtime_name`` plus its MCP server and command."""

  def __init__(
    self, marker, *, runtime_name="node", command_name="npm", own_session=True,
  ):
    import os
    import subprocess
    import sys
    self.marker = marker
    env = dict(os.environ, **{process_groups.RUN_MARKER_ENV: marker})
    # The Codex app-server is the npm ``#!/usr/bin/env node`` wrapper, so its
    # argv[0] is ``node``, not ``codex``.
    self.runtime = subprocess.Popen(
      [runtime_name, "-c", _RUNTIME, command_name, sys.executable],
      executable=sys.executable, env=env, start_new_session=own_session,
      stdout=subprocess.PIPE, text=True,
    )
    self.mcp, self.command = map(int, self.runtime.stdout.readline().split())

  @property
  def pids(self):
    return (self.runtime.pid, self.mcp, self.command)

  def close(self):
    import os
    # An orphan may already be reaped and its pid reused: signal it only
    # while it still carries this turn's marker.
    needle = f"{process_groups.RUN_MARKER_ENV}={self.marker}".encode()
    for pid in (self.command, self.mcp):
      try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
          if needle in handle.read().split(b"\0"):
            os.kill(pid, signal.SIGKILL)
      except (OSError, ProcessLookupError):
        pass
    self.runtime.kill()
    self.runtime.wait()
    self.runtime.stdout.close()


def _ended(pid, timeout=2.0):
  """Whether ``pid`` exited (gone, or a zombie its parent has not reaped)."""
  import time
  deadline = time.monotonic() + timeout
  while True:
    try:
      with open(f"/proc/{pid}/stat", "rb") as handle:
        if handle.read().rsplit(b") ", 1)[1].split()[0] == b"Z":
          return True
    except (FileNotFoundError, ProcessLookupError):
      return True
    if time.monotonic() >= deadline:
      return False
    time.sleep(0.02)


def _alive(pid):
  return not _ended(pid, timeout=0)


def test_storage_stop_kills_tool_commands_but_keeps_a_node_wrapped_runtime(
  monkeypatch,
):
  turn = _Turn(f"filling-{uuid4().hex}", runtime_name="node")
  try:
    _scoped_to(monkeypatch, *turn.pids)

    assert process_groups.stop_agent_commands() == 1
    assert _ended(turn.command)
    # The runtime keeps running whatever its executable is called, so its
    # turn can still be paused and resumed.
    assert turn.runtime.poll() is None
  finally:
    turn.close()


def test_storage_stop_keeps_the_runtimes_mcp_servers(monkeypatch):
  """A continued cleanup turn must not lose its Möbius tools."""
  turn = _Turn(f"mcp-{uuid4().hex}", runtime_name="claude")
  try:
    _scoped_to(monkeypatch, *turn.pids)

    process_groups.stop_agent_commands()
    assert _ended(turn.command)
    assert _alive(turn.mcp)
  finally:
    turn.close()


def test_storage_stop_keeps_mcp_servers_of_a_runtime_launched_without_setsid(
  monkeypatch,
):
  """Without ``setsid`` (a development host) the runtime shares the server's
  session; what stayed in it is still the runtime's, and its commands still
  leave it."""
  turn = _Turn(f"no-setsid-{uuid4().hex}", runtime_name="claude", own_session=False)
  try:
    _scoped_to(monkeypatch, *turn.pids)

    assert process_groups.stop_agent_commands() == 1
    assert _ended(turn.command)
    assert _alive(turn.mcp)
    assert turn.runtime.poll() is None
  finally:
    turn.close()


def test_storage_stop_kills_a_command_its_runtime_left_behind(monkeypatch):
  """Re-parented away from its runtime, an orphan cannot pass for one."""
  turn = _Turn(f"orphan-{uuid4().hex}")
  try:
    turn.runtime.kill()
    turn.runtime.wait()
    _scoped_to(monkeypatch, *turn.pids)

    process_groups.stop_agent_commands()
    assert _ended(turn.command)
  finally:
    turn.close()


def test_storage_stop_spares_a_run_the_owner_continued(monkeypatch):
  continued = _Turn(f"continued-{uuid4().hex}", command_name="du")
  other = _Turn(f"other-{uuid4().hex}", command_name="curl")
  try:
    _scoped_to(monkeypatch, *continued.pids, *other.pids)

    assert process_groups.stop_agent_commands(
      spare_markers=frozenset({continued.marker}),
    ) == 1
    assert _ended(other.command)
    assert _alive(continued.command)
  finally:
    continued.close()
    other.close()


def test_storage_stop_never_leaves_a_process_frozen(monkeypatch):
  """A stopped process still dies: SIGKILL ends it without a SIGCONT."""
  turn = _Turn(f"frozen-{uuid4().hex}", command_name="tar")
  try:
    _scoped_to(monkeypatch, *turn.pids)
    process_groups.stop_agent_commands()
    # A process left frozen would read T (stopped), never Z or gone.
    assert _ended(turn.command)
  finally:
    turn.close()


def test_unmarked_processes_are_never_scanned_as_agent_commands(tmp_path):
  import os
  import shutil
  import subprocess
  env = {k: v for k, v in os.environ.items() if k != process_groups.RUN_MARKER_ENV}
  plain = subprocess.Popen(
    [shutil.which("sleep"), "60"], env=env, start_new_session=True,
  )
  try:
    assert plain.pid not in process_groups._marked_processes()
  finally:
    plain.kill()
    plain.wait()
