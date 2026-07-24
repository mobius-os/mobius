import os
from pathlib import Path
import subprocess
import textwrap

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"
HELPERS_START = "# ── deploy disk policy helpers"
HELPERS_END = "# ── end deploy disk policy helpers"


def _read() -> str:
  return SCRIPT.read_text(encoding="utf-8")


def _helpers() -> str:
  text = _read()
  start = text.index(HELPERS_START)
  end = text.index(HELPERS_END, start)
  return text[start:end]


def _run_disk_check(
  free_bytes: str, *, allow_low_disk: bool,
) -> subprocess.CompletedProcess:
  harness = _helpers() + textwrap.dedent(f"""\
    info() {{ printf 'INFO %s\\n' "$1"; }}
    warn() {{ printf 'WARN %s\\n' "$1"; }}
    fail() {{ printf 'FAIL %s\\n' "$1" >&2; }}
    docker_storage_path() {{ printf '/docker-root\\n'; }}
    disk_available_bytes() {{ printf '%s\\n' {free_bytes!r}; }}
    report_docker_disk_usage() {{ printf 'DOCKER USAGE\\n'; }}
    DEPLOY_MIN_FREE_GB=15
    ALLOW_LOW_DISK={1 if allow_low_disk else 0}
    check_build_disk
  """)
  return subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )


@pytest.mark.parametrize("value", ["0", "-1", "15GB", "1.5"])
def test_invalid_disk_threshold_is_rejected_before_docker(value):
  result = subprocess.run(
    ["bash", str(SCRIPT), "--check"],
    env={**os.environ, "DEPLOY_MIN_FREE_GB": value},
    capture_output=True,
    text=True,
  )

  assert result.returncode == 2
  assert "DEPLOY_MIN_FREE_GB" in result.stderr
  assert "positive integer" in result.stderr


def test_disk_floor_accepts_exact_threshold():
  result = _run_disk_check(
    str(15 * 1024**3), allow_low_disk=False,
  )

  assert result.returncode == 0, result.stderr
  assert "required before build: 15 GiB" in result.stdout
  assert "DOCKER USAGE" in result.stdout


def test_disk_floor_refuses_low_space_without_override():
  result = _run_disk_check(
    str(15 * 1024**3 - 1), allow_low_disk=False,
  )

  assert result.returncode == 1
  assert "below the 15 GiB build floor" in result.stderr
  assert "--allow-low-disk" in result.stderr


def test_disk_floor_allows_low_space_only_by_explicit_override():
  result = _run_disk_check("1024", allow_low_disk=True)

  assert result.returncode == 0, result.stderr
  assert "proceeding by explicit override" in result.stdout


def test_unavailable_disk_probe_fails_closed_or_uses_same_override():
  refused = _run_disk_check("unavailable", allow_low_disk=False)
  overridden = _run_disk_check("unavailable", allow_low_disk=True)

  assert refused.returncode == 2
  assert "could not read free space" in refused.stderr
  assert overridden.returncode == 0
  assert "proceeding by explicit override" in overridden.stdout


@pytest.mark.parametrize(("image", "expected"), [
  ("mobius-app", "mobius-app:rollback-prev"),
  ("mobius-app:latest", "mobius-app:rollback-prev"),
  (
    "registry.example:5000/team/mobius:prod",
    "registry.example:5000/team/mobius:rollback-prev",
  ),
])
def test_rollback_tag_replaces_only_the_last_image_tag(image, expected):
  result = subprocess.run(
    ["bash", "-c", _helpers() + '\nrollback_tag_for_image "$1"', "_", image],
    capture_output=True,
    text=True,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout.strip() == expected


def test_cleanup_removes_only_the_superseded_rollback_image(tmp_path):
  docker_log = tmp_path / "docker.log"
  harness = _helpers() + textwrap.dedent(f"""\
    ok() {{ printf 'OK %s\n' "$1"; }}
    warn() {{ printf 'WARN %s\n' "$1"; }}
    docker() {{
      if [ "$1" = inspect ]; then
        printf 'sha256:new\n'
      elif [ "$1" = image ] && [ "$2" = rm ]; then
        printf 'REMOVE %s\n' "$3" >>{str(docker_log)!r}
      else
        return 1
      fi
    }}
    CONTAINER=mobius
    PREV_IMAGE=sha256:previous
    PREVIOUS_ROLLBACK_IMAGE=sha256:superseded
    remove_superseded_rollback_image
  """)
  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  calls = docker_log.read_text(encoding="utf-8")
  assert "REMOVE sha256:superseded" in calls
  assert "REMOVE sha256:previous" not in calls
  assert "REMOVE sha256:new" not in calls


def test_cleanup_never_uses_a_host_wide_image_prune():
  text = _read()

  assert "docker image prune" not in text
  assert 'docker image rm "$old_image"' in text
  assert '--filter "until=${DEPLOY_BUILD_CACHE_MAX_AGE_HOURS}h"' in text
  assert '--max-used-space "${DEPLOY_BUILD_CACHE_MAX_GB}GB"' in text
  assert '--keep-storage "${DEPLOY_BUILD_CACHE_MAX_GB}GB"' in text
  assert "docker builder prune -f" in text
  assert "docker builder prune -af" not in text


def test_admission_refusal_happens_before_rollback_tag_mutation():
  text = _read()
  build_step = text.index("# ── step 1: build")
  admission = text.index("if check_build_disk;", build_step)
  pin = text.index('if ! docker tag "$PREV_IMAGE" "$ROLLBACK_TAG";', admission)

  assert text.index('if [ "$TARGET" = "prod" ]; then', build_step) < admission
  assert admission < pin


def test_reflection_refresh_is_cheap_and_runs_after_success_cleanup():
  text = _read()
  refresh_call = text.rindex("refresh_reflection_resource_snapshot")
  recovery_failure = text.rindex(
    'if [ "${RECOVERYD_CUTOVER_FAILED:-0}" = "1" ]'
  )
  exact_cleanup = text.rindex("remove_superseded_rollback_image")

  assert "REFLECTION_RESOURCE_DEEP_SCAN=skip" in text
  assert recovery_failure < exact_cleanup < refresh_call


def test_deploy_script_still_parses():
  result = subprocess.run(
    ["bash", "-n", str(SCRIPT)], capture_output=True, text=True,
  )
  assert result.returncode == 0, result.stderr
