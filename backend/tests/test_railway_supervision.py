import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest


_SUPERVISION_PATH = (
  Path(__file__).resolve().parents[1] / "scripts" / "railway_supervision.sh"
)
_COUNTER_PATH = (
  Path(__file__).resolve().parents[1] / "scripts" / "boot_attempt_counter.py"
)


def _free_port() -> int:
  with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    return sock.getsockname()[1]


def _write_fake_recovery(tmp_path: Path) -> Path:
  child = tmp_path / "fake_recovery.py"
  child.write_text(textwrap.dedent("""
    import os
    import signal
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    count_path = Path(os.environ["FAKE_RECOVERY_COUNT"])
    count = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(count))
    scenario = os.environ["FAKE_RECOVERY_SCENARIO"]
    attempts = Path(os.environ["RECOVERY_LIVE_ROOT"]) / ".attempts"

    if scenario in {"fallback", "terminal"} and count <= 3:
      attempts.parent.mkdir(parents=True, exist_ok=True)
      prior = int(attempts.read_text()) if attempts.exists() else 0
      attempts.write_text(str(prior + 1))
      raise SystemExit(7)
    if scenario == "terminal":
      raise SystemExit(9)
    if scenario == "clean-unready":
      raise SystemExit(0)

    class Handler(BaseHTTPRequestHandler):
      def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

      def log_message(self, _format, *_args):
        pass

    class Server(ThreadingHTTPServer):
      allow_reuse_address = True

    server = Server(("127.0.0.1", int(os.environ["FAKE_RECOVERY_PORT"])), Handler)

    def stop(_signum, _frame):
      marker = os.environ.get("FAKE_RECOVERY_TERM")
      if marker:
        Path(marker).write_text("terminated")
      os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if scenario in {"planned", "ready-crash"} and count == 1:
      exit_status = 0 if scenario == "planned" else 11
      timer = threading.Timer(2.5, lambda: os._exit(exit_status))
      timer.daemon = True
      timer.start()
    server.serve_forever()
  """))
  return child


def _start_recovery_supervisor(
  tmp_path: Path,
  scenario: str,
) -> tuple[subprocess.Popen, int, Path, Path]:
  port = _free_port()
  count = tmp_path / "launch-count"
  term = tmp_path / "term-marker"
  live_root = tmp_path / "recovery-live"
  child = _write_fake_recovery(tmp_path)
  env = os.environ.copy()
  env.update({
    "FAKE_RECOVERY_COUNT": str(count),
    "FAKE_RECOVERY_PORT": str(port),
    "FAKE_RECOVERY_SCENARIO": scenario,
    "FAKE_RECOVERY_TERM": str(term),
    "RECOVERY_LIVE_ROOT": str(live_root),
  })
  process = subprocess.Popen(
    [
      "sh", "-c",
      '. "$1"; shift; railway_supervise_recovery "$@"',
      "railway-recovery-supervisor",
      str(_SUPERVISION_PATH),
      f"http://127.0.0.1:{port}/recover/health",
      sys.executable,
      str(child),
    ],
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text=True,
  )
  return process, port, count, term


def _wait_until(predicate, *, timeout: float = 8) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if predicate():
      return
    time.sleep(0.05)
  raise AssertionError("condition did not become true before timeout")


def _health_ok(port: int) -> bool:
  try:
    with urllib.request.urlopen(
      f"http://127.0.0.1:{port}/recover/health",
      timeout=0.2,
    ) as response:
      return response.status == 200 and response.read() == b"ok"
  except OSError:
    return False


def _stop_supervisor(process: subprocess.Popen) -> tuple[str, str]:
  if process.poll() is None:
    process.terminate()
  try:
    return process.communicate(timeout=5)
  except subprocess.TimeoutExpired:
    os.kill(process.pid, signal.SIGKILL)
    return process.communicate(timeout=2)


def test_ready_recovery_clean_exit_reloads_without_exiting_supervisor(tmp_path):
  process, port, count, _term = _start_recovery_supervisor(tmp_path, "planned")
  try:
    _wait_until(
      lambda: count.exists() and int(count.read_text()) >= 2 and _health_ok(port),
    )
    assert process.poll() is None
    assert int(count.read_text()) == 2
  finally:
    _stop_supervisor(process)


def test_ready_recovery_nonzero_exit_is_terminal_without_relaunch(tmp_path):
  process, _port, count, _term = _start_recovery_supervisor(
    tmp_path,
    "ready-crash",
  )
  _stdout, stderr = process.communicate(timeout=5)

  assert process.returncode == 11
  assert int(count.read_text()) == 1
  assert "exited after readiness with status 11" in stderr


def test_three_live_failures_earn_a_fourth_baked_launch(tmp_path):
  process, port, count, _term = _start_recovery_supervisor(tmp_path, "fallback")
  try:
    _wait_until(
      lambda: count.exists() and int(count.read_text()) == 4 and _health_ok(port),
    )
    assert process.poll() is None
  finally:
    _stop_supervisor(process)


def test_fourth_pre_ready_failure_is_terminal(tmp_path):
  process, _port, count, _term = _start_recovery_supervisor(tmp_path, "terminal")
  _stdout, stderr = process.communicate(timeout=8)

  assert process.returncode == 9
  assert int(count.read_text()) == 4
  assert "without advancing its trusted-live attempts" in stderr


def test_clean_exit_before_readiness_is_terminal_not_an_unbounded_reload(
  tmp_path,
):
  process, _port, count, _term = _start_recovery_supervisor(
    tmp_path,
    "clean-unready",
  )
  _stdout, stderr = process.communicate(timeout=5)

  assert process.returncode == 1
  assert int(count.read_text()) == 1
  assert "without advancing its trusted-live attempts" in stderr


def test_supervisor_term_forwards_to_child_without_relaunch(tmp_path):
  process, port, count, term = _start_recovery_supervisor(tmp_path, "term")
  _wait_until(lambda: count.exists() and _health_ok(port))

  process.terminate()
  process.communicate(timeout=5)
  time.sleep(1.2)

  assert process.returncode == 0
  assert term.read_text() == "terminated"
  assert int(count.read_text()) == 1


@pytest.mark.parametrize(
  ("component", "child_status", "expected_status", "expected_count"),
  [
    ("recovery", 0, 1, 7),
    ("app", 7, 7, 8),
    ("gateway", 9, 9, 7),
  ],
)
def test_essential_child_exit_attributes_boot_attempt_to_owning_component(
  tmp_path,
  component,
  child_status,
  expected_status,
  expected_count,
):
  counter = tmp_path / ".boot-attempt"
  subprocess.run(
    [
      sys.executable, "-P", str(_COUNTER_PATH),
      "begin", str(counter), "boot-a",
    ],
    check=True,
    capture_output=True,
    text=True,
  )
  # Raise the exact current charge while keeping the helper-authenticated boot
  # id. The wait helper should restore 7 only for a non-app component.
  counter.write_text("8 2026-07-28T00:00:00Z boot-a\n")
  script = textwrap.dedent("""
    . "$1"
    component="$2"
    child_status="$3"
    (sleep 10) & gateway=$!
    (sleep 10) & app=$!
    (sleep 10) & recovery=$!
    case "$component" in
      gateway) kill "$gateway"; wait "$gateway" 2>/dev/null || true
               (exit "$child_status") & gateway=$! ;;
      app) kill "$app"; wait "$app" 2>/dev/null || true
           (exit "$child_status") & app=$! ;;
      recovery) kill "$recovery"; wait "$recovery" 2>/dev/null || true
                (exit "$child_status") & recovery=$! ;;
    esac
    railway_wait_for_essential_child_exit \
      "$gateway" "$app" "$recovery" "$4" "$5" boot-a 7 8
    result=$?
    kill "$gateway" "$app" "$recovery" 2>/dev/null || true
    wait "$gateway" 2>/dev/null || true
    wait "$app" 2>/dev/null || true
    wait "$recovery" 2>/dev/null || true
    printf '%s %s\n' "$result" "$(cut -d' ' -f1 "$5")"
  """)
  completed = subprocess.run(
    [
      "sh", "-c", script, "railway-child-wait",
      str(_SUPERVISION_PATH), component, str(child_status),
      str(_COUNTER_PATH), str(counter),
    ],
    check=True,
    capture_output=True,
    text=True,
    timeout=8,
  )

  assert completed.stdout.strip() == f"{expected_status} {expected_count}"


def test_simultaneous_app_and_recovery_exit_is_charged_to_app(tmp_path):
  counter = tmp_path / ".boot-attempt"
  subprocess.run(
    [
      sys.executable, "-P", str(_COUNTER_PATH),
      "begin", str(counter), "boot-a",
    ],
    check=True,
    capture_output=True,
    text=True,
  )
  counter.write_text("1 2026-07-28T00:00:00Z boot-a\n")
  script = textwrap.dedent("""
    . "$1"
    (sleep 10) & gateway=$!
    (exit 6) & app=$!
    (exit 7) & recovery=$!
    railway_wait_for_essential_child_exit \
      "$gateway" "$app" "$recovery" "$2" "$3" boot-a 0 1
    result=$?
    kill "$gateway" 2>/dev/null || true
    wait "$gateway" 2>/dev/null || true
    printf '%s %s\n' "$result" "$(cut -d' ' -f1 "$3")"
  """)
  completed = subprocess.run(
    [
      "sh", "-c", script, "railway-child-wait",
      str(_SUPERVISION_PATH), str(_COUNTER_PATH), str(counter),
    ],
    check=True,
    capture_output=True,
    text=True,
    timeout=8,
  )

  assert completed.stdout.strip() == "6 1"


def test_railway_entrypoint_supervises_every_essential_process():
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text()
  supervision = _SUPERVISION_PATH.read_text()
  assert "_wait_for_railway_child_exit" in entrypoint
  assert ". /app/scripts/railway_supervision.sh" in entrypoint
  assert "railway_supervise_recovery" in entrypoint
  assert "railway_wait_for_essential_child_exit" in entrypoint
  assert "boot_attempt_counter.py" in entrypoint
  assert (
    "automatic crash-loop restore is disabled for this boot"
    in entrypoint
  )
  early_gateway = entrypoint[
    entrypoint.index("Railway gateway exited before app startup."):
    entrypoint.index("_wait_for_railway_child_exit", entrypoint.index(
      "Railway gateway exited before app startup.",
    ))
  ]
  assert "railway_rollback_platform_boot_attempt" in early_gateway
  assert 'railway_child_running "$_app_pid"' in early_gateway
  assert 'railway_child_running "$_railway_app_pid"' in supervision
  assert 'wait "$_railway_app_pid"' in supervision
  assert 'wait "$_railway_gateway_pid"' in supervision
  assert 'wait "$_railway_recovery_pid"' in supervision
  assert "railway_recovery_live_attempts" in supervision
  assert "_MAX_LIVE_ATTEMPTS" not in supervision
  assert "recoveryd exited with status" not in entrypoint
  assert "A clean essential-child exit is still a service failure" in supervision
  assert ".boot-attempt.lock" in entrypoint

  protected = (
    Path(__file__).resolve().parents[2] / "protected-files.txt"
  ).read_text()
  assert "/app/scripts/boot_attempt_counter.py" in protected
  assert "/app/scripts/railway_supervision.sh" in protected

  railway = Path(__file__).resolve().parents[2] / "railway.toml"
  config = railway.read_text()
  assert 'restartPolicyType = "ON_FAILURE"' in config
  assert "restartPolicyMaxRetries" in config
