"""The boot transaction's failure report is what an operator and a repair agent
have to go on, so it must carry the failing command's own explanation."""

import json
import subprocess
from pathlib import Path

import pytest

from app import platform_boot, platform_update


def _git_refusal() -> subprocess.CalledProcessError:
  return subprocess.CalledProcessError(
    128, ["git", "read-tree", "-n", "-m", "-u", "before", "target"],
    output="", stderr="error: Entry 'backend/app/x.py' not uptodate. Cannot merge.\n",
  )


def test_failure_detail_carries_git_stderr_through_a_wrapping_error():
  try:
    try:
      raise _git_refusal()
    except subprocess.CalledProcessError as exc:
      raise platform_update.BootTransactionError("could not swap in the update") from exc
  except platform_update.BootTransactionError as wrapped:
    detail = platform_boot.failure_detail(wrapped)

  assert detail.startswith("BootTransactionError('could not swap in the update')")
  assert "stderr: error: Entry 'backend/app/x.py' not uptodate. Cannot merge." in detail


def test_activate_failure_is_printed_and_recorded_with_git_stderr(
  tmp_path, monkeypatch, capsys,
):
  log = tmp_path / "platform-boot.jsonl"
  marker = tmp_path / "boot-transaction"
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  monkeypatch.setattr(platform_update, "BOOT_TRANSACTION_MARKER", marker)
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-1")

  def refuse(_repo):
    raise _git_refusal()

  monkeypatch.setattr(platform_update, "settle_prepared_update_for_this_image", refuse)

  assert platform_boot.main(["platform_boot", "activate"]) == 1

  assert "not uptodate. Cannot merge." in capsys.readouterr().err
  assert not marker.exists()  # a failed transaction never publishes its protocol
  [record] = [json.loads(line) for line in log.read_text().splitlines()]
  assert record["boot_id"] == "boot-1"
  assert record["command"] == "activate" and record["ok"] is False
  assert "not uptodate. Cannot merge." in record["detail"]


def test_boot_log_keeps_only_the_most_recent_runs(tmp_path, monkeypatch):
  log = tmp_path / "platform-boot.jsonl"
  monkeypatch.setattr(platform_boot, "_BOOT_LOG_RECORDS", 3)
  for number in range(5):
    platform_boot.record_boot_run("guard", ok=True, detail=f"run {number}", log=log)

  details = [json.loads(line)["detail"] for line in log.read_text().splitlines()]
  assert details == ["run 2", "run 3", "run 4"]
  assert sorted(path.name for path in tmp_path.iterdir()) == ["platform-boot.jsonl"]


def test_an_unwritable_boot_log_never_fails_the_boot(tmp_path, capsys):
  missing_dir = tmp_path / "absent" / "platform-boot.jsonl"

  platform_boot.record_boot_run("activate", ok=True, detail="none", log=missing_dir)

  assert "could not record this run" in capsys.readouterr().err


ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"


def _shell_function(source: str, name: str) -> str:
  start = source.index(f"{name}() {{\n")
  return source[start:source.index("\n}\n", start) + 3]


@pytest.mark.parametrize(
  ("boot_results", "probe_results", "served", "unsettled"),
  [
    # A plain boot serves the checkout.
    ({"activate": [0], "guard": [0]}, [0], "direct", None),
    # Today's outage: activate fails. Serve the baked platform, never exit.
    ({"activate": [1]}, [], "baked", "activate"),
    ({"activate": [124]}, [], "baked", "activate"),  # a timed-out step
    ({"activate": [0], "guard": [1]}, [], "baked", "guard"),
    # A checkout that does not import with nothing to revert is settled.
    ({"activate": [0], "guard": [0], "revert": [3]}, [1], "baked", None),
    ({"activate": [0], "guard": [0, 0], "revert": [0]}, [1, 0], "direct", None),
    ({"activate": [0], "guard": [0], "revert": [1]}, [1], "baked", "revert"),
    ({"activate": [0], "guard": [0, 1], "revert": [0]}, [1], "baked", "guard"),
  ],
)
def test_a_failed_boot_step_serves_the_baked_platform_and_marks_updates_paused(
  tmp_path, boot_results, probe_results, served, unsettled,
):
  source = ENTRYPOINT.read_text(encoding="utf-8")
  marker = tmp_path / "platform-boot-unsettled"
  functions = (
    _shell_function(source, "_platform_boot_unsettled")
    + _shell_function(source, "_platform_serve_checkout")
  ).replace("/tmp/platform-boot-unsettled", str(marker))
  assert "exit" not in _shell_function(source, "_platform_serve_checkout")
  results = tmp_path / "results"
  results.mkdir()
  for step, codes in boot_results.items():
    (results / step).write_text("\n".join(map(str, codes)) + "\n")
  (results / "probe").write_text("".join(f"{code}\n" for code in probe_results))
  script = f"""
next_result() {{
  file="{results}/$1"
  code=$(head -n 1 "$file") || exit 99
  [ -n "$code" ] || {{ echo "unexpected $1" >&2; exit 99; }}
  tail -n +2 "$file" > "$file.rest" && mv "$file.rest" "$file"
  return "$code"
}}
_platform_boot() {{ next_result "$1"; }}
_platform_import_probe() {{ next_result probe; }}
_platform_use_direct() {{ echo served=direct; }}
_platform_use_baked() {{ echo served=baked; }}
{functions}
_platform_serve_checkout
"""
  run = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)

  assert run.returncode == 0, run.stderr
  assert run.stdout.strip().splitlines()[-1] == f"served={served}"
  if unsettled is None:
    assert not marker.exists()
  else:
    assert marker.read_text() == f"{unsettled}\n"
    assert "platform-boot.jsonl" in run.stderr


def test_revert_with_nothing_to_revert_is_an_answer_not_a_failure(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(platform_boot, "BOOT_LOG", tmp_path / "platform-boot.jsonl")
  monkeypatch.setattr(platform_update, "revert_failed_update", lambda _repo: False)

  assert platform_boot.main(["platform_boot", "revert"]) == platform_boot.NOTHING_TO_REVERT
  assert platform_boot.NOTHING_TO_REVERT == 3  # entrypoint.sh tests for 3
