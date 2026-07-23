from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "docker-probe.sh"


def test_docker_probe_scripts_parse():
  for script in (HELPER, ROOT / "scripts" / "test-docker-probe.sh"):
    result = subprocess.run(
      ["bash", "-n", str(script)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_direct_docker_runs_use_the_lifecycle_helper():
  test_wrapper = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
  preship = (ROOT / "scripts" / "preship-gate.sh").read_text(encoding="utf-8")
  workflow = (
    ROOT / ".github" / "workflows" / "test.yml"
  ).read_text(encoding="utf-8")

  assert "docker-probe.sh" in test_wrapper
  assert "docker-probe.sh" in preship
  assert "scripts/docker-probe.sh" in workflow
  assert "scripts/test-docker-probe.sh" in workflow


def test_helper_owns_exact_container_identity_and_cleanup():
  helper = HELPER.read_text(encoding="utf-8")

  assert '--cidfile "$CID_FILE"' in helper
  assert '--name "$PROBE_NAME"' in helper
  assert 'docker rm -f "$ref"' in helper
  assert 'docker ps -aq --no-trunc --filter "id=$ref"' in helper
  assert "trap cleanup EXIT" in helper
  assert "trap 'exit 143' TERM" in helper
  assert "io.mobius.probe.started_at" in helper
  assert "io.mobius.probe.owner_token" in helper
  assert "docker stats --no-stream" in helper


def test_name_collision_never_deletes_unrelated_container(tmp_path):
  """A user-supplied name is not ownership.

  If docker run rejects the name because another service already holds it, the
  cleanup trap must preserve that service and return Docker's original 125.
  """
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  log = tmp_path / "docker.log"
  docker = fake_bin / "docker"
  docker.write_text(
    """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$DOCKER_LOG"
case "$1" in
  run)
    exit 125
    ;;
  inspect)
    if [ "${2:-}" = "--format" ]; then
      printf '%s\\n' 'unrelated-container-id unrelated-owner-token'
      exit 0
    fi
    exit 0
    ;;
  rm)
    exit 0
    ;;
esac
exit 0
""",
    encoding="utf-8",
  )
  docker.chmod(0o755)
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "DOCKER_LOG": str(log),
  }

  result = subprocess.run(
    [str(HELPER), "--timeout", "5", "--name", "production-db", "--", "image"],
    env=env,
    timeout=10,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )

  assert result.returncode == 125
  calls = log.read_text(encoding="utf-8").splitlines()
  assert any(call.startswith("inspect --format ") for call in calls)
  assert not any(call.startswith("rm ") for call in calls)


def test_cleanup_verification_failure_is_not_reported_as_success(tmp_path):
  """124/0 promise verified absence; daemon uncertainty must return 125."""
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  docker = fake_bin / "docker"
  docker.write_text(
    """#!/usr/bin/env bash
set -u
case "$1" in
  run)
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--cidfile" ]; then
        printf '%s\\n' 'owned-container-id' >"$2"
        break
      fi
      shift
    done
    exit 0
    ;;
  rm|ps)
    exit 1
    ;;
esac
exit 1
""",
    encoding="utf-8",
  )
  docker.chmod(0o755)
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
  }

  result = subprocess.run(
    [str(HELPER), "--timeout", "2", "--name", "owned-probe", "--", "image"],
    env=env,
    timeout=10,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )

  assert result.returncode == 125
