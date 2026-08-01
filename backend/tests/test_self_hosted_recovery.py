import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def _compose():
  return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_external_recovery_worker_is_unprivileged_and_has_no_host_control():
  worker = _compose()["services"]["recovery"]
  assert worker["profiles"] == ["recovery"]
  # The hardened worker must be its own PID 1 so it can reject wrappers that
  # retain bootstrap authority; Compose's `init` shim would make it PID 2.
  assert worker.get("init") is None
  # The tmpfs-backed one-time-code ledger is not durable. An automatic restart
  # would reconstruct it from the unchanged Compose environment and replay a
  # consumed code, so only mobiusctl `reopen` may recreate this container.
  assert worker["restart"] == "no"
  assert worker["read_only"] is True
  assert worker["cap_drop"] == ["ALL"]
  assert "no-new-privileges:true" in worker["security_opt"]
  assert worker.get("volumes", []) == []
  assert all("docker.sock" not in item for item in worker.get("volumes", []))
  assert worker["ports"] == [
    "127.0.0.1:${MOBIUS_RECOVERY_PORT:-18003}:8000"
  ]
  assert "/state:uid=10001,gid=10001,mode=0700" in worker["tmpfs"]
  assert "HOME=/state" in worker["environment"]
  assert "MOBIUS_RECOVERY_SECURE_COOKIE=0" in worker["environment"]


def test_only_root_target_mounts_the_stopped_app_data():
  services = _compose()["services"]
  target = services["recovery-target"]
  worker = services["recovery"]
  assert target["profiles"] == ["recovery"]
  assert target.get("init") is None
  # A restarted target would accept the same still-live bearer. It must remain
  # exited until `reopen` removes both services and rotates both credentials.
  assert target["restart"] == "no"
  assert target["read_only"] is True
  assert set(target["cap_drop"]) == {
    "NET_ADMIN", "NET_RAW", "SYS_ADMIN", "SYS_PTRACE",
  }
  assert target["tmpfs"] == ["/tmp", "/run"]
  assert target["volumes"] == ["app_data:/data"]
  assert any(
    item == "MOBIUS_BOOT_MODE=recovery" for item in target["environment"]
  )
  assert (
    "MOBIUS_RECOVERY_TARGET_EXPIRES_AT="
    "${MOBIUS_RECOVERY_TARGET_EXPIRES_AT:-0}"
  ) in target["environment"]
  assert target["healthcheck"] == {"disable": True}
  assert worker.get("volumes") is None
  assert worker["depends_on"]["recovery-target"]["condition"] == "service_started"


def test_lifecycle_pulls_latest_before_stopping_app_and_restores_on_finish():
  script = (ROOT / "scripts" / "mobiusctl").read_text()
  assert "/?token=" not in script
  assert "Paste this one-time code into the Recovery sign-in form" in script
  start = script.index("compose pull recovery")
  stop = script.index("compose stop app", start)
  launch = script.index("compose up -d --force-recreate recovery-target recovery")
  assert start < stop < launch

  finish = script.index("finish_recovery()")
  down = script.index("recovery_down_strict", finish)
  app_up = script.index("normal_compose up -d --force-recreate app", finish)
  health = script.index("wait_for_normal_app", app_up)
  assert down < app_up < health


def test_lifecycle_directs_crashed_services_through_credential_rotation():
  script = (ROOT / "scripts" / "mobiusctl").read_text()
  assert "required after either isolated service exits" in script
  assert "If either recovery service exits, do not restart it" in script
  assert "If either service exited, do not restart it" in script
  assert script.count("scripts/mobiusctl recovery reopen") >= 3


def test_app_sudo_is_explicit_and_defaults_off():
  app_env = _compose()["services"]["app"]["environment"]
  assert "MOBIUS_AGENT_SUDO=${MOBIUS_AGENT_SUDO:-0}" in app_env


def _mobiusctl(tmp_path, action, *, state=None, extra_env=None):
  state_path = state or tmp_path / "recovery.env"
  env = os.environ.copy()
  env["MOBIUS_RECOVERY_STATE_FILE"] = str(state_path)
  env["MOBIUS_RECOVERY_LOCK_DIR"] = str(tmp_path)
  env.update(extra_env or {})
  result = subprocess.run(
    [str(ROOT / "scripts" / "mobiusctl"), "recovery", action],
    cwd=ROOT,
    env=env,
    text=True,
    capture_output=True,
    timeout=10,
  )
  return result, state_path


def _write_recovery_state(
  path, target="t" * 43, local="l" * 43, expires_at=None,
):
  expires_at = expires_at or int(time.time()) + 3600
  path.write_text(
    f"MOBIUS_RECOVERY_TARGET_TOKEN={target}\n"
    f"MOBIUS_RECOVERY_TARGET_EXPIRES_AT={expires_at}\n"
    f"MOBIUS_RECOVERY_LOCAL_TOKEN={local}\n"
    "MOBIUS_RECOVERY_PORT=18003\n"
    "MOBIUS_RECOVERY_IMAGE=ghcr.io/mobius/recovery:stable\n"
  )
  path.chmod(0o600)


def _read_recovery_state(path):
  return dict(
    line.split("=", 1)
    for line in path.read_text().splitlines()
  )


def _fake_docker(tmp_path):
  bin_dir = tmp_path / "bin"
  bin_dir.mkdir()
  docker = bin_dir / "docker"
  docker.write_text(
    "#!/bin/sh\n"
    "printf '%s\\n' \"$*\" >>\"$DOCKER_LOG\"\n"
    "case \"$*\" in\n"
    "  *\"${DOCKER_FAIL_MATCH:-__never__}\"*) exit 42 ;;\n"
    "  *\"${DOCKER_BLOCK_MATCH:-__never__}\"*)\n"
    "    : >\"$DOCKER_BLOCK_READY\"\n"
    "    while [ ! -e \"$DOCKER_BLOCK_RELEASE\" ]; do sleep 0.05; done ;;\n"
    "esac\n"
    "case \"$*\" in\n"
    "  *\"${DOCKER_REMAINING_MATCH:-__never__}\"*) printf 'remaining-container\\n' ;;\n"
    "  *'ps -q app'*) printf 'fake-app\\n' ;;\n"
    "  inspect*fake-app*) printf '%s\\n' \"${DOCKER_APP_HEALTH:-healthy}\" ;;\n"
    "esac\n"
    "exit 0\n"
  )
  docker.chmod(0o755)
  log = tmp_path / "docker.log"
  return {
    "PATH": f"{bin_dir}:{os.environ['PATH']}",
    "DOCKER_LOG": str(log),
  }, log


def test_reopen_rotates_both_tokens_without_starting_normal_app(tmp_path):
  state = tmp_path / "recovery.env"
  old_target = "t" * 43
  old_local = "l" * 43
  _write_recovery_state(state, old_target, old_local)
  fake_env, log = _fake_docker(tmp_path)

  result, _ = _mobiusctl(
    tmp_path, "reopen", state=state, extra_env=fake_env,
  )

  assert result.returncode == 0, result.stderr
  rotated = _read_recovery_state(state)
  assert rotated["MOBIUS_RECOVERY_TARGET_TOKEN"] != old_target
  assert rotated["MOBIUS_RECOVERY_LOCAL_TOKEN"] != old_local
  assert int(rotated["MOBIUS_RECOVERY_TARGET_EXPIRES_AT"]) > int(time.time())
  assert rotated["MOBIUS_RECOVERY_LOCAL_TOKEN"] in result.stdout
  assert old_local not in result.stdout
  assert (state.stat().st_mode & 0o777) == 0o600
  commands = log.read_text().splitlines()
  app_stop = next(
    i for i, line in enumerate(commands) if "stop app" in line
  )
  pull = next(i for i, line in enumerate(commands) if "pull recovery" in line)
  stop = next(
    i for i, line in enumerate(commands)
    if "stop recovery recovery-target" in line
  )
  remove = next(
    i for i, line in enumerate(commands)
    if "rm -f recovery recovery-target" in line
  )
  launch = next(
    i for i, line in enumerate(commands)
    if "up -d --force-recreate recovery-target recovery" in line
  )
  assert app_stop < pull < stop < remove < launch
  assert all("up -d app" not in line for line in commands)


def test_failed_reopen_keeps_app_stopped_and_new_tokens_retryable(tmp_path):
  state = tmp_path / "recovery.env"
  old_target = "t" * 43
  old_local = "l" * 43
  _write_recovery_state(state, old_target, old_local)
  fake_env, log = _fake_docker(tmp_path)
  fake_env["DOCKER_FAIL_MATCH"] = (
    "up -d --force-recreate recovery-target recovery"
  )

  result, _ = _mobiusctl(
    tmp_path, "reopen", state=state, extra_env=fake_env,
  )

  assert result.returncode == 42
  assert "ordinary Mobius app remains stopped" in result.stderr
  rotated = _read_recovery_state(state)
  assert rotated["MOBIUS_RECOVERY_TARGET_TOKEN"] != old_target
  assert rotated["MOBIUS_RECOVERY_LOCAL_TOKEN"] != old_local
  assert all("up -d app" not in line for line in log.read_text().splitlines())


def test_reopen_requires_an_active_valid_state(tmp_path):
  fake_env, log = _fake_docker(tmp_path)
  result, state = _mobiusctl(
    tmp_path, "reopen", extra_env=fake_env,
  )
  assert result.returncode != 0
  assert "recovery start" in result.stderr
  assert not state.exists()
  assert not log.exists()


def test_lifecycle_rejects_user_image_shell_injection_before_writing_state(
  tmp_path,
):
  marker = tmp_path / "injected"
  malicious = f"ghcr.io/mobius/recovery:stable\n$(touch {marker})"
  result, state = _mobiusctl(
    tmp_path,
    "start",
    extra_env={"MOBIUS_RECOVERY_IMAGE": malicious},
  )
  assert result.returncode != 0
  assert "unsafe characters" in result.stderr
  assert not marker.exists()
  assert not state.exists()


def test_start_mints_a_bounded_target_expiry(tmp_path):
  fake_env, _log = _fake_docker(tmp_path)
  before = int(time.time())

  result, state = _mobiusctl(
    tmp_path,
    "start",
    extra_env={**fake_env, "MOBIUS_RECOVERY_TTL_SECONDS": "300"},
  )

  assert result.returncode == 0, result.stderr
  expiry = int(_read_recovery_state(state)["MOBIUS_RECOVERY_TARGET_EXPIRES_AT"])
  assert before + 300 <= expiry <= int(time.time()) + 300


@pytest.mark.parametrize("ttl", ["299", "86401", "0300", "1.5"])
def test_start_rejects_unbounded_or_noncanonical_target_ttl(tmp_path, ttl):
  result, state = _mobiusctl(
    tmp_path,
    "start",
    extra_env={"MOBIUS_RECOVERY_TTL_SECONDS": ttl},
  )
  assert result.returncode != 0
  assert "300 to 86400" in result.stderr
  assert not state.exists()


def test_lifecycle_never_sources_injected_state(tmp_path):
  marker = tmp_path / "injected"
  state = tmp_path / "recovery.env"
  state.write_text(
    "MOBIUS_RECOVERY_TARGET_TOKEN=" + "t" * 43 + "\n"
    "MOBIUS_RECOVERY_TARGET_EXPIRES_AT=1800000000\n"
    "MOBIUS_RECOVERY_LOCAL_TOKEN=" + "l" * 43 + "\n"
    "MOBIUS_RECOVERY_PORT=18003\n"
    "MOBIUS_RECOVERY_IMAGE=ghcr.io/mobius/recovery:stable\n"
    f"$(touch {marker})\n"
  )
  state.chmod(0o600)
  result, _ = _mobiusctl(tmp_path, "status", state=state)
  assert result.returncode != 0
  assert "unexpected or malformed line" in result.stderr
  assert not marker.exists()


def test_lifecycle_rejects_unsafe_state_files(tmp_path):
  content = (
    "MOBIUS_RECOVERY_TARGET_TOKEN=" + "t" * 43 + "\n"
    f"MOBIUS_RECOVERY_TARGET_EXPIRES_AT={int(time.time()) + 3600}\n"
    "MOBIUS_RECOVERY_LOCAL_TOKEN=" + "l" * 43 + "\n"
    "MOBIUS_RECOVERY_PORT=18003\n"
    "MOBIUS_RECOVERY_IMAGE=ghcr.io/mobius/recovery:stable\n"
  )

  permissive = tmp_path / "permissive.env"
  permissive.write_text(content)
  permissive.chmod(0o644)
  result, _ = _mobiusctl(tmp_path, "status", state=permissive)
  assert result.returncode != 0
  assert "permissions must be" in result.stderr

  original = tmp_path / "original.env"
  original.write_text(content)
  original.chmod(0o600)
  hardlink = tmp_path / "hardlink.env"
  os.link(original, hardlink)
  result, _ = _mobiusctl(tmp_path, "status", state=hardlink)
  assert result.returncode != 0
  assert "exactly one hard link" in result.stderr

  symlink = tmp_path / "symlink.env"
  symlink.symlink_to(original)
  result, _ = _mobiusctl(tmp_path, "status", state=symlink)
  assert result.returncode != 0
  assert "must not be a symlink" in result.stderr


def test_finish_refuses_to_start_app_when_recovery_stop_fails(tmp_path):
  state = tmp_path / "recovery.env"
  _write_recovery_state(state)
  fake_env, log = _fake_docker(tmp_path)
  fake_env["DOCKER_FAIL_MATCH"] = "stop recovery recovery-target"

  result, _ = _mobiusctl(
    tmp_path, "finish", state=state, extra_env=fake_env,
  )

  assert result.returncode == 42
  assert state.exists(), "credentials must survive an unverified teardown"
  commands = log.read_text().splitlines()
  assert all("up -d --force-recreate app" not in line for line in commands)


def test_finish_refuses_to_start_app_when_removed_services_still_exist(tmp_path):
  state = tmp_path / "recovery.env"
  _write_recovery_state(state)
  fake_env, log = _fake_docker(tmp_path)
  fake_env["DOCKER_REMAINING_MATCH"] = (
    "ps -aq recovery recovery-target"
  )

  result, _ = _mobiusctl(
    tmp_path, "finish", state=state, extra_env=fake_env,
  )

  assert result.returncode != 0
  assert "app stays stopped" in result.stderr
  assert state.exists()
  assert all(
    "up -d --force-recreate app" not in line
    for line in log.read_text().splitlines()
  )


def test_finish_recreates_app_only_after_verified_teardown(tmp_path):
  state = tmp_path / "recovery.env"
  _write_recovery_state(state)
  fake_env, log = _fake_docker(tmp_path)

  result, _ = _mobiusctl(
    tmp_path, "finish", state=state, extra_env=fake_env,
  )

  assert result.returncode == 0, result.stderr
  assert not state.exists()
  commands = log.read_text().splitlines()
  stop = next(
    i for i, line in enumerate(commands)
    if "stop recovery recovery-target" in line
  )
  remove = next(
    i for i, line in enumerate(commands)
    if "rm -f recovery recovery-target" in line
  )
  absent = next(
    i for i, line in enumerate(commands)
    if "ps -aq recovery recovery-target" in line
  )
  recreate = next(
    i for i, line in enumerate(commands)
    if "up -d --force-recreate app" in line
  )
  assert stop < remove < absent < recreate


def test_lifecycle_lock_rejects_concurrent_finish_or_reopen(tmp_path):
  state = tmp_path / "recovery.env"
  _write_recovery_state(state)
  fake_env, log = _fake_docker(tmp_path)
  ready = tmp_path / "block.ready"
  release = tmp_path / "block.release"
  fake_env.update({
    "MOBIUS_RECOVERY_STATE_FILE": str(state),
    "MOBIUS_RECOVERY_LOCK_DIR": str(tmp_path),
    "DOCKER_BLOCK_MATCH": "ps recovery-target recovery",
    "DOCKER_BLOCK_READY": str(ready),
    "DOCKER_BLOCK_RELEASE": str(release),
  })
  first = subprocess.Popen(
    [str(ROOT / "scripts" / "mobiusctl"), "recovery", "status"],
    cwd=ROOT,
    env={**os.environ, **fake_env},
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
  )
  try:
    for _attempt in range(100):
      if ready.exists():
        break
      time.sleep(0.02)
    assert ready.exists(), "first lifecycle never reached the fake Docker gate"

    second, _ = _mobiusctl(
      tmp_path,
      "finish",
      state=state,
      extra_env={**fake_env, "MOBIUS_RECOVERY_LOCK_WAIT_SECONDS": "0"},
    )
    assert second.returncode == 75
    assert "Another Mobius recovery/update lifecycle is active" in second.stderr
  finally:
    release.touch()
    first_stdout, first_stderr = first.communicate(timeout=5)
  assert first.returncode == 0, (first_stdout, first_stderr)
  assert sum(
    "ps recovery-target recovery" in line
    for line in log.read_text().splitlines()
  ) == 1
