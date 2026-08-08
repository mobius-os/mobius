"""Self-hosted recovery is now a root shell in the LIVE app container.

The previous subsystem (isolated recovery/recovery-target Compose services, a
one-time-code state file, credential rotation, a flock lifecycle lock, and a
force-recreate of the app on finish) has been deleted. On a single-owner box the
operator already has host + Docker root, so recovery is simply
`docker exec -u 0` into the running container to repair /data/platform in place.
The app is never stopped or recreated. These tests pin that contract.
"""

import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MOBIUSCTL = ROOT / "scripts" / "mobiusctl"


def _compose():
  return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def _fake_docker(bin_dir: Path, log: Path, *, app_id: str) -> Path:
  """A recording `docker` shim. `app_id` is what `compose ps -q app` reports;
  empty means the container is not running."""
  bin_dir.mkdir(parents=True, exist_ok=True)
  docker = bin_dir / "docker"
  docker.write_text(
    "#!/bin/sh\n"
    'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
    'case "$*" in\n'
    '  *compose*"ps -q app"*) printf "%s\\n" "$FAKE_APP_ID" ;;\n'
    '  *exec*"command -v bash"*) exit 0 ;;\n'
    "  *) exit 0 ;;\n"
    "esac\n"
  )
  docker.chmod(0o755)
  return docker


def _run(args, tmp_path, app_id=""):
  bin_dir = tmp_path / "bin"
  log = tmp_path / "docker.log"
  log.write_text("")
  _fake_docker(bin_dir, log, app_id=app_id)
  env = {
    **os.environ,
    "PATH": f"{bin_dir}:{os.environ['PATH']}",
    "DOCKER_LOG": str(log),
    "FAKE_APP_ID": app_id,
  }
  result = subprocess.run(
    [str(MOBIUSCTL), *args],
    cwd=ROOT,
    env=env,
    text=True,
    capture_output=True,
    timeout=10,
  )
  return result, log.read_text()


# --- The subsystem is gone from Compose -------------------------------------


def test_no_recovery_services_or_private_network():
  compose = _compose()
  assert set(compose["services"]) == {"caddy", "app"}
  assert "networks" not in compose
  # app + caddy never joined a bespoke recovery segment.
  for name in ("caddy", "app"):
    assert "networks" not in compose["services"][name]


# --- The script no longer stops, recreates, or manages credentials ----------


def test_mobiusctl_has_no_disruptive_or_credential_machinery():
  script = MOBIUSCTL.read_text()
  for forbidden in (
    "stop app",
    "--force-recreate",
    "MOBIUS_RECOVERY_TARGET_TOKEN",
    "MOBIUS_RECOVERY_LOCAL_TOKEN",
    "recovery_private",
    "recovery.env",
    "flock",
    "rotate_state",
  ):
    assert forbidden not in script, f"expected {forbidden!r} to be gone"
  # The honest primitive: a root shell in the live container.
  assert "docker exec -u 0 -it" in script


# --- Behaviour --------------------------------------------------------------


def test_running_container_opens_root_shell_in_place(tmp_path):
  result, log = _run(["recovery"], tmp_path, app_id="abc123")
  assert result.returncode == 0, result.stderr
  # Resolved the live container, then exec'd a root shell into it.
  assert "compose -p mobius" in log and "ps -q app" in log
  assert "exec -u 0 -it abc123 bash" in log
  # Never stopped or recreated anything.
  assert "stop app" not in log
  assert "force-recreate" not in log
  assert "up -d" not in log


def test_stopped_container_prints_start_guidance_and_fails(tmp_path):
  result, log = _run(["recovery"], tmp_path, app_id="")
  assert result.returncode == 1
  assert "not running" in result.stderr
  assert "up -d app" in result.stderr
  # It only probed for the container; it did not exec or mutate anything.
  assert "exec -u 0" not in log


def test_retired_subcommands_explain_and_do_not_touch_docker(tmp_path):
  for sub in ("start", "reopen", "status", "finish"):
    result, log = _run(["recovery", sub], tmp_path, app_id="abc123")
    assert result.returncode == 64, sub
    assert "retired" in result.stderr
    assert "scripts/mobiusctl recovery" in result.stderr
    assert log.strip() == "", f"{sub} must not call docker"


def test_usage_on_unknown_command(tmp_path):
  result, log = _run(["update"], tmp_path)
  assert result.returncode == 64
  assert "Usage: scripts/mobiusctl recovery" in result.stderr
  assert log.strip() == ""
