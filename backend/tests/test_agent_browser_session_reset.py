"""Exact-owner safety and failure contracts for shared browser cleanup."""
from pathlib import Path
import shutil

import pytest
from app import browser_processes as RESET


def _write_process(proc_root, pid, *, ppid=1, start_ticks=None,
                   args=("/opt/chrome",), environment=None, state="S"):
  process = proc_root / str(pid)
  process.mkdir(parents=True, exist_ok=True)
  fields = [state, str(ppid), *("0" for _ in range(17)), str(start_ticks or pid)]
  (process / "stat").write_text(f"{pid} (process with spaces) {' '.join(fields)}\n")
  (process / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
  (process / "exe").unlink(missing_ok=True)
  (process / "exe").symlink_to(args[0])
  (process / "environ").write_bytes(b"\0".join(
    f"{key}={value}".encode() for key, value in (environment or {}).items()) + b"\0")


def _pids(scan):
  return [p.pid for p in scan.processes]


def test_exact_profile_includes_helpers_but_not_foreign_profile(tmp_path):
  profile = str(tmp_path / "profile")
  _write_process(tmp_path, 100, args=("/opt/agent-browser-linux-x64",),
                 environment={"AGENT_BROWSER_PROFILE": profile, "AGENT_BROWSER_SESSION": "owned"})
  _write_process(tmp_path, 101, args=("/opt/chrome", f"--user-data-dir={profile}"))
  _write_process(tmp_path, 102, ppid=101, args=("/opt/chrome", "--type=renderer"))
  _write_process(tmp_path, 201, ppid=100, args=("/opt/chrome", "--user-data-dir=/foreign"))
  _write_process(tmp_path, 202, ppid=201, args=("/opt/chrome", "--type=renderer"))
  scan = RESET.scan_browser_processes(profile=profile, proc_root=tmp_path)
  assert scan.complete
  assert _pids(scan) == [100, 101, 102]
  assert scan.targets == frozenset({RESET.BrowserSessionTarget("owned")})


@pytest.mark.parametrize("kind", ["daemon", "browser"])
def test_ambiguous_profile_ownership_is_never_guessed(tmp_path, kind):
  profile = str(tmp_path / "profile")
  for pid in (100, 200):
    args = (("/opt/agent-browser-linux-x64",) if kind == "daemon" else
            ("/opt/chrome", f"--user-data-dir={profile}"))
    _write_process(tmp_path, pid, args=args, environment={"AGENT_BROWSER_PROFILE": profile})
  with pytest.raises(RESET.SessionResetError, match="refusing to guess"):
    RESET.scan_browser_processes(profile=profile, proc_root=tmp_path)


def test_orphan_chrome_and_detached_crashpad_are_not_idle(tmp_path):
  profile = str(tmp_path / "profile")
  _write_process(tmp_path, 101, args=("/opt/chrome", "--user-data-dir", profile))
  _write_process(tmp_path, 102, args=("/opt/chrome_crashpad_handler",),
                 environment={"CHAT_ID": "a"})
  scan = RESET.scan_browser_processes(chat_id="a", profile=profile, proc_root=tmp_path)
  assert _pids(scan) == [101, 102]
  assert scan.targets == frozenset()
  assert not scan.idle


def test_daemon_before_chrome_is_still_resettable(tmp_path):
  _write_process(tmp_path, 100, args=("/opt/agent-browser-linux-arm64",),
                 environment={"CHAT_ID": "a", "AGENT_BROWSER_SESSION": "custom"})
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert _pids(scan) == [100]
  assert scan.targets == frozenset({RESET.BrowserSessionTarget("custom")})


def test_foreign_chat_wins_over_matching_profile_and_ancestry(tmp_path):
  profile = str(tmp_path / "profile")
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  _write_process(tmp_path, 101, ppid=100, args=("/opt/chrome", f"--user-data-dir={profile}"),
                 environment={"CHAT_ID": "b"})
  scan = RESET.scan_browser_processes(chat_id="a", profile=profile, proc_root=tmp_path)
  assert _pids(scan) == [100]


def test_unknown_executable_is_never_selected_even_as_owned_child(tmp_path):
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  _write_process(tmp_path, 101, ppid=100, args=("/opt/agent-browser-linux-x64-wrapper",),
                 environment={"CHAT_ID": "a"})
  assert _pids(RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)) == [100]


def test_forged_browser_argv_does_not_override_executable(tmp_path):
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  (tmp_path / "100" / "exe").unlink()
  (tmp_path / "100" / "exe").symlink_to("/usr/bin/python3")
  assert RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path).idle
  assert RESET.browser_memory_by_chat(proc_root=tmp_path) == {}


def test_unreadable_browser_executable_is_unknown_not_idle(tmp_path, monkeypatch):
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  original = RESET.os.readlink
  def readlink(path):
    if path == tmp_path / "100" / "exe":
      raise PermissionError("denied")
    return original(path)
  monkeypatch.setattr(RESET.os, "readlink", readlink)
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert not scan.complete and not scan.idle
  with pytest.raises(RESET.SessionResetError, match="incomplete"):
    RESET.browser_memory_by_chat(proc_root=tmp_path)


@pytest.mark.parametrize("argv0", ["/usr/sbin/sshd", "python3"])
def test_unreadable_unrelated_executable_does_not_poison_browser_inventory(tmp_path, monkeypatch, argv0):
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  _write_process(tmp_path, 200, args=(argv0,))
  original = RESET.os.readlink
  def readlink(path):
    if path == tmp_path / "200" / "exe":
      raise PermissionError("foreign executable")
    return original(path)
  monkeypatch.setattr(RESET.os, "readlink", readlink)
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert scan.complete
  assert _pids(scan) == [100]


def test_zombies_are_ignored_even_with_identifying_cmdline(tmp_path):
  _write_process(tmp_path, 100, state="Z", environment={"CHAT_ID": "a"})
  assert RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path).idle


@pytest.mark.parametrize("field", ["stat", "environ"])
def test_unreadable_browser_inventory_is_incomplete(tmp_path, monkeypatch, field):
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  original = Path.read_bytes if field == "environ" else Path.read_text
  def read(path, *args, **kwargs):
    if path == tmp_path / "100" / field:
      raise PermissionError("denied")
    return original(path, *args, **kwargs)
  monkeypatch.setattr(Path, "read_bytes" if field == "environ" else "read_text", read)
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert not scan.complete
  assert not scan.idle


def test_malformed_stat_is_incomplete(tmp_path):
  _write_process(tmp_path, 100, environment={"CHAT_ID": "a"})
  (tmp_path / "100" / "stat").write_text("invalid")
  assert not RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path).complete


def test_missing_proc_root_and_missing_owner_are_not_idle(tmp_path):
  assert not RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path / "missing").idle
  assert not RESET.scan_browser_processes(proc_root=tmp_path).idle


def test_pid_reuse_between_inventory_reads_is_incomplete(tmp_path, monkeypatch):
  _write_process(tmp_path, 100, start_ticks=10, environment={"CHAT_ID": "a"})
  monkeypatch.setattr(RESET, "_identity", lambda pid, root: RESET.ProcessIdentity(pid, 11))
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert not scan.complete
  assert not scan.processes


def test_pid_reuse_is_rechecked_before_every_signal(tmp_path, monkeypatch):
  for pid in (100, 101):
    _write_process(tmp_path, pid, start_ticks=10)
  signals = []
  def kill(pid, sig):
    signals.append((pid, sig))
    shutil.rmtree(tmp_path / str(pid))
    # The second PID is recycled after the first signal, not before the batch.
    _write_process(tmp_path, 101, start_ticks=99)
  monkeypatch.setattr(RESET.os, "kill", kill)
  RESET.terminate_processes(tuple(RESET.ProcessIdentity(pid, 10) for pid in (100, 101)),
                            wait_seconds=0, proc_root=tmp_path)
  assert signals == [(100, RESET.signal.SIGKILL)]


def test_exited_but_unreaped_child_counts_as_released(tmp_path, monkeypatch):
  _write_process(tmp_path, 100, start_ticks=10)
  signals = []
  def kill(pid, sig):
    signals.append((pid, sig))
    _write_process(tmp_path, pid, start_ticks=10, state="Z")
  monkeypatch.setattr(RESET.os, "kill", kill)
  RESET.terminate_processes((RESET.ProcessIdentity(100, 10),), wait_seconds=0, proc_root=tmp_path)
  assert signals == [(100, RESET.signal.SIGKILL)]


def test_signal_failure_is_not_claimed_as_success(tmp_path, monkeypatch):
  _write_process(tmp_path, 100, start_ticks=10)
  monkeypatch.setattr(RESET.os, "kill", lambda *args: None)
  with pytest.raises(RESET.SessionResetError, match="did not exit"):
    RESET.terminate_processes((RESET.ProcessIdentity(100, 10),), wait_seconds=0, proc_root=tmp_path)


def test_reset_refuses_incomplete_inventory_without_signaling(monkeypatch):
  monkeypatch.setattr(RESET, "scan_browser_processes", lambda **kw:
                      RESET.BrowserSessionScan(frozenset(), False, (RESET.ProcessIdentity(100, 10),)))
  calls = []
  monkeypatch.setattr(RESET, "terminate_processes", calls.append)
  with pytest.raises(RESET.SessionResetError, match="incomplete"):
    RESET.reset_browser_processes(chat_id="a")
  assert calls == []


def test_reset_rescans_and_releases_newly_visible_helper(monkeypatch):
  identities = [RESET.ProcessIdentity(100, 10), RESET.ProcessIdentity(101, 11)]
  scans = iter([*(RESET.BrowserSessionScan(frozenset(), True, (p,)) for p in identities),
                RESET.BrowserSessionScan(frozenset(), True)])
  monkeypatch.setattr(RESET, "scan_browser_processes", lambda **kw: next(scans))
  calls = []
  monkeypatch.setattr(RESET, "terminate_processes", calls.append)
  assert RESET.reset_browser_processes(chat_id="a")
  assert calls == [(identities[0],), (identities[1],)]


def test_reset_is_bounded_when_owned_processes_keep_appearing(monkeypatch):
  scan = RESET.BrowserSessionScan(frozenset(), True, (RESET.ProcessIdentity(100, 10),))
  monkeypatch.setattr(RESET, "scan_browser_processes", lambda **kw: scan)
  calls = []
  monkeypatch.setattr(RESET, "terminate_processes", calls.append)
  with pytest.raises(RESET.SessionResetError, match="remained active"):
    RESET.reset_browser_processes(chat_id="a")
  assert len(calls) == 3


def test_pidfd_delivery_revalidates_identity_and_closes_descriptor(tmp_path, monkeypatch):
  _write_process(tmp_path, 100, start_ticks=10)
  monkeypatch.setattr(RESET, "PROC_ROOT", tmp_path)
  events = []
  monkeypatch.setattr(RESET.os, "pidfd_open", lambda pid: events.append(("open", pid)) or 42)
  original_close = RESET.os.close
  monkeypatch.setattr(RESET.os, "close", lambda fd: events.append(("close", fd)) if fd == 42 else original_close(fd))
  def send(fd, sig):
    events.append(("signal", fd, sig))
    shutil.rmtree(tmp_path / "100")
  monkeypatch.setattr(RESET.signal, "pidfd_send_signal", send)
  RESET.terminate_processes((RESET.ProcessIdentity(100, 10),), wait_seconds=0, proc_root=tmp_path)
  assert events == [("open", 100), ("signal", 42, RESET.signal.SIGKILL), ("close", 42)]


def test_pidfd_open_race_does_not_signal_recycled_process(tmp_path, monkeypatch):
  _write_process(tmp_path, 100, start_ticks=10)
  monkeypatch.setattr(RESET, "PROC_ROOT", tmp_path)
  events = []
  def open_fd(pid):
    _write_process(tmp_path, pid, start_ticks=99)
    return 42
  monkeypatch.setattr(RESET.os, "pidfd_open", open_fd)
  original_close = RESET.os.close
  monkeypatch.setattr(RESET.os, "close", lambda fd: events.append(("close", fd)) if fd == 42 else original_close(fd))
  monkeypatch.setattr(RESET.signal, "pidfd_send_signal", lambda *args: events.append(("signal", args)))
  RESET.terminate_processes((RESET.ProcessIdentity(100, 10),), wait_seconds=0, proc_root=tmp_path)
  assert events == [("close", 42)]


def test_ancestry_does_not_attach_older_helper_to_recycled_parent_pid(tmp_path):
  _write_process(tmp_path, 100, start_ticks=50, environment={"CHAT_ID": "a"})
  # This PPID was captured before the old parent disappeared and PID 100 was
  # recycled. No inherited environment independently attributes this helper.
  _write_process(tmp_path, 101, ppid=100, start_ticks=20,
                 args=("/opt/chrome", "--type=renderer"))
  _write_process(tmp_path, 102, ppid=101, start_ticks=30,
                 args=("/opt/chrome", "--type=renderer"))
  # Same-tick births are legitimate; Linux start times have finite precision.
  _write_process(tmp_path, 103, ppid=100, start_ticks=50,
                 args=("/opt/chrome", "--type=renderer"))
  _write_process(tmp_path, 104, ppid=103, start_ticks=60,
                 args=("/opt/chrome", "--type=renderer"))
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert scan.complete
  assert _pids(scan) == [100, 103, 104]



def _write_browser(proc_root, pid, *, pss_kb, exe="/opt/chrome", **process):
  """A process as Chrome leaves it: real executable, possibly rewritten argv."""
  _write_process(proc_root, pid, **process)
  (proc_root / str(pid) / "exe").unlink()
  (proc_root / str(pid) / "exe").symlink_to(exe)
  (proc_root / str(pid) / "smaps_rollup").write_text(
    f"Rss:  {pss_kb * 2} kB\nPss:  {pss_kb} kB\n")


def test_browser_memory_counts_helpers_chrome_renamed_and_untagged(tmp_path):
  # Chrome collapses a helper's argv into one string and drops CHAT_ID; the
  # renderer still belongs to the chat through zygote -> tagged root.
  _write_browser(tmp_path, 100, pss_kb=100, args=("/opt/chrome",),
                 environment={"CHAT_ID": "a"})
  _write_browser(tmp_path, 101, pss_kb=10, ppid=100,
                 args=("/opt/chrome --type=zygote --user-data-dir=/p/chat-a",))
  _write_browser(tmp_path, 102, pss_kb=2_000_000, ppid=101,
                 args=("/opt/chrome --type=renderer",))
  # A named session belongs to no chat, and a non-browser is never counted.
  _write_browser(tmp_path, 200, pss_kb=9000, args=("/opt/chrome",))
  _write_browser(tmp_path, 300, pss_kb=9000, exe="/usr/bin/python3",
                 environment={"CHAT_ID": "a"})
  assert RESET.browser_memory_by_chat(proc_root=tmp_path) == {
    "a": 2_000_110 * 1024}


def test_browser_memory_ancestry_skips_recycled_parent_pid(tmp_path):
  _write_browser(tmp_path, 100, pss_kb=10, start_ticks=50,
                 environment={"CHAT_ID": "a"})
  _write_browser(tmp_path, 101, pss_kb=5000, ppid=100, start_ticks=20)
  assert RESET.browser_memory_by_chat(proc_root=tmp_path) == {"a": 10 * 1024}


def test_browser_memory_skips_pid_recycled_during_pss_read(tmp_path, monkeypatch):
  _write_browser(tmp_path, 100, pss_kb=2000, start_ticks=10,
                 environment={"CHAT_ID": "a"})
  def recycled(pid, root):
    _write_browser(root, pid, pss_kb=3000, start_ticks=99,
                   environment={"CHAT_ID": "b"})
    return 2_000 * 1024
  monkeypatch.setattr(RESET, "_pss_bytes", recycled)
  assert RESET.browser_memory_by_chat(proc_root=tmp_path) == {}


def test_unreadable_pss_is_unknown_not_measured_zero(tmp_path, monkeypatch):
  _write_browser(tmp_path, 100, pss_kb=2000, environment={"CHAT_ID": "a"})
  original = Path.read_text
  def read(path, *args, **kwargs):
    if path == tmp_path / "100" / "smaps_rollup":
      raise PermissionError("PSS denied")
    return original(path, *args, **kwargs)
  monkeypatch.setattr(Path, "read_text", read)
  with pytest.raises(RESET.SessionResetError, match="PSS"):
    RESET.browser_memory_by_chat(proc_root=tmp_path)


def test_preclose_identity_keeps_rewritten_helper_after_root_exit(tmp_path, monkeypatch):
  _write_browser(tmp_path, 100, pss_kb=10, environment={"CHAT_ID": "a"})
  _write_browser(tmp_path, 101, pss_kb=2000, ppid=100,
                 args=("/opt/chrome --type=renderer",))
  scan = RESET.scan_browser_processes(chat_id="a", proc_root=tmp_path)
  assert _pids(scan) == [100, 101]
  signals = []

  def kill(pid, sig):
    signals.append(pid)
    shutil.rmtree(tmp_path / str(pid))
    if pid == 100:
      _write_browser(tmp_path, 101, pss_kb=2000, ppid=1,
                     args=("/opt/chrome --type=renderer",))

  monkeypatch.setattr(RESET.os, "kill", kill)
  RESET.terminate_processes(scan.processes, wait_seconds=0, proc_root=tmp_path)
  assert signals == [100, 101]
