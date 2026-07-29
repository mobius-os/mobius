"""Safety contract for resetting one poisoned agent-browser profile."""

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent_browser_session_reset.py"
SPEC = importlib.util.spec_from_file_location("agent_browser_session_reset", SCRIPT)
assert SPEC and SPEC.loader
RESET = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RESET
SPEC.loader.exec_module(RESET)


def _write_process(
  proc_root: Path,
  pid: int,
  *,
  ppid: int,
  start_ticks: int,
  args: tuple[str, ...],
  environment: dict[str, str] | None = None,
) -> None:
  process = proc_root / str(pid)
  process.mkdir()
  fields = ["S", str(ppid), *("0" for _ in range(17)), str(start_ticks)]
  (process / "stat").write_text(
    f"{pid} (process with spaces) {' '.join(fields)}\n",
    encoding="utf-8",
  )
  (process / "cmdline").write_bytes(
    b"\0".join(arg.encode("utf-8") for arg in args) + b"\0"
  )
  (process / "environ").write_bytes(b"\0".join(
    f"{key}={value}".encode("utf-8")
    for key, value in (environment or {}).items()
  ) + b"\0")


def test_profile_maps_to_its_exact_browser_root_and_daemon(tmp_path: Path):
  proc_root = tmp_path / "proc"
  proc_root.mkdir()
  profile = str(tmp_path / "profile")
  _write_process(
    proc_root,
    100,
    ppid=1,
    start_ticks=10,
    args=("/opt/agent-browser-linux-x64",),
    environment={"AGENT_BROWSER_PROFILE": profile},
  )
  _write_process(
    proc_root,
    101,
    ppid=1,
    start_ticks=11,
    args=("/opt/chrome", f"--user-data-dir={profile}"),
  )
  _write_process(
    proc_root,
    102,
    ppid=101,
    start_ticks=12,
    args=("/opt/chrome", "--type=renderer", f"--user-data-dir={profile}"),
  )
  _write_process(
    proc_root,
    201,
    ppid=100,
    start_ticks=20,
    args=("/opt/chrome", "--user-data-dir=/another/profile"),
  )

  processes = RESET.find_session_processes(profile, proc_root)

  assert processes == RESET.SessionProcesses(
    browser=RESET.ProcessIdentity(pid=101, start_ticks=11),
    daemon=RESET.ProcessIdentity(pid=100, start_ticks=10),
  )


def test_ambiguous_profile_ownership_is_never_guessed(tmp_path: Path):
  proc_root = tmp_path / "proc"
  proc_root.mkdir()
  profile = str(tmp_path / "profile")
  for daemon, browser in ((100, 101), (200, 201)):
    _write_process(
      proc_root,
      daemon,
      ppid=1,
      start_ticks=daemon,
      args=("/opt/agent-browser-linux-x64",),
      environment={"AGENT_BROWSER_PROFILE": profile},
    )
    _write_process(
      proc_root,
      browser,
      ppid=daemon,
      start_ticks=browser,
      args=("/opt/chrome", f"--user-data-dir={profile}"),
    )

  with pytest.raises(RESET.SessionResetError, match="refusing to guess"):
    RESET.find_session_processes(profile, proc_root)


def test_orphaned_browser_root_is_still_owned_by_the_exact_profile(tmp_path: Path):
  proc_root = tmp_path / "proc"
  proc_root.mkdir()
  profile = str(tmp_path / "profile")
  _write_process(
    proc_root,
    101,
    ppid=1,
    start_ticks=11,
    args=("/opt/chrome", f"--user-data-dir={profile}"),
  )

  assert RESET.find_session_processes(profile, proc_root) == RESET.SessionProcesses(
    browser=RESET.ProcessIdentity(pid=101, start_ticks=11),
    daemon=None,
  )


def test_daemon_remains_resettable_before_chrome_starts(tmp_path: Path):
  proc_root = tmp_path / "proc"
  proc_root.mkdir()
  profile = str(tmp_path / "profile")
  _write_process(
    proc_root,
    100,
    ppid=1,
    start_ticks=10,
    args=("/opt/agent-browser-linux-x64",),
    environment={"AGENT_BROWSER_PROFILE": profile},
  )

  assert RESET.find_session_processes(profile, proc_root) == RESET.SessionProcesses(
    browser=None,
    daemon=RESET.ProcessIdentity(pid=100, start_ticks=10),
  )


def test_pid_reuse_is_rechecked_before_any_signal(tmp_path: Path, monkeypatch):
  proc_root = tmp_path / "proc"
  proc_root.mkdir()
  _write_process(
    proc_root,
    100,
    ppid=1,
    start_ticks=10,
    args=("/opt/agent-browser-linux-x64",),
    environment={"AGENT_BROWSER_PROFILE": str(tmp_path / "profile")},
  )
  _write_process(
    proc_root,
    101,
    ppid=100,
    start_ticks=11,
    args=("/opt/chrome", f"--user-data-dir={tmp_path / 'profile'}"),
  )
  processes = RESET.SessionProcesses(
    browser=RESET.ProcessIdentity(pid=101, start_ticks=8),
    daemon=RESET.ProcessIdentity(pid=100, start_ticks=9),
  )
  signals: list[tuple[int, int]] = []
  monkeypatch.setattr(RESET.os, "kill", lambda pid, sig: signals.append((pid, sig)))

  RESET.terminate_session_processes(
    processes,
    wait_seconds=0,
    proc_root=proc_root,
  )

  assert signals == []
