"""An in-app restart reruns the boot inside the container, on any host.

Railway's default ON_FAILURE restart policy never revives a container that
exits cleanly, so a planned restart that stopped the container left such an
instance down until a manual redeploy. The image's container supervisor
instead reruns the boot in place when the entrypoint's restart poller asks,
while host stops and crashes still end the container with their own status.
These tests run the real supervisor with a fake boot script.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "backend/runtime/container_supervisor.py"
ENTRYPOINT = ROOT / "backend/scripts/entrypoint.sh"

pytestmark = pytest.mark.skipif(
  not sys.platform.startswith("linux"), reason="the supervisor uses Linux /proc and prctl",
)

# A stand-in for entrypoint.sh: it records each boot, starts a long-lived
# background service (like cron or the identity broker) and, like `su`, dies
# from the SIGTERM it receives.
FAKE_BOOT = r"""
echo "boot $$ ${MOBIUS_SUPERVISOR_PID:-}" >> "$BOOT_LOG"
sleep 1000 &
echo "service $!" >> "$BOOT_LOG"
trap 'trap - TERM; kill -TERM $$' TERM
while :; do sleep 0.05; done
"""


def _wait_for(predicate, timeout=15.0):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if predicate():
      return
    time.sleep(0.05)
  raise AssertionError("condition not reached in time")


def _log_lines(log: Path, kind: str) -> list[list[str]]:
  if not log.exists():
    return []
  return [line.split()[1:] for line in log.read_text().splitlines() if line.startswith(kind + " ")]


def _alive(pid: int) -> bool:
  try:
    state = Path(f"/proc/{pid}/stat").read_text()
  except OSError:
    return False
  return state[state.rfind(")") + 2:].split()[0] != "Z"


def _start(tmp_path: Path, script: str) -> tuple[subprocess.Popen, Path]:
  log = tmp_path / "boots.log"
  proc = subprocess.Popen(
    [sys.executable, "-I", str(SUPERVISOR), "sh", "-c", script],
    env={"PATH": os.environ["PATH"], "BOOT_LOG": str(log)},
  )
  return proc, log


def _stop(proc: subprocess.Popen) -> None:
  if proc.poll() is None:
    proc.kill()
    proc.wait(timeout=10)


def test_planned_restart_reruns_the_boot_without_ending_the_container(tmp_path):
  proc, log = _start(tmp_path, FAKE_BOOT)
  try:
    _wait_for(lambda: len(_log_lines(log, "service")) == 1)
    first_service = int(_log_lines(log, "service")[0][0])

    proc.send_signal(signal.SIGUSR1)

    _wait_for(lambda: len(_log_lines(log, "service")) == 2)
    assert proc.poll() is None, "the container's supervisor must keep running"
    boots = _log_lines(log, "boot")
    assert len(boots) == 2 and boots[0][0] != boots[1][0]
  finally:
    _stop(proc)
  assert not _alive(first_service)


def test_planned_restart_clears_what_the_old_boot_left_running(tmp_path):
  proc, log = _start(tmp_path, FAKE_BOOT)
  try:
    _wait_for(lambda: len(_log_lines(log, "service")) == 1)
    first_service = int(_log_lines(log, "service")[0][0])
    proc.send_signal(signal.SIGUSR1)
    _wait_for(lambda: len(_log_lines(log, "service")) == 2)
    # The old boot's background service must be gone before the new boot
    # starts its own, so services never run twice side by side.
    assert not _alive(first_service)
    assert _alive(int(_log_lines(log, "service")[1][0]))
  finally:
    _stop(proc)


def test_consecutive_planned_restarts_each_rerun_the_boot(tmp_path):
  proc, log = _start(tmp_path, FAKE_BOOT)
  try:
    for boots in (2, 3):
      _wait_for(lambda: len(_log_lines(log, "service")) == boots - 1)
      proc.send_signal(signal.SIGUSR1)
      _wait_for(lambda: len(_log_lines(log, "service")) == boots)
      assert proc.poll() is None
    services = [int(line[0]) for line in _log_lines(log, "service")]
    assert [_alive(pid) for pid in services] == [False, False, True]

    # A host stop after in-place restarts still ends the container cleanly.
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 143
  finally:
    _stop(proc)
  assert len(_log_lines(log, "boot")) == 3


def test_each_boot_learns_which_process_to_ask_for_a_restart(tmp_path):
  proc, log = _start(tmp_path, FAKE_BOOT)
  try:
    _wait_for(lambda: len(_log_lines(log, "boot")) == 1)
    assert _log_lines(log, "boot")[0][1] == str(proc.pid)
  finally:
    _stop(proc)


def test_boot_starts_with_default_signal_handling_like_under_tini(tmp_path):
  # Python ignores SIGPIPE; a boot inheriting that would change how every
  # shell pipeline and the server react to a closed pipe.
  # The supervisor also blocks its handled signals while it starts a boot; the
  # boot must not inherit that mask either.
  proc, log = _start(
    tmp_path,
    'echo "ignored $(awk \'/^SigIgn:/ {print $2}\' /proc/$$/status)" >> "$BOOT_LOG"; '
    'echo "blocked $(awk \'/^SigBlk:/ {print $2}\' /proc/$$/status)" >> "$BOOT_LOG"; exit 0',
  )
  assert proc.wait(timeout=15) == 0
  assert int(_log_lines(log, "ignored")[0][0], 16) == 0
  assert int(_log_lines(log, "blocked")[0][0], 16) == 0


def test_host_stop_is_forwarded_and_reported_as_the_boot_ended(tmp_path):
  proc, log = _start(tmp_path, FAKE_BOOT)
  _wait_for(lambda: len(_log_lines(log, "boot")) == 1)
  proc.send_signal(signal.SIGTERM)
  # 143 is what `tini -e 143` reports to the host as a clean stop.
  assert proc.wait(timeout=15) == 143
  assert len(_log_lines(log, "boot")) == 1, "a host stop must never start another boot"


def test_crash_ends_the_container_instead_of_restarting_in_place(tmp_path):
  proc, log = _start(tmp_path, 'echo "boot $$" >> "$BOOT_LOG"; exit 3')
  assert proc.wait(timeout=15) == 3
  assert len(_log_lines(log, "boot")) == 1


def test_killed_boot_without_a_restart_request_reports_a_crash(tmp_path):
  # The restart path's SIGKILL backstop, or the OOM killer, must stay visible
  # to the host and must not be mistaken for a planned restart.
  proc, log = _start(tmp_path, 'echo "boot $$" >> "$BOOT_LOG"; kill -KILL $$')
  assert proc.wait(timeout=15) == 137
  assert len(_log_lines(log, "boot")) == 1


def _entrypoint_restart_poller() -> str:
  """The entrypoint's real restart poller, plus the lines that hand it the
  supervisor pid, as shell source that can run outside the image."""
  source = ENTRYPOINT.read_text()
  pid_setup = re.search(r'^_supervisor_pid=.*\nunset MOBIUS_SUPERVISOR_PID\n', source, re.M)
  poller = re.search(r"^_start_platform_restart_poller\(\) \{\n.*?^\}\n", source, re.M | re.S)
  assert pid_setup and poller, "entrypoint.sh restart poller not found"
  return pid_setup.group(0) + "_restart_poller_started=0\n" + poller.group(0)


def test_entrypoint_restart_poller_restarts_through_the_supervisor(tmp_path):
  data = tmp_path / "data"
  (data / "run").mkdir(parents=True)
  poller = _entrypoint_restart_poller().replace("/data/", f"{data}/")
  # The first boot publishes one restart request, as the app does; the
  # ledger's authentication is stubbed to accept it.
  script = (
    'python3() { rm -f "$DATA/.platform-restart-requested"; return 0; }\n'
    'MOBIUS_BOOT_ID="boot-$$"\n'
    + poller +
    'echo "boot $$" >> "$BOOT_LOG"\n'
    '[ -e "$DATA/requested-once" ] || { : > "$DATA/requested-once"; '
    ': > "$DATA/.platform-restart-requested"; }\n'
    '_start_platform_restart_poller\n'
    "trap 'trap - TERM; kill -TERM $$' TERM\n"
    'while :; do sleep 0.05; done\n'
  )
  log = tmp_path / "boots.log"
  proc = subprocess.Popen(
    [sys.executable, "-I", str(SUPERVISOR), "sh", "-c", script],
    env={"PATH": os.environ["PATH"], "BOOT_LOG": str(log), "DATA": str(data)},
  )
  try:
    _wait_for(lambda: len(_log_lines(log, "boot")) == 2)
    assert proc.poll() is None
  finally:
    _stop(proc)


# A stand-in identity broker: it binds its socket after a short start-up and,
# like the real broker, has no SIGTERM handler, so a stopped broker leaves its
# socket file behind. A boot that finds the BROKEN marker gets a broker that
# fails to start.
STAND_IN_BROKER = r"""
import socket, sys, time
time.sleep(0.3)
server = socket.socket(socket.AF_UNIX)
server.bind(sys.argv[1])
server.listen()
time.sleep(1000)
"""


def _entrypoint_broker_start() -> tuple[str, str]:
  """The entrypoint's real socket clearing and broker readiness check."""
  source = ENTRYPOINT.read_text()
  clear = re.search(r"^rm -f /run/mobius-identity-broker\.sock\n", source, re.M)
  launch = source.find("python3 -P /app/runtime/identity_broker.py &")
  wait = re.search(r"^_identity_broker_ready=0\n.*?^fi\n", source, re.M | re.S)
  assert clear and wait and launch != -1, "entrypoint.sh broker start not found"
  # The socket must be cleared before the broker starts and is waited for.
  assert clear.start() < launch < wait.start()
  return clear.group(0), wait.group(0)


def test_rerun_boot_waits_for_its_own_identity_broker(tmp_path):
  # /run outlives an in-place restart. If the old broker's socket survived
  # into the rerun, the readiness check would pass before the new broker
  # bound it, and a broker that fails to start would go unnoticed.
  sock = tmp_path / "broker.sock"
  clear, wait = _entrypoint_broker_start()
  clear = clear.replace("/run/mobius-identity-broker.sock", '"$BROKER_SOCK"')
  wait = wait.replace("/run/mobius-identity-broker.sock", '"$BROKER_SOCK"')
  script = (
    'echo "boot $$" >> "$BOOT_LOG"\n'
    + clear +
    'if [ -e "$BROKEN" ]; then "$PY" -c "raise SystemExit(1)" &\n'
    'else "$PY" -c "$BROKER" "$BROKER_SOCK" &\n'
    'fi\n'
    '_identity_broker_pid=$!\n'
    + wait +
    'echo "ready $$" >> "$BOOT_LOG"\n'
    ': > "$BROKEN"\n'
    "trap 'trap - TERM; kill -TERM $$' TERM\n"
    'while :; do sleep 0.05; done\n'
  )
  log = tmp_path / "boots.log"
  proc = subprocess.Popen(
    [sys.executable, "-I", str(SUPERVISOR), "sh", "-c", script],
    env={
      "PATH": os.environ["PATH"], "BOOT_LOG": str(log), "PY": sys.executable,
      "BROKER": STAND_IN_BROKER, "BROKER_SOCK": str(sock),
      "BROKEN": str(tmp_path / "broken"),
    },
  )
  try:
    _wait_for(lambda: len(_log_lines(log, "ready")) == 1)
    assert sock.is_socket()
    proc.send_signal(signal.SIGUSR1)
    # The rerun's broker fails, so its boot must fail rather than reach ready.
    assert proc.wait(timeout=20) == 1
  finally:
    _stop(proc)
  assert len(_log_lines(log, "boot")) == 2
  assert len(_log_lines(log, "ready")) == 1
