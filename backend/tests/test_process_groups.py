import logging
import signal

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
  mine = _command_in_own_session("run-under-test")
  try:
    assert process_groups.terminate_agent_processes(
      None,
      run_marker="run-under-test",
      logger=logging.getLogger(__name__),
      label="test",
      grace_seconds=0.2,
    ) is True
    assert _gone(mine)
  finally:
    mine.kill()


def test_another_runs_commands_are_never_touched():
  other = _command_in_own_session("another-run")
  try:
    assert process_groups.terminate_run_processes(
      "run-under-test", logger=logging.getLogger(__name__), label="test",
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
  chrome = tmp_path / "chrome"
  chrome.symlink_to("/bin/sleep")
  env = dict(os.environ, **{process_groups.RUN_MARKER_ENV: "run-under-test"})
  browser = subprocess.Popen([str(chrome), "60"], env=env, start_new_session=True)
  try:
    assert process_groups.terminate_run_processes(
      "run-under-test", logger=logging.getLogger(__name__), label="test",
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
