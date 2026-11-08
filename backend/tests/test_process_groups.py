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


def _scoped_to(monkeypatch, *procs):
  """Let stop_agent_commands see only these test processes.

  The real scan and real signals run, but a live instance's own agent
  commands (this test run may share the container) are never in view.
  """
  ours = {proc.pid for proc in procs}
  real = process_groups._marked_processes
  monkeypatch.setattr(
    process_groups, "_marked_processes",
    lambda: {pid: row for pid, row in real().items() if pid in ours},
  )


def _named_command(name, marker):
  """A long-running process whose argv[0] is ``name``.

  Runs the Python interpreter rather than a renamed ``sleep``: multi-call
  coreutils builds refuse to run under a name they do not recognise.
  """
  import os
  import subprocess
  import sys
  env = dict(os.environ, **{process_groups.RUN_MARKER_ENV: marker})
  return subprocess.Popen(
    [name, "-c", "import time; time.sleep(60)"],
    executable=sys.executable, env=env, start_new_session=True,
  )


def test_storage_stop_kills_agent_commands_but_keeps_agent_runtimes(
  tmp_path, monkeypatch,
):
  marker = f"filling-{uuid4().hex}"
  command = _named_command("npm", marker)
  agent = _named_command("claude", marker)
  try:
    _scoped_to(monkeypatch, command, agent)

    assert process_groups.stop_agent_commands() == 1
    assert _gone(command)
    # The agent keeps running so its turn can still be paused and resumed.
    assert agent.poll() is None
  finally:
    for proc in (command, agent):
      proc.kill()
      proc.wait()


def test_storage_stop_spares_a_run_the_owner_continued(tmp_path, monkeypatch):
  continued = f"continued-{uuid4().hex}"
  other = f"other-{uuid4().hex}"
  continued_command = _named_command("du", continued)
  other_command = _named_command("curl", other)
  try:
    _scoped_to(monkeypatch, continued_command, other_command)

    assert process_groups.stop_agent_commands(
      spare_markers=frozenset({continued}),
    ) == 1
    assert _gone(other_command)
    assert continued_command.poll() is None
  finally:
    for proc in (continued_command, other_command):
      proc.kill()
      proc.wait()


def test_storage_stop_never_leaves_a_process_frozen(tmp_path, monkeypatch):
  """A stopped process still dies: SIGKILL ends it without a SIGCONT."""
  marker = f"frozen-{uuid4().hex}"
  command = _named_command("tar", marker)
  try:
    _scoped_to(monkeypatch, command)
    process_groups.stop_agent_commands()
    assert _gone(command)
    assert command.returncode == -signal.SIGKILL
  finally:
    command.kill()
    command.wait()


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
