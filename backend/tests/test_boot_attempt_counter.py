import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


_HELPER = (
  Path(__file__).resolve().parents[1] / "scripts" / "boot_attempt_counter.py"
)


def _run(*args: object) -> str:
  return subprocess.run(
    [sys.executable, "-P", str(_HELPER), *map(str, args)],
    check=True,
    capture_output=True,
    text=True,
  ).stdout.strip()


def _record(path: Path) -> tuple[int, str]:
  parts = path.read_text().split()
  return int(parts[0]), parts[2]


def test_begin_upgrades_legacy_record_and_returns_prior_and_current(tmp_path):
  counter = tmp_path / ".boot-attempt"
  counter.write_text("2 2026-07-28T00:00:00Z\n")

  assert _run("begin", counter, "boot-a") == "2 3"
  assert _record(counter) == (3, "boot-a")


def test_begin_treats_malformed_or_negative_legacy_count_as_zero(tmp_path):
  for index, legacy in enumerate((
    "not-a-count 2026-07-28T00:00:00Z\n",
    "-7 2026-07-28T00:00:00Z\n",
  )):
    counter = tmp_path / f".boot-attempt-{index}"
    counter.write_text(legacy)

    assert _run("begin", counter, "boot-a") == "0 1"
    assert _record(counter) == (1, "boot-a")


def test_stale_boot_cannot_reset_or_roll_back_a_newer_boot(tmp_path):
  counter = tmp_path / ".boot-attempt"
  assert _run("begin", counter, "boot-a") == "0 1"
  assert _run("begin", counter, "boot-b") == "1 2"

  assert _run("reset", counter, "boot-a") == "0"
  assert _run("rollback", counter, "boot-a", 1, 0) == "0"
  assert _record(counter) == (2, "boot-b")


def test_rollback_is_an_exact_compare_and_swap(tmp_path):
  counter = tmp_path / ".boot-attempt"
  assert _run("begin", counter, "boot-a") == "0 1"

  assert _run("rollback", counter, "boot-a", 2, 0) == "0"
  assert _record(counter) == (1, "boot-a")
  assert _run("rollback", counter, "boot-a", 1, 0) == "1"
  assert _record(counter) == (0, "boot-a")


def test_atomic_replacement_preserves_existing_counter_mode(tmp_path):
  counter = tmp_path / ".boot-attempt"
  counter.write_text("0 2026-07-28T00:00:00Z\n")
  counter.chmod(0o640)

  _run("begin", counter, "boot-a")
  _run("reset", counter, "boot-a")

  assert stat.S_IMODE(counter.stat().st_mode) == 0o640


def test_concurrent_health_reset_and_component_rollback_converge_to_zero(
  tmp_path,
):
  # Whichever command acquires the lock first, reset wins: rollback-first is
  # followed by reset(0), while reset-first makes rollback's expected count
  # stale. Repeat to exercise both scheduler orders without timing hooks.
  for index in range(12):
    counter = tmp_path / f".boot-attempt-{index}"
    assert _run("begin", counter, "boot-a") == "0 1"
    commands = (
      ("reset", counter, "boot-a"),
      ("rollback", counter, "boot-a", 1, 0),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
      list(pool.map(lambda args: _run(*args), commands))
    assert _record(counter) == (0, "boot-a")
