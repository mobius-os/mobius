"""Hermetic lock and stale-snapshot contract for production deploy cutover."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = (Path(__file__).parents[2] / "scripts/deploy-prod.sh").read_text()


def _function(name: str) -> str:
  start = f"{name}() {{"
  return start + SCRIPT.split(start, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


@pytest.fixture
def harness(tmp_path):
  state = tmp_path / "state"
  config = tmp_path / "config"
  state.mkdir(mode=0o700)
  config.mkdir(mode=0o700)
  lock = state / "replace.lock"
  lock.touch(mode=0o600)
  functions = _function("acquire_helper_replacement_fence") + _function(
    "revalidate_live_snapshot",
  )
  # Substitute only the two fixed host paths and the expected owner in this
  # disposable copy; production still requires root ownership and paths.
  functions = functions.replace("/var/lib/mobius-rebuild", str(state)).replace(
    "/etc/mobius-rebuild", str(config),
  ).replace('[ "$owner" != 0 ]', f'[ "$owner" != {os.geteuid()} ]')
  assert '[ "$owner" != 0 ]' in SCRIPT

  def run(*, target="prod", cid="cid0", image="image0", ref="ref0",
          config_hash="hash0"):
    script = f"""set -euo pipefail
fail() {{ echo "$*" >&2; }}
TARGET={target!r}; CONTAINER=mobius
RUNNING_CID=cid0; PREV_IMAGE=image0
RUNNING_IMAGE_REF=ref0; RUNNING_CONFIG_HASH=hash0
docker() {{
  case "$3" in
    '{{{{.Id}}}}') echo {cid!r} ;;
    '{{{{.Image}}}}') echo {image!r} ;;
    '{{{{.Config.Image}}}}') echo {ref!r} ;;
    *) echo {config_hash!r} ;;
  esac
}}
{functions}
acquire_helper_replacement_fence
revalidate_live_snapshot
echo CUTOVER_ALLOWED
"""
    return subprocess.run(["bash", "-c", script], text=True,
                          capture_output=True, check=False)

  return state, config, lock, run


def test_helper_lock_owner_blocks_deploy_before_live_mutation(harness):
  _, _, lock, run = harness
  with lock.open("r+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    result = run()
  assert result.returncode != 0
  assert "helper owns" in result.stderr
  assert "CUTOVER_ALLOWED" not in result.stdout


def test_pending_helper_journal_blocks_even_after_lock_acquisition(harness):
  state, _, _, run = harness
  (state / "transaction.json").write_text("{}")
  result = run()
  assert result.returncode != 0
  assert "pending transaction" in result.stderr
  assert "CUTOVER_ALLOWED" not in result.stdout


def test_inaccessible_or_missing_helper_lock_fails_closed(harness):
  _, _, lock, run = harness
  lock.chmod(0o000)
  denied = run()
  assert denied.returncode != 0
  assert "cannot open" in denied.stderr
  lock.unlink()
  missing = run()
  assert missing.returncode != 0
  assert "private lock is unavailable" in missing.stderr


@pytest.mark.parametrize("changed", ["cid", "image", "ref", "config_hash"])
def test_stale_live_snapshot_aborts_before_cutover(harness, changed):
  _, _, _, run = harness
  result = run(**{changed: "changed"})
  assert result.returncode != 0
  assert "changed during build/preflight" in result.stderr
  assert "CUTOVER_ALLOWED" not in result.stdout


def test_matching_snapshot_under_helper_lock_allows_cutover(harness):
  _, _, _, run = harness
  result = run()
  assert result.returncode == 0, result.stderr
  assert "CUTOVER_ALLOWED" in result.stdout


def test_nonproduction_does_not_take_root_helper_lock(harness):
  _, _, lock, run = harness
  with lock.open("r+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    result = run(target="test")
  assert result.returncode == 0, result.stderr


def test_early_privilege_gate_keeps_test_and_no_helper_compatible(tmp_path):
  function = _function("preflight_helper_deploy_privilege").replace(
    "/var/lib/mobius-rebuild", str(tmp_path / "state"),
  ).replace("/etc/mobius-rebuild", str(tmp_path / "config"))
  script = f"fail() {{ echo \"$*\" >&2; }}\n{function}\npreflight_helper_deploy_privilege\n"
  for target in ("prod", "test"):
    result = subprocess.run(["bash", "-c", f"TARGET={target}\n{script}"],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr

  (tmp_path / "state").mkdir()
  result = subprocess.run(["bash", "-c", f"TARGET=test\n{script}"],
                          text=True, capture_output=True, check=False)
  assert result.returncode == 0, result.stderr


def test_fence_precedes_source_install_and_cutover_mutations():
  assert SCRIPT.index("preflight_helper_deploy_privilege\n\n# ── step 1") < SCRIPT.index(
    'step "[1/4] docker compose build"',
  )
  assert SCRIPT.index("  acquire_helper_replacement_fence\n  revalidate_live_snapshot\n"
                      ) < SCRIPT.index("--bundle \"$container_bundle\"")
  assert SCRIPT.index('step "[2/4] docker compose up -d') < SCRIPT.index(
    "  acquire_helper_replacement_fence\n  revalidate_live_snapshot\n",
    SCRIPT.index('step "[2/4] docker compose up -d'),
  ) < SCRIPT.index("if ! prepare_chat_cutover; then")


def test_real_root_private_helper_requires_privileged_deploy(tmp_path):
  """Exercise kernel permissions, not an owner-substituted lock fixture."""
  if os.geteuid() == 0:
    pytest.skip("requires a non-root test runner to prove unprivileged refusal")
  if shutil.which("sudo") is None or subprocess.run(
    ["sudo", "-n", "true"], capture_output=True, check=False,
  ).returncode != 0:
    pytest.skip("passwordless sudo is unavailable for disposable root fixture")

  state = tmp_path / "root-state"
  config = tmp_path / "root-config"
  subprocess.run([
    "sudo", "-n", "install", "-d", "-m", "0700", str(state), str(config),
  ], check=True)
  subprocess.run([
    "sudo", "-n", "install", "-m", "0600", "/dev/null",
    str(state / "replace.lock"),
  ], check=True)
  try:
    functions = (
      _function("preflight_helper_deploy_privilege")
      + _function("acquire_helper_replacement_fence")
    ).replace("/var/lib/mobius-rebuild", str(state)).replace(
      "/etc/mobius-rebuild", str(config),
    )
    script = f"""set -euo pipefail
fail() {{ echo "$*" >&2; }}
TARGET=prod
{functions}
preflight_helper_deploy_privilege
echo BUILD_REACHED
acquire_helper_replacement_fence
echo CUTOVER_ALLOWED
"""
    ordinary = subprocess.run(["bash", "-c", script], text=True,
                              capture_output=True, check=False)
    assert ordinary.returncode != 0
    assert "run this production deployment with sudo" in ordinary.stderr
    assert "BUILD_REACHED" not in ordinary.stdout
    assert "CUTOVER_ALLOWED" not in ordinary.stdout

    privileged = subprocess.run(["sudo", "-n", "bash", "-c", script],
                                text=True, capture_output=True, check=False)
    assert privileged.returncode == 0, privileged.stderr
    assert "CUTOVER_ALLOWED" in privileged.stdout
  finally:
    subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(state), str(config)],
                   check=True)
