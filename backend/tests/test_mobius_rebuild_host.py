"""Trust and failure-boundary tests for the installed host worker."""

from __future__ import annotations

import importlib.util
import base64
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "mobius-rebuild-host.py"
INSTALLER = Path(__file__).parents[2] / "scripts" / "install-rebuild-helper.sh"
ENTRYPOINT = Path(__file__).parents[1] / "scripts" / "entrypoint.sh"
SPEC = importlib.util.spec_from_file_location("mobius_rebuild_host", SCRIPT)
assert SPEC and SPEC.loader
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


def test_entrypoint_restores_host_control_after_compatibility_chown():
  source = ENTRYPOINT.read_text(encoding="utf-8")

  broad_chown = source.index("if ! _own_as_mobius /data 2>/dev/null; then")
  control_hardening = source.index("chown -R root:root /data/mobius-rebuild")
  inbox_grant = source.index(
    "chown -R mobius:mobius /data/mobius-rebuild/inbox",
  )

  assert broad_chown < control_hardening < inbox_grant


def test_installer_enables_boot_time_reconciliation():
  source = INSTALLER.read_text(encoding="utf-8")

  assert 'mkdir -p "$DATA_SOURCE/mobius-rebuild/inbox"' in source
  assert 'chown "$APP_UID:$APP_GID" "$DATA_SOURCE/mobius-rebuild/inbox"' in source
  assert 'chmod 0700 "$DATA_SOURCE/mobius-rebuild/inbox"' in source
  assert 'install -d -o "$APP_UID"' not in source
  assert "mobius-rebuild-reconcile.service" in source
  assert "ExecStart=/usr/local/libexec/mobius-rebuild-host reconcile" in source
  assert "Before=mobius-rebuild.path" in source
  assert "WantedBy=multi-user.target" in source
  assert "systemctl enable mobius-rebuild-reconcile.service" in source
  assert 'bootstrap-runtime "$CID"' not in source
  assert "MOBIUS_RUNTIME_OVERLAY" not in source
  assert "target: /app/runtime" not in source
  assert "FROZEN_SOURCE=/etc/mobius-rebuild/compose.yml" in source
  assert '"com.docker.compose.project.environment_file"' in source
  assert 'ARGS+=(--env-file "$file")' in source
  assert "CURRENT_IMAGE=$(docker inspect" in source
  assert 'MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose' in source


def _frozen(tmp_path: Path, monkeypatch) -> tuple[dict, Path]:
  etc = tmp_path / "etc"
  data = tmp_path / "data"
  control = data / "mobius-rebuild"
  inbox = control / "inbox"
  etc.mkdir()
  inbox.mkdir(parents=True)
  config_path = etc / "config.json"
  compose = etc / "compose.yml"
  override = etc / "image.override.yml"
  for path in (config_path, compose, override):
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)
  control.chmod(0o755)
  monkeypatch.setattr(host, "CONFIG", config_path)
  monkeypatch.setattr(host, "COMPOSE", compose)
  monkeypatch.setattr(host, "OVERRIDE", override)
  value = {"version": 3, "project": "mobius", "data_dir": str(data)}
  return value, control


def test_frozen_config_accepts_minimal_root_owned_boundary(tmp_path, monkeypatch):
  value, control = _frozen(tmp_path, monkeypatch)

  result = host.validate_config(value, trusted_uid=os.getuid())

  assert result["project"] == "mobius"
  assert result["control_dir"] == control
  assert set(value) == {"version", "project", "data_dir"}


def test_frozen_config_rejects_group_writable_input(tmp_path, monkeypatch):
  value, _control = _frozen(tmp_path, monkeypatch)
  host.COMPOSE.chmod(0o620)

  with pytest.raises(ValueError, match="not root-controlled"):
    host.validate_config(value, trusted_uid=os.getuid())


def test_frozen_config_rejects_symlinked_input(tmp_path, monkeypatch):
  value, _control = _frozen(tmp_path, monkeypatch)
  target = host.COMPOSE.with_name("mutable.yml")
  target.write_text("{}\n", encoding="utf-8")
  host.COMPOSE.unlink()
  host.COMPOSE.symlink_to(target)

  with pytest.raises(ValueError, match="may not use symlinks"):
    host.validate_config(value, trusted_uid=os.getuid())


def test_served_generation_requires_the_image_runtime(monkeypatch):
  def execute(args, **_kwargs):
    payload = '{"sha":"' + "a" * 40 + '"}' if "curl" in args else "[]"
    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

  monkeypatch.setattr(host, "docker_command", execute)
  host.verify_served_generation("cid", "a" * 40)

  def mounted(args, **_kwargs):
    payload = (
      '{"sha":"' + "a" * 40 + '"}' if "curl" in args else
      '[{"Destination":"/app/runtime"}]'
    )
    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

  monkeypatch.setattr(host, "docker_command", mounted)
  with pytest.raises(host.ProvenanceRejected, match="image's protected runtime"):
    host.verify_served_generation("cid", "a" * 40)


def _provenance_command(scenario, calls):
  expected = "a" * 40

  def command(args, **_kwargs):
    if "curl" in args:
      calls.append("curl")
      if scenario == "curl_oserror":
        raise OSError("private Docker failure")
      output = (
        "{" if scenario == "invalid_json" else
        "[]" if scenario == "invalid_version_shape" else
        '{"sha":"not-a-sha"}' if scenario == "invalid_sha" else
        json.dumps({"sha": "b" * 40 if scenario == "wrong_revision" else expected})
      )
      return subprocess.CompletedProcess(
        args, 7 if scenario == "curl_nonzero" else 0, stdout=output, stderr="private stderr",
      )
    if args[1:3] == ["container", "inspect"]:
      calls.append("mount_probe")
      if scenario == "mount_timeout":
        raise subprocess.TimeoutExpired(args, 10)
      if scenario == "mount_calledprocess":
        raise subprocess.CalledProcessError(1, args)
      output = (
        "{" if scenario == "invalid_mount_json" else
        "{}" if scenario == "invalid_mount_shape" else
        "[null]" if scenario == "invalid_mount_item" else
        '[{"Destination":"/app/runtime"}]' if scenario == "protected_runtime" else
        "[]"
      )
      return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
    calls.append("other_docker")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  return command


@pytest.mark.parametrize("scenario", [
  "curl_nonzero", "invalid_json", "invalid_version_shape", "invalid_sha",
  "invalid_mount_json", "invalid_mount_shape", "invalid_mount_item",
  "curl_oserror", "mount_timeout", "mount_calledprocess",
])
def test_unconfirmed_served_provenance_never_claims_rejection(monkeypatch, scenario):
  monkeypatch.setattr(host, "docker_command", _provenance_command(scenario, []))
  with pytest.raises(host.ProvenanceUnconfirmed) as error:
    host.verify_served_generation("cid", "a" * 40)
  assert "private" not in str(error.value)


@pytest.mark.parametrize("scenario", ["wrong_revision", "protected_runtime"])
def test_well_formed_served_mismatch_is_proven_rejection(monkeypatch, scenario):
  monkeypatch.setattr(host, "docker_command", _provenance_command(scenario, []))
  with pytest.raises(host.ProvenanceRejected):
    host.verify_served_generation("cid", "a" * 40)


def test_replacement_verifies_provenance_before_retiring_chat_handoff():
  source = SCRIPT.read_text(encoding="utf-8")
  run = source[source.index("def run()") : source.index("def reconcile()")]
  healthy = run.index("if not wait_healthy(config_value)")
  provenance = run.index("verify_served_generation(", healthy)
  finalize = run.index("finish_verified(", provenance)

  assert healthy < provenance < finalize


def _worker_paths(tmp_path: Path, monkeypatch):
  # Most controller tests simulate a consumed boot; real-ledger tests below
  # restore the actual read-only proof against a private on-disk ledger.
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda *_a, **_k: True)
  state = tmp_path / "state"
  inbox = tmp_path / "control" / "inbox"
  state.mkdir()
  inbox.mkdir(parents=True)
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "LOCK", state / "replace.lock")
  monkeypatch.setattr(host, "STATUS", state / "status.json")
  monkeypatch.setattr(host, "IMAGES", state / "images.json")
  monkeypatch.setattr(host, "TRANSACTION", state / "transaction.json")
  monkeypatch.setattr(host, "FAILED_TARGET_LOG", state / "failed-target.json")
  data = tmp_path / "data"
  data.mkdir()
  config = {
    "project": "mobius", "control_dir": inbox.parent, "data_dir": data,
  }
  monkeypatch.setattr(host, "config", lambda: config)
  return config, inbox


def test_compose_never_mounts_a_generated_runtime(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  calls = []

  def execute(args, **kwargs):
    calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  monkeypatch.setattr(host, "docker_command", execute)
  host.compose(config, "ps")

  _args, kwargs = calls[0]
  assert "MOBIUS_RUNTIME_OVERLAY" not in kwargs["env"]




























def test_no_change_does_not_drain_active_chats(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "c" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(
    host, "request_drain",
    lambda *_args: (_ for _ in ()).throw(AssertionError("no-change must not drain")),
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 0
  assert statuses[-1]["state"] == "no_change"




def test_request_is_claimed_on_the_control_filesystem(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "e" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  real_replace = host.os.replace
  claims = []

  def same_filesystem_replace(source, target):
    if Path(source) == request:
      claims.append(Path(target))
      assert Path(target).parent == config["control_dir"]
    return real_replace(source, target)

  monkeypatch.setattr(host.os, "replace", same_filesystem_replace)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert len(claims) == 1
  assert not claims[0].exists()


def test_worker_locks_before_exposing_claim_to_reconcile(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "1" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  order = []
  real_replace = host.os.replace
  real_flock = host.fcntl.flock

  def record_flock(fd, operation):
    order.append("lock")
    return real_flock(fd, operation)

  def record_replace(source, target):
    if Path(source) == request:
      order.append("claim")
    return real_replace(source, target)

  monkeypatch.setattr(host.fcntl, "flock", record_flock)
  monkeypatch.setattr(host.os, "replace", record_replace)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert order[:2] == ["lock", "claim"]


def test_worker_waits_for_boot_reconcile_before_claiming(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "2" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  attempts = []

  def reconcile_then_release(_fd, _operation):
    attempts.append("lock")
    if len(attempts) == 1:
      raise BlockingIOError

  monkeypatch.setattr(host.fcntl, "flock", reconcile_then_release)
  monkeypatch.setattr(host.time, "monotonic", lambda: 0)
  monkeypatch.setattr(host.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert attempts == ["lock", "lock"]
  assert not request.exists()


def test_failed_request_claim_is_terminal_and_retryable(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{"f" * 40}"}}', encoding="utf-8",
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )
  monkeypatch.setattr(
    host.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("claim failed")),
  )

  assert host.run() == 1
  assert statuses[-1]["state"] == "failed"
  # It was never claimed, so it may be another request by now: it stays for
  # the owner to withdraw instead of being deleted unverified.
  assert request.exists()


@pytest.mark.parametrize("state", ["replacing", "verifying", "needs_recovery"])
def test_lock_loser_preserves_active_operation_and_queued_request(tmp_path, monkeypatch, state):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  host.write_status(
    config, operation_id="1" * 32, request_nonce="2" * 32,
    expected_sha="a" * 40, state=state, code="active-operation",
  )
  host.write_transaction(host.transaction_record(
    "1" * 32, "a" * 40, "2" * 32, "sha256:previous", "sha256:target",
  ))
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "b" * 40, "nonce": "3" * 32,
  }))
  paths = (host.STATUS, config["control_dir"] / "status.json", host.TRANSACTION, request)
  before = [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths]
  acquire = host.acquire_lock
  monkeypatch.setattr(host, "acquire_lock", lambda lock: acquire(lock, timeout=0))
  monkeypatch.setattr(host, "docker_command", lambda *_a, **_k: pytest.fail("lock loser ran Docker"))

  # Separate open file descriptions contend through the real kernel flock,
  # not a mock that merely raises BlockingIOError.
  with host.LOCK.open("a+") as owner:
    host.fcntl.flock(owner, host.fcntl.LOCK_EX | host.fcntl.LOCK_NB)
    assert host.run() == 1
    with host.LOCK.open("a+") as contender:
      with pytest.raises(BlockingIOError):
        acquire(contender, timeout=0)

  assert [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths] == before
  assert not list(config["control_dir"].glob(".request-*.json"))




@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_replacement_drains_then_rolls_back_after_cutover_error(tmp_path, monkeypatch, cleanup_fails):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "d" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "sha256:old"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "sha256:new"
  ))
  snapshots = iter([("cid", "sha256:new", "dead"), ("old", "sha256:old", "healthy")])
  monkeypatch.setattr(host, "container_health", lambda *_a: next(snapshots))
  monkeypatch.setattr(host, "docker_command", lambda *_a, **_k: None)
  if cleanup_fails:
    monkeypatch.setattr(host, "discard_pulled_image", lambda *_a:
                        (_ for _ in ()).throw(RuntimeError("cleanup failed")))
  order = []
  ready = inbox / "ready"
  monkeypatch.setattr(
    host, "request_drain", lambda *_args: order.append("drain") or ready,
  )
  monkeypatch.setattr(host, "restart_ledger", lambda *_args, **_kwargs: True)

  def compose(_config, *args, image=None, **_kwargs):
    order.append(f"compose:{image}")
    if image == host.TARGET_TAG:
      raise RuntimeError("cutover failed")

  monkeypatch.setattr(host, "compose", compose)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 1
  assert order == [
    "drain",
    f"compose:{host.TARGET_TAG}",
    "compose:sha256:old",
  ]
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == "replacement_failed"


@pytest.mark.parametrize("outcome_write_fails", [False, True])
def test_verified_success_never_rolls_back_for_handoff_or_outcome_write_failure(
  tmp_path, monkeypatch, outcome_write_fails,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "9" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  # The running container holds the previous image until Compose replaces it.
  replaced = []
  monkeypatch.setattr(
    host, "app_container", lambda _config: ("cid", "new" if replaced else "old"),
  )
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "new"
  ))
  monkeypatch.setattr(host, "request_drain", lambda *_args: None)
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: replaced.append(1))
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(
    host, "restart_ledger",
    lambda _config, _cid, command, _operation, **_kwargs:
      command != "finalize-cutover",
  )
  monkeypatch.setattr(host, "adopt_from_image", lambda _image: "not adopted: test")
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  write = host.write_transaction

  def persist(value):
    if outcome_write_fails and value.get("outcome"):
      raise OSError("outcome write failed")
    write(value)

  monkeypatch.setattr(host, "write_transaction", persist)
  monkeypatch.setattr(host, "rollback", lambda *a, **k: pytest.fail("verified service must not roll back"))
  assert host.run() == int(outcome_write_fails)
  assert replaced == [1]
  if outcome_write_fails:
    assert statuses[-1]["state"] == "needs_recovery"
    assert statuses[-1]["code"] == "outcome_record_failed"
    assert host.TRANSACTION.exists()
  else:
    assert statuses[-1]["state"] == "succeeded"
    assert statuses[-1]["code"] == "handoff_finalize_unconfirmed"
    assert "finalization is unconfirmed" in statuses[-1]["message"]


@pytest.mark.parametrize(
  ("rearmed", "finalized", "expected_code", "message_fragment"),
  [
    (False, False, "handoff_finalize_unconfirmed", "manual Resume may be needed"),
    (True, False, "handoff_finalize_unconfirmed", "finalization is unconfirmed"),
  ],
)
def test_healthy_rollback_reports_degraded_chat_handoff(
  tmp_path, monkeypatch, rearmed, finalized, expected_code, message_fragment,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  operation = "a" * 32
  expected = "b" * 40
  host.write_transaction(host.transaction_record(operation, expected, None, "sha256:old", "sha256:new"))
  snapshots = iter([("new", "sha256:new", "dead"), ("old", "sha256:old", "healthy")])
  monkeypatch.setattr(host, "container_health", lambda *_a: next(snapshots))
  monkeypatch.setattr(host, "docker_command", lambda *_a, **_k: None)
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)

  def ledger(_config, _cid, command, _operation, **_kwargs):
    assert _operation == operation
    return rearmed if command == "rearm-cutover" else finalized

  monkeypatch.setattr(host, "restart_ledger", ledger)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.rollback(
    config, operation, expected, "health_check_failed", "new image unhealthy",
  ) == 1
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == expected_code
  assert message_fragment in statuses[-1]["message"]


def test_reconcile_marks_interrupted_active_worker_failed(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  host.STATUS.write_text('{"state":"verifying"}', encoding="utf-8")
  abandoned = inbox.parent / f'.request-{"a" * 32}.json'
  abandoned.write_text("{}", encoding="utf-8")
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written[-1]["code"] == "worker_interrupted"
  assert not abandoned.exists()


@pytest.mark.parametrize("state", ["verifying", "needs_recovery", "succeeded"])
def test_reconcile_lock_loser_leaves_owner_files_untouched(tmp_path, monkeypatch, state):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  host.write_status(config, operation_id="1" * 32, request_nonce="2" * 32,
                    expected_sha="a" * 40, state=state, code="owner-status")
  host.write_transaction(host.transaction_record(
    "1" * 32, "a" * 40, "2" * 32, "sha256:previous", "sha256:target",
  ))
  request = inbox / "request.json"
  request.write_text(json.dumps({"version": 2, "expected_sha": "b" * 40, "nonce": "3" * 32}))
  claimed = config["control_dir"] / f'.request-{"1" * 32}.json'
  claimed.write_bytes(request.read_bytes())
  paths = (host.STATUS, config["control_dir"] / "status.json", host.TRANSACTION, request, claimed)

  def snapshot():
    return [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths]

  before = snapshot()
  monkeypatch.setattr(host, "read_json", lambda *_a: pytest.fail("lock loser read owner state"))
  monkeypatch.setattr(host, "docker_command", lambda *_a, **_k: pytest.fail("lock loser ran Docker"))
  with host.LOCK.open("a+") as owner:
    host.fcntl.flock(owner, host.fcntl.LOCK_EX | host.fcntl.LOCK_NB)
    assert host.reconcile() == 0
    with host.LOCK.open("a+") as contender:
      with pytest.raises(BlockingIOError):
        host.acquire_lock(contender, timeout=0)
  assert snapshot() == before


@pytest.mark.parametrize("state", ["succeeded", "rolled_back"])
@pytest.mark.parametrize("finalized", [False, True])
def test_reconcile_reads_status_after_other_reconciler_settles(
  tmp_path, monkeypatch, state, finalized,
):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  host.write_status(config, operation_id=transaction["operation_id"], state="verifying",
                    expected_sha=transaction["expected_sha"], request_nonce=transaction["request_nonce"])
  image = transaction["target_image"] if state == "succeeded" else transaction["previous_image"]
  monkeypatch.setattr(host, "container_health", lambda _c: ("cid", image, "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *_a: True)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_a: None)
  monkeypatch.setattr(host, "retain_images", lambda *_a: None)
  monkeypatch.setattr(host, "adopt_from_image", lambda *_a: "not adopted")
  monkeypatch.setattr(host, "compose", lambda *_a, **_k: pytest.fail("must not recreate"))

  def docker(args, **kwargs):
    assert args[:3] == ["docker", "ps", "-aq"]
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  calls = []

  def finalize(_config, _cid, command, operation, **kwargs):
    assert command == "finalize-cutover" and operation == transaction["operation_id"]
    calls.append(command)
    return finalized and ledger.finalize_cutover(operation, now=now + 4)

  monkeypatch.setattr(host, "docker_command", docker)
  monkeypatch.setattr(host, "restart_ledger", finalize)
  at_lock, settled = threading.Event(), threading.Event()
  local = threading.local()
  flock = host.fcntl.flock

  def pause_before_lock(fd, operation):
    if getattr(local, "delayed", False):
      at_lock.set()
      assert settled.wait(10), "other reconciler did not settle"
    return flock(fd, operation)

  def delayed_reconcile():
    local.delayed = True
    return host.reconcile()

  monkeypatch.setattr(host.fcntl, "flock", pause_before_lock)
  # Both reconcilers use real kernel locks, journal settlement, both status
  # publications and journal removal. Only scheduling before flock is gated.
  with ThreadPoolExecutor(max_workers=2) as pool:
    delayed = pool.submit(delayed_reconcile)
    try:
      assert at_lock.wait(10)
      assert pool.submit(host.reconcile).result(timeout=10) == 0
      assert not host.TRANSACTION.exists()
      owner_status = host.read_json(host.STATUS)
      assert owner_status["state"] == state
      expected_code = (None if state == "succeeded" else "worker_interrupted") if finalized else "handoff_finalize_unconfirmed"
      assert owner_status["code"] == expected_code
      assert host.read_json(config["control_dir"] / "status.json") == owner_status
    finally:
      settled.set()
    assert delayed.result(timeout=10) == 0

  # Capability refresh may advance the timestamp, not overwrite the outcome.
  for path in (host.STATUS, config["control_dir"] / "status.json"):
    actual = host.read_json(path)
    assert {k: v for k, v in actual.items() if k != "updated_at"} == {
      k: v for k, v in owner_status.items() if k != "updated_at"
    }
  assert not host.TRANSACTION.exists()
  assert calls == ["finalize-cutover"]
  assert ledger.CUTOVER_RECEIPT_PATH.exists() is not finalized
  assert not ledger.ACCEPTED_PATH.exists()


def test_reconcile_cleans_claim_abandoned_before_first_status(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  abandoned = inbox.parent / f'.request-{"b" * 32}.json'
  abandoned.write_text("{}", encoding="utf-8")
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written == [{}]
  assert not abandoned.exists()


def test_reconcile_refreshes_an_idle_controller_capability_receipt(
  tmp_path, monkeypatch,
):
  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  host.STATUS.write_text(
    '{"state":"idle","handoff":"external-cutover-v1"}', encoding="utf-8",
  )
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written == [{}]


def test_drain_requires_root_open_prepare_accept_order(tmp_path, monkeypatch):
  data = tmp_path / "data"
  data.mkdir()
  operation = "a" * 32
  order = []

  def ledger(_config, _cid, command, value, **_kwargs):
    order.append(command)
    assert value == operation
    return True

  def execute(args, **_kwargs):
    order.append("prepare")
    assert args[-1] == operation
    return subprocess.CompletedProcess([], 0)

  monkeypatch.setattr(host, "restart_ledger", ledger)
  monkeypatch.setattr(host, "docker_command", execute)

  result = host.request_drain(
    {"data_dir": data, "control_dir": data / "mobius-rebuild"},
    operation,
    "container",
  )

  assert result is None
  assert order == ["open-cutover", "prepare", "accept-cutover"]


def test_docker_observations_and_drain_use_finite_deadlines(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  calls = []

  def command(args, **kwargs):
    calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 0, stdout="sha256:image\n", stderr="")

  monkeypatch.setattr(host, "docker_command", command)
  monkeypatch.setattr(host, "compose", lambda *_a, **_k:
                      subprocess.CompletedProcess([], 0, stdout="cid\n", stderr=""))
  monkeypatch.setattr(host, "restart_ledger", lambda *_a, **_k: True)
  assert host.inspect_image("image", "{{.Id}}") == "sha256:image"
  assert host.app_container(config) == ("cid", "sha256:image")
  assert str(host._docker_root()) == "sha256:image"
  host.request_drain(config, "a" * 32, "cid")
  host.discard_pulled_image(f"{host.IMAGE}:sha-{'b' * 40}")
  assert [args[1:3] for args, _ in calls] == [
    ["image", "inspect"], ["container", "inspect"], ["info", "--format"],
    ["exec", "cid"], ["image", "rm"],
  ]
  assert [kwargs["timeout"] for _, kwargs in calls] == [30, 30, 30, 90, 30]


def test_drain_timeout_does_not_accept_an_unfinished_handoff(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  ledger = []
  monkeypatch.setattr(host, "restart_ledger", lambda _c, _cid, command, _operation:
                      ledger.append(command) or True)
  monkeypatch.setattr(host, "docker_command", lambda args, **kwargs:
                      (_ for _ in ()).throw(subprocess.TimeoutExpired(args, kwargs["timeout"])))
  with pytest.raises(subprocess.TimeoutExpired):
    host.request_drain(config, "a" * 32, "cid")
  assert ledger == ["open-cutover"]


def test_uncertain_image_removal_keeps_its_recorded_reference(tmp_path, monkeypatch):
  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  target = f"{host.IMAGE}:sha-{'b' * 40}"
  host.record_pulled_image(target)
  monkeypatch.setattr(host, "docker_command", lambda args, **kwargs:
                      (_ for _ in ()).throw(subprocess.TimeoutExpired(args, kwargs["timeout"])))
  with pytest.raises(subprocess.TimeoutExpired):
    host.discard_pulled_image(target)
  assert host.read_json(host.IMAGES)["sha_refs"] == [target]


@pytest.mark.parametrize("stage", ["pull", "drain", "target_tag", "post_inspect"])
def test_run_timeout_never_guesses_the_replacement_outcome(
  tmp_path, monkeypatch, stage,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "f" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  observations = []
  compose_calls = []
  deadlines = []

  def app_container(_config):
    observations.append("inspect")
    if stage == "post_inspect" and len(observations) == 2:
      raise subprocess.TimeoutExpired("docker container inspect", 30)
    return "cid", "sha256:old" if len(observations) == 1 else "sha256:new"

  def command(args, **kwargs):
    deadlines.append((args[1:3], kwargs["timeout"]))
    if ((stage == "pull" and args[1] == "pull") or
        (stage == "target_tag" and args[1:3] == ["tag", "sha256:new"])):
      raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  def drain(*_args):
    if stage == "drain":
      raise subprocess.TimeoutExpired("docker exec prepare", 90)

  monkeypatch.setattr(host, "app_container", app_container)
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "docker_command", command)
  monkeypatch.setattr(host, "request_drain", drain)
  monkeypatch.setattr(host, "compose", lambda *_a, **_k: compose_calls.append(1))
  monkeypatch.setattr(host, "wait_healthy", lambda *_a: True)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else host.IMAGE_SOURCE if "source" in template
    else "amd64" if "Architecture" in template else "sha256:new"
  ))

  assert host.run() == 1
  status = host.read_json(host.STATUS)
  assert status["code"] == "observation_timed_out"
  assert status["state"] == ("failed" if stage == "pull" else "needs_recovery")
  transaction = host.read_transaction()
  assert (transaction is None) == (stage == "pull")
  if transaction:
    assert transaction["previous_image"] == "sha256:old"
    assert transaction.get("phase") == (None if stage == "drain" else "replacement_started")
  assert compose_calls == ([1] if stage == "post_inspect" else [])
  assert (["pull", f"{host.IMAGE}:sha-{expected}"], 3600) in deadlines
  if stage != "pull":
    assert (["tag", "sha256:old"], 30) in deadlines
  if stage in {"target_tag", "post_inspect"}:
    assert (["tag", "sha256:new"], 30) in deadlines


@pytest.mark.parametrize("scenario", [
  "curl_nonzero", "invalid_json", "invalid_mount_shape", "invalid_mount_item",
  "curl_oserror", "mount_timeout", "mount_calledprocess",
  "wrong_revision", "protected_runtime",
])
def test_run_preserves_unknown_provenance_but_rolls_back_proven_rejection(
  tmp_path, monkeypatch, scenario,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "a" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  containers = iter([("old", "sha256:old"), ("new", "sha256:new")])
  monkeypatch.setattr(host, "app_container", lambda _c: next(containers))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else host.IMAGE_SOURCE if "source" in template
    else "amd64" if "Architecture" in template else "sha256:new"
  ))
  docker_calls = []
  monkeypatch.setattr(host, "docker_command", _provenance_command(scenario, docker_calls))
  monkeypatch.setattr(host, "request_drain", lambda *_a: None)
  compose_images = []
  monkeypatch.setattr(host, "compose", lambda *_a, image=None, **_k:
                      compose_images.append(image))
  monkeypatch.setattr(host, "wait_healthy", lambda *_a, **_k: True)
  health = iter([("new", "sha256:new", "healthy"),
                 ("old", "sha256:old", "healthy")])
  monkeypatch.setattr(host, "container_health", lambda _c: next(health))
  monkeypatch.setattr(host, "restart_ledger", lambda *_a, **_k: True)

  assert host.run() == 1
  status = host.read_json(host.STATUS)
  if scenario in {"wrong_revision", "protected_runtime"}:
    assert status["state"] == "rolled_back"
    assert status["code"] == "replacement_failed"
    assert host.read_transaction() is None
    assert compose_images == [host.TARGET_TAG, "sha256:old"]
  else:
    assert status["state"] == "needs_recovery"
    assert status["code"] == "observation_unconfirmed"
    assert host.read_transaction()["phase"] == "replacement_started"
    assert compose_images == [host.TARGET_TAG]
  assert "private" not in json.dumps(status)


@pytest.mark.parametrize("scenario, code", [
  ("curl_nonzero", "observation_unconfirmed"),
  ("wrong_revision", "provenance_failed"),
])
def test_no_change_provenance_failure_never_drains_or_rolls_back(
  tmp_path, monkeypatch, scenario, code,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "a" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _c: ("same", "sha256:new"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else host.IMAGE_SOURCE if "source" in template
    else "amd64" if "Architecture" in template else "sha256:new"
  ))
  monkeypatch.setattr(host, "docker_command", _provenance_command(scenario, []))
  monkeypatch.setattr(host, "request_drain", lambda *_a: pytest.fail("no-change drained"))
  assert host.run() == 1
  assert host.read_json(host.STATUS)["code"] == code
  assert host.read_json(host.STATUS)["state"] == "failed"
  assert "private" not in host.STATUS.read_text(encoding="utf-8")
  assert host.read_transaction() is None


def test_the_helper_accepts_both_request_versions_and_echoes_only_the_nonce():
  assert host.parse_request({"version": 1, "expected_sha": "a" * 40}) == ("a" * 40, None)
  assert host.parse_request({
    "version": 2, "expected_sha": "a" * 40, "nonce": "b" * 32,
  }) == ("a" * 40, "b" * 32)
  for invalid in (
    {"version": 1, "expected_sha": "a" * 40, "nonce": "b" * 32},
    {"version": 2, "expected_sha": "a" * 40},
    {"version": 2, "expected_sha": "a" * 40, "nonce": "not-a-nonce"},
    {"version": 3, "expected_sha": "a" * 40},
  ):
    with pytest.raises(ValueError):
      host.parse_request(invalid)


def test_the_helper_advertises_the_request_versions_it_accepts(tmp_path, monkeypatch):
  monkeypatch.setattr(host, "STATE_DIR", tmp_path / "state")
  monkeypatch.setattr(host, "STATUS", tmp_path / "state" / "status.json")
  control = tmp_path / "control"
  control.mkdir()

  status = host.write_status({"control_dir": control}, state="idle")

  assert status["request_versions"] == [1, 2]


def test_the_helper_names_a_request_in_its_status_before_claiming_it(
  tmp_path, monkeypatch,
):
  """The app treats a request gone from the inbox and absent from the status
  as never claimed, so the helper must publish its nonce first."""
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  nonce = "a" * 32
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "3" * 40, "nonce": nonce,
  }), encoding="utf-8")
  order = []
  real_replace = host.os.replace

  def record_replace(source, target):
    if Path(source) == request:
      order.append("claim")
    return real_replace(source, target)

  def record_status(_config, **fields):
    if fields.get("request_nonce") == nonce and fields.get("state") == "queued":
      order.append("named")
    return fields

  monkeypatch.setattr(host.os, "replace", record_replace)
  monkeypatch.setattr(host, "write_status", record_status)
  monkeypatch.setattr(
    host, "app_container",
    lambda _config: (_ for _ in ()).throw(RuntimeError("stop after the claim")),
  )

  host.run()

  assert order[:2] == ["named", "claim"]


def _requeue(request: Path, content: str) -> None:
  """What the app's withdraw-then-Finish does: a new file at the same path."""
  temp = request.with_name(".app-request.tmp")
  temp.write_text(content, encoding="utf-8")
  os.replace(temp, request)


def test_a_newer_request_that_replaced_the_one_read_stays_queued(
  tmp_path, monkeypatch,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  nonce = "b" * 32
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": nonce,
  }), encoding="utf-8")
  newer = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})
  statuses = []

  def status(_config, **fields):
    statuses.append(fields)
    if fields.get("state") == "queued":
      _requeue(request, newer)  # withdrawn and re-queued before the claim
    return fields

  monkeypatch.setattr(host, "write_status", status)

  assert host.run() == 1
  assert (statuses[-1]["state"], statuses[-1]["code"]) == ("failed", "withdrawn")
  assert statuses[-1]["request_nonce"] == nonce
  assert request.read_text(encoding="utf-8") == newer
  assert not list(_config["control_dir"].glob(".request-*"))


def test_a_request_queued_after_a_withdrawal_survives_the_failed_claim(
  tmp_path, monkeypatch,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": "b" * 32,
  }), encoding="utf-8")
  newer = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})

  def status(_config, **fields):
    if fields.get("state") == "queued":
      request.unlink()  # withdrawn before the claim
    elif fields.get("code") == "withdrawn":
      _requeue(request, newer)  # a newer Finish before the worker cleans up
    return fields

  monkeypatch.setattr(host, "write_status", status)

  assert host.run() == 1
  assert request.read_text(encoding="utf-8") == newer


def test_a_request_rewritten_in_place_is_returned_not_run(tmp_path, monkeypatch):
  """Same inode, different bytes: the file claimed is not the request read."""
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": "b" * 32,
  }), encoding="utf-8")
  rewritten = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})

  def status(_config, **fields):
    if fields.get("state") == "queued":
      with open(request, "w", encoding="utf-8") as handle:  # same inode
        handle.write(rewritten)
    return fields

  monkeypatch.setattr(host, "write_status", status)
  monkeypatch.setattr(
    host, "app_container",
    lambda _config: (_ for _ in ()).throw(AssertionError("must not run")),
  )

  assert host.run() == 1
  assert request.read_text(encoding="utf-8") == rewritten


def test_an_unverified_claim_that_cannot_be_returned_is_kept(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": "b" * 32,
  }), encoding="utf-8")
  newer = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})

  def status(_config, **fields):
    if fields.get("state") == "queued":
      _requeue(request, newer)
    return fields

  def no_link(*_args):
    raise PermissionError("link refused")

  monkeypatch.setattr(host, "write_status", status)
  monkeypatch.setattr(host.os, "link", no_link)

  assert host.run() == 1
  kept = list(config["control_dir"].glob(".unreturned-*"))
  assert [path.read_text(encoding="utf-8") for path in kept] == [newer]


@pytest.fixture
def interrupted_rollback(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  transaction = host.transaction_record(
    "1" * 32, "a" * 40, "2" * 32, "sha256:previous", "sha256:target",
  )
  transaction.update(phase="rollback_started", failure_code="checkout_failed",
                     failure_detail="target checkout failed", handoff_rearmed=True)
  host.write_transaction(transaction)
  calls = []
  monkeypatch.setattr(host, "compose", lambda *a, **k: calls.append(("compose", k)))
  monkeypatch.setattr(host, "docker_command", lambda *a, **k: calls.append(("docker", a)))
  monkeypatch.setattr(host, "restart_ledger", lambda _c, cid, command, operation, **k:
                      calls.append((command, operation, k)) or True)
  return config, transaction, calls


@pytest.mark.parametrize("health", ["healthy", "starting", "unhealthy", "running"])
def test_recovery_observes_exact_previous_boot_without_recreating_or_rearming(
  interrupted_rollback, monkeypatch, health,
):
  config, transaction, calls = interrupted_rollback
  observed = [health]
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous-container", transaction["previous_image"], observed[0]))

  def finish_boot(_config, timeout):
    assert timeout == host.ROLLBACK_HEALTH_SECONDS
    observed[0] = "healthy"
    return True

  monkeypatch.setattr(host, "wait_healthy", finish_boot)
  host.recover(config, transaction)
  assert calls == [("finalize-cutover", transaction["operation_id"],
                    {"image": transaction["previous_image"]})]
  status = host.read_json(host.STATUS)
  assert status["state"] == "rolled_back"
  assert status["code"] == "checkout_failed"
  assert "target checkout failed" in status["message"]
  assert status["request_nonce"] == transaction["request_nonce"]
  assert status["operation_id"] == transaction["operation_id"]
  assert host.read_transaction() is None


def test_rollback_timeout_preserves_receipt_and_original_error_then_can_settle(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous-container", transaction["previous_image"], "unhealthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: False)
  host.recover(config, transaction)
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction()["failure_detail"] == "target checkout failed"
  assert not calls
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous-container", transaction["previous_image"], "healthy"))
  host.recover(config, host.read_transaction())
  assert [call[0] for call in calls] == ["finalize-cutover"]
  assert host.read_json(host.STATUS)["code"] == "checkout_failed"


@pytest.mark.parametrize("health", ["dead", "exited", "missing"])
def test_failed_rollback_boot_cannot_authorize_another_boot(
  interrupted_rollback, monkeypatch, health,
):
  config, transaction, calls = interrupted_rollback
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("", "" if health == "missing" else transaction["previous_image"], health))
  host.recover(config, transaction)
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction()["phase"] == "rollback_started"


def test_first_rollback_is_journaled_before_rearm_and_uses_exact_image(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  snapshots = iter([
    ("target", transaction["target_image"], "dead"),
    ("previous", transaction["previous_image"], "healthy"),
  ])
  monkeypatch.setattr(host, "container_health", lambda _c: next(snapshots))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)

  def ledger(_config, cid, command, operation, **kwargs):
    assert operation == transaction["operation_id"]
    assert host.read_transaction()["phase"] == "rollback_started"
    calls.append((command, operation, kwargs))
    return True

  monkeypatch.setattr(host, "restart_ledger", ledger)
  host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "new", "new")
  assert [call[0] for call in calls] == ["docker", "rearm-cutover", "compose", "finalize-cutover"]
  assert calls[2][1]["image"] == transaction["previous_image"]


def test_failed_target_evidence_precedes_replacement_and_preserves_both_streams(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  target_cid = "a" * 64
  snapshots = iter([
    (target_cid, transaction["target_image"], "unhealthy"),
    ("previous", transaction["previous_image"], "healthy"),
  ])
  monkeypatch.setattr(host, "container_health", lambda _c: next(snapshots))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)

  def logs(cid):
    assert cid == target_cid
    calls.append(("logs", cid))
    return b"startup stdout", b"startup stderr", False, False

  monkeypatch.setattr(host, "_bounded_docker_logs", logs)
  host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "x", "y")
  assert [call[0] for call in calls[:4]] == ["logs", "docker", "rearm-cutover", "compose"]
  artifact = host.read_json(host.FAILED_TARGET_LOG)
  assert artifact["container_id"] == target_cid
  assert artifact["target_image"] == transaction["target_image"]
  assert base64.b64decode(artifact["stdout_b64"]) == b"startup stdout"
  assert base64.b64decode(artifact["stderr_b64"]) == b"startup stderr"
  assert host.FAILED_TARGET_LOG.stat().st_mode & 0o777 == 0o600
  assert host.read_json(host.STATUS)["evidence_capture"] == "saved"
  assert "startup stdout" not in json.dumps(host.read_json(host.STATUS))
  assert "startup stderr" not in json.dumps(host.read_json(config["control_dir"] / "status.json"))


@pytest.mark.parametrize("observed", ["sha256:previous", "sha256:alien", ""])
def test_non_target_container_never_captured(interrupted_rollback, monkeypatch, observed):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  monkeypatch.setattr(host, "container_health", lambda _c: ("b" * 64, observed, "unhealthy"))
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda _cid: pytest.fail("wrong target"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: False)
  host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "x", "y")
  assert not host.FAILED_TARGET_LOG.exists()


@pytest.mark.parametrize("capture_error", [PermissionError(), OSError("disk full"),
                                               subprocess.TimeoutExpired("docker logs", 0.1)])
def test_evidence_replay_does_not_overwrite_and_capture_error_does_not_block(
  interrupted_rollback, monkeypatch, capture_error,
):
  config, transaction, calls = interrupted_rollback
  cid = "c" * 64
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda _cid: (b"first", b"error", False, False))
  assert host.capture_failed_target(transaction["operation_id"], cid, transaction["target_image"]) == "saved"
  first = host.FAILED_TARGET_LOG.read_bytes()
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda _cid: pytest.fail("replayed logs"))
  assert host.capture_failed_target(transaction["operation_id"], cid, transaction["target_image"]) == "saved"
  assert host.FAILED_TARGET_LOG.read_bytes() == first
  transaction.pop("phase")
  host.write_transaction(transaction)
  monkeypatch.setattr(host, "container_health", lambda _c: ("d" * 64, transaction["target_image"], "dead"))
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda _cid: (_ for _ in ()).throw(capture_error))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: False)
  assert host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "x", "y") == 1
  assert [call[0] for call in calls[:3]] == ["docker", "rearm-cutover", "compose"]
  assert host.read_json(host.STATUS)["evidence_capture"] == "failed"
  assert host.FAILED_TARGET_LOG.read_bytes() == first


def test_docker_log_capture_has_byte_and_wall_clock_bounds(monkeypatch):
  import sys
  import time

  original = subprocess.Popen
  monkeypatch.setattr(host, "FAILED_TARGET_LOG_BYTES", 2048)
  monkeypatch.setattr(host, "FAILED_TARGET_LOG_SECONDS", 0.2)

  def popen(_args, **kwargs):
    return original([sys.executable, "-c",
                     "import sys,time; sys.stdout.write('x'*100000); "
                     "sys.stderr.write('e'*100000); sys.stdout.flush(); "
                     "sys.stderr.flush(); time.sleep(30)"], **kwargs)

  monkeypatch.setattr(host.subprocess, "Popen", popen)
  started = time.monotonic()
  out, err, truncated, timed_out = host._bounded_docker_logs("e" * 64)
  assert time.monotonic() - started < 3
  assert truncated and not timed_out and len(out) + len(err) <= 2048


def test_docker_log_timeout_kills_pipe_holding_descendant(tmp_path, monkeypatch):
  import sys
  import time

  original = subprocess.Popen
  marker = tmp_path / "orphan-finished"
  child = f"import time; time.sleep(0.7); open({str(marker)!r}, 'w').close()"
  parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
  monkeypatch.setattr(host, "FAILED_TARGET_LOG_SECONDS", 0.1)
  monkeypatch.setattr(host.subprocess, "Popen",
                      lambda _args, **kwargs: original([sys.executable, "-c", parent], **kwargs))
  started = time.monotonic()
  out, err, truncated, timed_out = host._bounded_docker_logs("e" * 64)
  assert (out, err) == (b"", b"")
  assert truncated and timed_out
  assert time.monotonic() - started < 2
  time.sleep(0.8)
  assert not marker.exists()


def test_docker_log_timeout_saves_partial_private_evidence(tmp_path, monkeypatch):
  import sys

  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  original = subprocess.Popen
  monkeypatch.setattr(host, "FAILED_TARGET_LOG_SECONDS", 0.3)
  monkeypatch.setattr(host, "FAILED_TARGET_LOG_BYTES", 16)
  monkeypatch.setattr(host.subprocess, "Popen", lambda _args, **kwargs:
                      original([sys.executable, "-c",
                                "import sys,time; sys.stdout.buffer.write(b'partial'); "
                                "sys.stdout.flush(); time.sleep(30)"], **kwargs))
  assert host.capture_failed_target("a" * 32, "b" * 64, "sha256:target") == "saved"
  evidence = host.read_json(host.FAILED_TARGET_LOG)
  assert base64.b64decode(evidence["stdout_b64"]) == b"partial"
  assert base64.b64decode(evidence["stderr_b64"]) == b""
  assert evidence["truncated"] is True
  assert evidence["timed_out"] is True
  assert host.FAILED_TARGET_LOG.stat().st_mode & 0o777 == 0o600


def test_rollback_refuses_another_operations_receipt(interrupted_rollback):
  config, transaction, calls = interrupted_rollback
  with pytest.raises(RuntimeError, match="does not own"):
    host.rollback(config, "3" * 32, transaction["expected_sha"], "x", "y")
  assert not calls
  assert host.read_transaction() == transaction


def test_consumed_boot_with_uncertain_finalization_settles_without_rearming(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous", transaction["previous_image"], "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **k: False)
  host.recover(config, transaction)
  assert host.read_json(host.STATUS)["code"] == "handoff_finalize_unconfirmed"
  assert host.read_json(host.STATUS)["state"] == "rolled_back"
  assert host.read_transaction() is None
  assert host.read_json(host.STATUS)["operation_id"] == transaction["operation_id"]
  assert not calls


def test_interrupted_settlement_replays_success_without_touching_container(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  clear = host.clear_transaction
  monkeypatch.setattr(host, "clear_transaction", lambda: (_ for _ in ()).throw(InterruptedError()))
  with pytest.raises(InterruptedError):
    host.settle_transaction(config, transaction, state="succeeded", code=None, message="done")
  monkeypatch.setattr(host, "clear_transaction", clear)
  host.recover(config, host.read_transaction())
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert host.read_transaction() is None
  assert not calls


def test_health_wait_does_not_treat_startup_unhealthy_as_terminal(monkeypatch):
  snapshots = iter(["starting", "unhealthy", "unhealthy", "healthy"])
  monkeypatch.setattr(host, "container_health", lambda *a, **k: ("cid", "image", next(snapshots)))
  monkeypatch.setattr(host.time, "sleep", lambda _seconds: None)
  assert host.wait_healthy({}, timeout=1)


def test_every_health_query_is_bounded_by_remaining_observation(monkeypatch):
  calls = []

  def command(args, **kwargs):
    calls.append(kwargs["timeout"])
    return subprocess.CompletedProcess(args, 0, stdout="image running healthy", stderr="")

  monkeypatch.setattr(host, "docker_command", command)
  assert host.wait_healthy({"project": "test"}, timeout=1)
  assert len(calls) == 2 and all(0 < value <= 0.5 for value in calls)


def test_docker_timeout_kills_descendants_without_waiting_for_inherited_pipes(tmp_path):
  import sys
  import time

  marker = tmp_path / "descendant-finished"
  child = f"import time; time.sleep(1); open({str(marker)!r}, 'w').close()"
  parent = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
  )
  started = time.monotonic()
  with pytest.raises(subprocess.TimeoutExpired):
    host.docker_command([sys.executable, "-c", parent], timeout=0.2)
  assert time.monotonic() - started < 3
  time.sleep(1.1)
  assert not marker.exists()


def test_generated_units_allow_the_bounded_recovery_budget(tmp_path):
  import configparser
  from app import platform_activation

  source = INSTALLER.read_text()
  start = source.index("cat >/etc/systemd/system/mobius-rebuild.service")
  end = source.index("chmod 0644 /etc/systemd/system/mobius-rebuild.service", start)
  # Execute the installer's actual unit heredocs, redirecting only their
  # destination. No root installation, Docker, or systemctl is invoked.
  units = source[start:end].replace("/etc/systemd/system/", f"{tmp_path}/")
  subprocess.run(["bash", "-eu", "-c", units], check=True,
                 env={**os.environ, "DATA_SOURCE": str(tmp_path / "data")})
  main = configparser.ConfigParser()
  main.read(tmp_path / "mobius-rebuild.service")
  reconcile = configparser.ConfigParser()
  reconcile.read(tmp_path / "mobius-rebuild-reconcile.service")
  assert main["Service"]["ExecStopPost"] == "/usr/local/libexec/mobius-rebuild-host reconcile"
  assert reconcile["Service"]["ExecStart"] == "/usr/local/libexec/mobius-rebuild-host reconcile"
  assert main["Service"]["Type"] == "oneshot"
  assert "TimeoutStartSec" not in main["Service"]  # don't cap legitimate pulls
  assert main["Service"].getint("TimeoutStopSec") == 900
  assert reconcile["Service"].getint("TimeoutStartSec") == 900
  assert host.ROLLBACK_HEALTH_SECONDS == 300
  assert host.WORKER_REVISION > 2
  marker = SCRIPT.parents[1] / "deployment" / "self-hosted-helper.required"
  assert marker.read_text().strip() == "2"
  assert "# Helper protocol revision: 2 " in source
  impact = platform_activation.classify_activation(
    ["deployment/self-hosted-helper.required"], deployment="self_hosted",
  )
  assert impact["level"] == "host_maintenance"


def test_legacy_running_rollback_adopts_only_its_own_failure_status(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  for key in ("phase", "failure_code", "failure_detail"):
    transaction.pop(key)
  host.write_transaction(transaction)
  host.write_status(config, operation_id=transaction["operation_id"], state="needs_recovery",
                    code="old_failure", message="original rollback timed out")
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous", transaction["previous_image"], "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  host.recover(config, transaction)
  assert [call[0] for call in calls] == ["finalize-cutover"]
  assert host.read_json(host.STATUS)["code"] == "old_failure"
  assert "original rollback timed out" in host.read_json(host.STATUS)["message"]


def test_missing_previous_image_never_rearms_or_launches(interrupted_rollback, monkeypatch):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  monkeypatch.setattr(host, "container_health", lambda _c: ("target", transaction["target_image"], "dead"))
  monkeypatch.setattr(host, "docker_command", lambda *a, **k:
                      (_ for _ in ()).throw(subprocess.CalledProcessError(1, "docker tag")))
  host.recover(config, transaction)
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction() is not None


def test_wrong_image_after_rollback_never_finalizes_receipt(interrupted_rollback, monkeypatch):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  snapshots = iter([
    ("target", transaction["target_image"], "dead"),
    ("unexpected", "sha256:unrelated", "healthy"),
  ])
  monkeypatch.setattr(host, "container_health", lambda _c: next(snapshots))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "x", "y")
  assert "finalize-cutover" not in [call[0] for call in calls]
  assert host.read_json(host.STATUS)["code"] == "rollback_wrong_image"
  assert host.read_transaction() is not None


def test_settlement_io_failure_cannot_roll_back_a_completed_success(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction["outcome"] = {
    "state": "succeeded", "code": None, "operation_id": transaction["operation_id"],
    "expected_sha": transaction["expected_sha"], "request_nonce": transaction["request_nonce"],
  }
  host.write_transaction(transaction)
  assert host.rollback(config, transaction["operation_id"], transaction["expected_sha"],
                       "replacement_failed", "status write failed") == 0
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert host.read_transaction() is None


def test_interrupted_target_boot_gets_observed_before_rollback(interrupted_rollback, monkeypatch):
  config, transaction, calls = interrupted_rollback
  transaction["phase"] = "replacement_started"
  host.write_transaction(transaction)
  health = ["unhealthy"]
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("target", transaction["target_image"], health[0]))

  def finish_target(_config):
    health[0] = "healthy"
    return True

  monkeypatch.setattr(host, "wait_healthy", finish_target)
  adopted = []
  monkeypatch.setattr(host, "adopt_from_image", lambda image: adopted.append(image) or "adopted")
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_a: None)
  host.recover(config, transaction)
  assert [call[0] for call in calls] == ["finalize-cutover"]
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert host.read_transaction() is None
  assert adopted == [transaction["target_image"]]
  assert host.read_json(host.STATUS)["worker_adoption"] == "adopted"


@pytest.mark.parametrize("scenario", ["wrong_revision", "protected_runtime"])
def test_recovery_rolls_back_only_proven_served_rejection(
  interrupted_rollback, monkeypatch, scenario,
):
  config, transaction, calls = interrupted_rollback
  transaction["phase"] = "replacement_started"
  transaction.pop("failure_code")
  transaction.pop("failure_detail")
  host.write_transaction(transaction)
  observations = iter([
    ("target", transaction["target_image"], "healthy"),
    ("target", transaction["target_image"], "healthy"),
    ("previous", transaction["previous_image"], "healthy"),
  ])
  monkeypatch.setattr(host, "container_health", lambda _c: next(observations))
  monkeypatch.setattr(host, "wait_healthy", lambda *_a: True)
  docker_calls = []
  monkeypatch.setattr(host, "docker_command", _provenance_command(scenario, docker_calls))
  host.recover(config, transaction)
  assert [call[0] for call in calls] == ["rearm-cutover", "compose", "finalize-cutover"]
  assert calls[1][1]["image"] == transaction["previous_image"]
  assert docker_calls[-1] == "other_docker"  # exact previous image tag
  assert host.read_json(host.STATUS)["state"] == "rolled_back"
  assert host.read_json(host.STATUS)["code"] == "replacement_failed"
  assert host.read_transaction() is None


@pytest.mark.parametrize("scenario", [
  "curl_nonzero", "invalid_json", "invalid_mount_shape", "invalid_mount_item",
  "curl_oserror", "mount_timeout", "mount_calledprocess",
])
def test_recovery_does_not_rollback_unconfirmed_provenance(
  interrupted_rollback, monkeypatch, scenario,
):
  config, transaction, calls = interrupted_rollback
  transaction["phase"] = "replacement_started"
  host.write_transaction(transaction)
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("target", transaction["target_image"], "healthy"))
  docker_calls = []
  monkeypatch.setattr(host, "docker_command", _provenance_command(scenario, docker_calls))
  host.recover(config, transaction)
  assert not calls
  assert "other_docker" not in docker_calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_json(host.STATUS)["code"] == "observation_unconfirmed"
  assert "private" not in host.STATUS.read_text(encoding="utf-8")
  assert host.read_transaction()["phase"] == "replacement_started"


def test_transient_health_query_failure_does_not_undo_boot(monkeypatch):
  probes = iter([subprocess.CalledProcessError(1, "docker inspect"), "healthy"])

  def observe(*args, **kwargs):
    result = next(probes)
    if isinstance(result, Exception):
      raise result
    return "cid", "image", result

  monkeypatch.setattr(host, "container_health", observe)
  monkeypatch.setattr(host.time, "sleep", lambda _delay: None)
  assert host.wait_healthy({}, timeout=1)


def test_unknown_target_health_at_deadline_preserves_boot_and_receipt(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction["phase"] = "replacement_started"
  host.write_transaction(transaction)
  clock = [0.0]
  probes = [0]
  monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(host.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

  def observe(*args, **kwargs):
    probes[0] += 1
    if probes[0] == 1:
      return "target", transaction["target_image"], "starting"
    raise subprocess.CalledProcessError(1, "docker inspect")

  monkeypatch.setattr(host, "container_health", observe)
  host.recover(config, transaction)
  assert clock[0] == 180
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction()["phase"] == "replacement_started"


@pytest.mark.parametrize("path", ["run", "recover"])
@pytest.mark.parametrize("target_health", ["starting", "running"])
@pytest.mark.parametrize("unknown_observation", [False, True])
def test_readiness_budget_preserves_single_rollback_policy(
  tmp_path, monkeypatch, path, target_health, unknown_observation,
):
  # Exercise run/recover -> wait_healthy -> container_health and the real
  # rollback/journal/ledger-command/settlement paths. The fixture supplies a
  # consumed-boot proof; Docker transport, logs, disk preflight and time are fake.
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected, nonce = "a" * 40, "2" * 32
  previous, target = "sha256:previous", "sha256:target"
  clock = [0.0]
  current = [previous if path == "run" else target]
  previous_health = ["starting"]
  target_probes, boots, ledger_calls, deadlines = [], [], [], []
  monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(host.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda _cid: (b"", b"", False, False))

  def command(args, **kwargs):
    output = ""
    if args[1] == "compose":
      if "ps" in args:
        output = "b" * 64 if current[0] == previous else "c" * 64
        if "-a" in args:
          deadlines.append((clock[0], kwargs["timeout"]))
      elif "up" in args:
        image = kwargs["env"]["MOBIUS_IMAGE"]
        journal = host.read_transaction()
        assert journal["phase"] == ("replacement_started" if image == host.TARGET_TAG else "rollback_started")
        assert image in {host.TARGET_TAG, previous}
        boots.append((clock[0], image, journal["operation_id"]))
        current[0] = target if image == host.TARGET_TAG else previous
      else:
        pytest.fail(f"unexpected Compose command: {args}")
    elif args[1:3] == ["image", "inspect"]:
      template = args[-2]
      output = (expected if "revision" in template else
                host.IMAGE_SOURCE if "source" in template else
                "amd64" if "Architecture" in template else target)
    elif args[1:3] == ["container", "inspect"]:
      if args[-2] == "{{.Image}}":
        output = current[0]
      else:
        health = previous_health[0]
        if current[0] == target:
          target_probes.append(clock[0])
          if unknown_observation and len(target_probes) > 1:
            raise subprocess.CalledProcessError(1, args)
          health = target_health
        # "running" has no Docker healthcheck, "starting" has one.
        output = f"{current[0]} running" + (f" {health}" if health != "running" else "")
    elif args[1] == "exec":
      action, operation = args[-2:]
      assert operation == host.read_transaction()["operation_id"]
      if action == "rearm-cutover":
        assert host.read_transaction()["phase"] == "rollback_started"
      ledger_calls.append((action, operation))
    elif args[1] not in {"pull", "tag", "image", "ps"}:
      pytest.fail(f"unexpected Docker command: {args}")
    return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

  monkeypatch.setattr(host, "docker_command", command)
  if path == "run":
    (inbox / "request.json").write_text(json.dumps({
      "version": 2, "expected_sha": expected, "nonce": nonce,
    }))
    assert host.run() == 1
    assert not (inbox / "request.json").exists()
  else:
    transaction = host.transaction_record("1" * 32, expected, nonce, previous, target)
    transaction["phase"] = "replacement_started"
    host.write_transaction(transaction)
    host.recover(config, transaction)

  journal = host.read_transaction()
  operation = journal["operation_id"]
  status = host.read_json(host.STATUS)
  assert status["state"] == "needs_recovery"
  assert status["request_nonce"] == nonce and status["expected_sha"] == expected
  assert all(0 < timeout <= 10 for _at, timeout in deadlines)
  # The final pair of Docker queries must fit the remaining readiness budget.
  assert (177.0, 1.5) in deadlines
  wait_probes = target_probes[:60] if path == "run" else target_probes[1:61]
  assert wait_probes == [float(t) for t in range(0, 180, 3)]
  rollback_boots = [boot for boot in boots if boot[1] == previous]
  if unknown_observation:
    assert clock[0] == 180
    assert journal["phase"] == "replacement_started"
    assert not rollback_boots
    assert not any(action in {"rearm-cutover", "finalize-cutover"} for action, _op in ledger_calls)
    before = list(boots), list(ledger_calls)
    assert host.reconcile() == 0
    assert host.reconcile() == 0
    assert (boots, ledger_calls) == before
    assert host.read_json(host.STATUS)["state"] == "needs_recovery"
    assert host.read_transaction()["phase"] == "replacement_started"
    return

  assert clock[0] == 180 + host.ROLLBACK_HEALTH_SECONDS
  assert rollback_boots == [(180.0, previous, operation)]
  assert journal["phase"] == "rollback_started"
  assert journal["failure_code"] == "readiness_budget_exhausted"
  subject = "the new container" if path == "run" else "the interrupted replacement"
  detail = f"{subject} was not serviceable within the readiness budget"
  assert journal["failure_detail"] == detail
  assert [(action, op) for action, op in ledger_calls if action == "rearm-cutover"] == [("rearm-cutover", operation)]
  # Reconciliation may observe the same slow rollback, but cannot boot it again.
  before = list(boots), list(ledger_calls)
  assert host.reconcile() == 0
  assert clock[0] == 180 + 2 * host.ROLLBACK_HEALTH_SECONDS
  assert (boots, ledger_calls) == before
  assert host.read_transaction()["failure_code"] == "readiness_budget_exhausted"
  previous_health[0] = "healthy"
  assert host.reconcile() == 0
  status = host.read_json(host.STATUS)
  assert status["state"] == "rolled_back"
  assert status["code"] == status["failure_code"] == "readiness_budget_exhausted"
  assert status["failure_detail"] == detail
  assert status["message"] == f"The previous container was restored: {detail}"
  assert status["operation_id"] == operation and status["request_nonce"] == nonce
  assert host.read_json(config["control_dir"] / "status.json") == status
  assert host.read_transaction() is None
  assert ledger_calls[-1] == ("finalize-cutover", operation)
  before = list(boots), list(ledger_calls)
  assert host.reconcile() == 0
  assert host.reconcile() == 0
  assert (boots, ledger_calls) == before


def test_installer_stops_before_publishing_units_when_seeding_refuses(tmp_path):
  source = INSTALLER.read_text()
  start = source.index("MOBIUS_REBUILD_LOCK_HELD=1")
  end = source.index("install -D", start)
  python = tmp_path / "python"
  python.write_text("#!/bin/sh\n[ \"$MOBIUS_REBUILD_LOCK_HELD\" = 1 ] || exit 99\nexit 1\n")
  python.chmod(0o700)
  seed = source[start:end].replace("/usr/bin/python3", str(python))
  published = tmp_path / "units-published"
  result = subprocess.run(
    ["bash", "-eu", "-c", seed + '\n touch "$PUBLISHED"'],
    env={**os.environ, "ROOT": str(tmp_path), "PUBLISHED": str(published)},
    capture_output=True, text=True,
  )
  assert result.returncode == 1
  assert not published.exists()


@pytest.mark.parametrize("failure", [
  "open_response", "prepare_response", "accept_rejected",
  "accept_response", "accept_oserror", "accept_timeout",
  "replacing_status", "recovery_status",
])
def test_run_keeps_real_drain_authorization_owned_until_recovery(tmp_path, monkeypatch, failure):
  import time
  from tests.test_restart_ledger import _bind, _load_supervisor
  from app import restart_ledger as platform_ledger

  proof = host.cutover_boot_consumed
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda c, op:
                      proof(c, op, trusted_uid=os.getuid(), trusted_gid=os.getgid()))
  ledger = _load_supervisor()
  _bind(ledger, config["data_dir"], monkeypatch)
  now = time.time()
  source_boot = "source-boot-1234"
  assert not ledger.begin_boot(source_boot, now=now)
  expected, nonce = "a" * 40, "2" * 32
  request = inbox / "request.json"
  request.write_text(json.dumps({"version": 2, "expected_sha": expected, "nonce": nonce}))
  calls = []

  def command(args, **_kwargs):
    result = True
    if args[1] == "exec":
      operation = args[-1]
      action = args[-2]
      calls.append(action)
      if action == "open-cutover":
        assert ledger.open_cutover(operation, now=now + 1)
        result = failure != "open_response"
      elif action.endswith("prepare-container-cutover.py"):
        platform_ledger.publish_cutover_intent(
          boot_id=source_boot, nonce=nonce, cutover_id=operation, runs=[], now=now + 1,
        )
        result = failure != "prepare_response"
      elif action == "accept-cutover":
        if failure == "accept_rejected":
          result = False
        else:
          assert ledger.accept_cutover(operation, now=now + 2)
          if failure == "accept_oserror":
            raise OSError("accept response lost")
          if failure == "accept_timeout":
            raise subprocess.TimeoutExpired(args, 10)
          result = failure != "accept_response"
      else:
        pytest.fail(f"unexpected ledger action: {action}")
    return subprocess.CompletedProcess(args, 0 if result else 1, stdout="", stderr="")

  write_status = host.write_status

  def status(config_value, **fields):
    if failure in {"replacing_status", "recovery_status"} and (
      fields.get("state") == "replacing" or
      (failure == "recovery_status" and fields.get("state") == "needs_recovery")
    ):
      raise OSError("status publication failed")
    return write_status(config_value, **fields)

  monkeypatch.setattr(host, "docker_command", command)
  monkeypatch.setattr(host, "write_status", status)
  monkeypatch.setattr(host, "app_container", lambda _c: ("cid", "sha256:previous"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else host.IMAGE_SOURCE if "source" in template
    else "amd64" if "Architecture" in template else "sha256:target"
  ))
  monkeypatch.setattr(host, "compose", lambda *_a, **_k: pytest.fail("must not recreate the source"))

  # Real run -> request_drain -> restart_ledger; only Docker transport is fake.
  if failure == "recovery_status":
    with pytest.raises(OSError, match="status publication failed"):
      host.run()
  else:
    assert host.run() == 1
  transaction = host.read_transaction()
  assert transaction is not None
  if failure != "recovery_status":
    assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert transaction["request_nonce"] == nonce
  assert transaction["expected_sha"] == expected
  assert transaction["previous_image"] == "sha256:previous"
  assert transaction["target_image"] == "sha256:target"
  assert "phase" not in transaction  # draining is not replacement or rollback
  assert "outcome" not in transaction
  assert host.read_json(host.IMAGES)["sha_refs"] == [f"{host.IMAGE}:sha-{expected}"]
  assert not request.exists()
  accepted = failure not in {"open_response", "prepare_response", "accept_rejected"}
  assert ledger.ACCEPTED_PATH.exists() == accepted
  authorization = ledger.ACCEPTED_PATH.read_bytes() if accepted else None
  if accepted:
    assert json.loads(authorization)["cutover_id"] == transaction["operation_id"]

  # The pending journal prevents a different request from taking ownership.
  request.write_text(json.dumps({"version": 2, "expected_sha": "b" * 40, "nonce": "3" * 32}))
  pending_request = request.read_bytes()
  before = host.TRANSACTION.read_bytes(), host.STATUS.read_bytes(), list(calls)
  assert host.run() == 0
  assert (host.TRANSACTION.read_bytes(), host.STATUS.read_bytes(), calls) == before
  assert request.read_bytes() == pending_request

  # The still-healthy source is NOT evidence that acceptance was cancelled.
  # Real reconciliation keeps the exact authorization under manual recovery,
  # without finalizing it or arming another boot, including repeated attempts.
  monkeypatch.setattr(host, "write_status", write_status)
  monkeypatch.setattr(host, "container_health", lambda _c: ("cid", "sha256:previous", "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *_a, **_k: True)
  for _ in range(2):
    assert host.reconcile() == 0
    assert host.read_transaction()["operation_id"] == transaction["operation_id"]
    assert "outcome" not in host.read_transaction()
    assert host.read_json(host.STATUS)["code"] == "handoff_boot_unconfirmed"
    assert host.read_json(host.STATUS)["state"] == "needs_recovery"
    assert ledger.ACCEPTED_PATH.exists() == accepted
    if accepted:
      assert ledger.ACCEPTED_PATH.read_bytes() == authorization
    assert calls == before[2]
    assert request.read_bytes() == pending_request


def _real_cutover(tmp_path, monkeypatch, *, consume=True):
  import time
  from tests.test_restart_ledger import _bind, _load_supervisor
  from app import restart_ledger as platform_ledger

  proof = host.cutover_boot_consumed
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  ledger = _load_supervisor()
  ledger_root = tmp_path / "ledger-data"
  ledger_root.mkdir()
  config["data_dir"] = ledger_root
  _bind(ledger, ledger_root, monkeypatch)
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda c, op:
                      proof(c, op, trusted_uid=os.getuid(), trusted_gid=os.getgid()))
  now = time.time()
  operation, expected, nonce = "1" * 32, "a" * 40, "2" * 32
  source_boot, target_boot = "source-boot-1234", "target-boot-1234"
  ledger.begin_boot(source_boot, now=now)
  assert ledger.open_cutover(operation, now=now + 1)
  platform_ledger.publish_cutover_intent(
    boot_id=source_boot, nonce=nonce, cutover_id=operation, runs=[], now=now + 1,
  )
  assert ledger.accept_cutover(operation, now=now + 2)
  if consume:
    assert ledger.begin_boot(target_boot, now=now + 3)
    assert not ledger.ACCEPTED_PATH.exists()
  transaction = host.transaction_record(operation, expected, nonce, "sha256:previous", "sha256:target")
  host.write_transaction(transaction)
  return config, transaction, ledger, now


@pytest.mark.parametrize("state", ["succeeded", "rolled_back"])
@pytest.mark.parametrize("crash", ["before_finalize", "after_finalize", "after_clean_write"])
def test_verified_service_crash_replays_honest_outcome_with_real_receipt(
  tmp_path, monkeypatch, state, crash,
):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  operation, nonce = transaction["operation_id"], transaction["request_nonce"]
  code = "checkout_failed" if state == "rolled_back" else None
  if code:
    transaction.update(failure_code=code, failure_detail="target checkout failed")
  host.write_transaction(transaction)
  writes = [0]
  write = host.write_transaction

  def persist(value):
    if not value.get("outcome"):
      write(value)
      return
    writes[0] += 1
    if writes[0] == 2 and crash == "after_finalize":
      assert not ledger.CUTOVER_RECEIPT_PATH.exists()
      raise KeyboardInterrupt
    write(value)
    if writes[0] == 2 and crash == "after_clean_write":
      raise KeyboardInterrupt

  def finalize(_config, _cid, command, owning_operation, **kwargs):
    assert command == "finalize-cutover" and owning_operation == operation
    assert host.read_transaction()["outcome"]["code"] == "handoff_finalize_unconfirmed"
    if crash == "before_finalize":
      raise KeyboardInterrupt
    return ledger.finalize_cutover(operation, now=now + 4)

  monkeypatch.setattr(host, "write_transaction", persist)
  monkeypatch.setattr(host, "restart_ledger", finalize)
  image = transaction["previous_image"] if state == "rolled_back" else transaction["target_image"]
  monkeypatch.setattr(host, "container_health", lambda _c: ("cid", image, "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  monkeypatch.setattr(host, "verify_served_generation", lambda *a: None)
  with pytest.raises(KeyboardInterrupt):
    if state == "rolled_back":
      host.rollback(config, operation, transaction["expected_sha"], code, "target checkout failed")
    else:
      host.recover(config, transaction)
  assert ledger.CUTOVER_RECEIPT_PATH.exists() == (crash == "before_finalize")
  monkeypatch.setattr(host, "write_transaction", write)

  def forbidden(*args, **kwargs):
    pytest.fail("replay must not query, finalize, rearm, recreate or adopt")

  for name in ("container_health", "restart_ledger", "compose", "adopt_from_image", "cutover_boot_consumed"):
    monkeypatch.setattr(host, name, forbidden)
  host.recover(config, host.read_transaction())
  status = host.read_json(host.STATUS)
  assert status["state"] == state
  assert status["code"] == (code if crash == "after_clean_write" else "handoff_finalize_unconfirmed")
  assert status["operation_id"] == operation and status["request_nonce"] == nonce
  assert status["failure_code"] == code
  assert host.read_transaction() is None
  # A leftover receipt by itself cannot authorize a later boot.
  assert not ledger.begin_boot("unrelated-boot-1234", now=now + 5)


@pytest.mark.parametrize("failure", ["finalize", "cleanup", "adoption"])
def test_verified_success_side_effect_failure_never_becomes_rollback(
  interrupted_rollback, monkeypatch, failure,
):
  config, transaction, calls = interrupted_rollback

  def fail(*args, **kwargs):
    assert host.read_transaction()["outcome"]["state"] == "succeeded"
    raise RuntimeError("side effect failed")

  monkeypatch.setattr(host, "retain_images", fail if failure == "cleanup" else lambda *a: None)
  monkeypatch.setattr(host, "adopt_from_image", fail if failure == "adoption" else lambda *a: "adopted")
  if failure == "finalize":
    monkeypatch.setattr(host, "restart_ledger", fail)
  host.finish_verified(config, transaction, "cid", transaction["target_image"],
                       state="succeeded", code=None, message="Container rebuilt successfully.")
  status = host.read_json(host.STATUS)
  assert status["state"] == "succeeded"
  assert status["code"] == ("handoff_finalize_unconfirmed" if failure == "finalize" else None)
  assert host.read_transaction() is None
  assert not any(call[0] in {"compose", "rearm-cutover"} for call in calls)


@pytest.mark.parametrize("pending", ["same_boot", "rearmed"])
def test_pending_accepted_authorization_prevents_terminal_outcome(tmp_path, monkeypatch, pending):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch, consume=pending != "same_boot")
  if pending == "rearmed":
    assert ledger.rearm_cutover(transaction["operation_id"], now=now + 4)
  assert ledger.ACCEPTED_PATH.exists()
  before = ledger.ACCEPTED_PATH.read_bytes()
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **k: pytest.fail("must not finalize pending authorization"))
  assert not host.finish_verified(config, transaction, "cid", transaction["previous_image"],
                                  state="rolled_back", code="failed", message="Previous image healthy.")
  assert "outcome" not in host.read_transaction()
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_json(host.STATUS)["code"] == "handoff_boot_unconfirmed"
  assert ledger.ACCEPTED_PATH.read_bytes() == before


@pytest.mark.parametrize("invalid", [
  "operation", "nonce", "version", "action", "source", "target", "boot",
  "receipt_nonce", "receipt_operation", "receipt_null", "malformed", "missing_ack",
  "symlink_ack", "symlink_ledger", "writable_ack", "writable_ledger", "read_error",
  "accepted_stat_error", "accepted_symlink", "changed_boot", "wrong_owner",
  "oversized_ack", "fifo_ack", "receipt_symlink", "numeric_nonce", "numeric_source",
])
def test_consumed_boot_proof_refuses_uncertain_or_untrusted_evidence(
  tmp_path, monkeypatch, invalid,
):
  config, transaction, ledger, _now = _real_cutover(tmp_path, monkeypatch)
  ack = json.loads(ledger.ACK_PATH.read_text())
  updates = {
    "operation": ("cutover_id", "9" * 32), "nonce": ("nonce", "!"),
    "version": ("version", 2), "action": ("action", "restart"),
    "source": ("source_boot_id", "!"), "target": ("target_boot_id", "another-boot-1234"),
    "numeric_nonce": ("nonce", 12345678), "numeric_source": ("source_boot_id", 12345678),
  }
  if invalid in updates:
    key, value = updates[invalid]
    ack[key] = value
    ledger._write_json(ledger.ACK_PATH, ack, 0o444)
    if invalid == "numeric_nonce":
      ledger.CUTOVER_RECEIPT_PATH.unlink()  # type check must stand on its own
  elif invalid == "boot":
    ledger._atomic_write(ledger.BOOT_PATH, b"another-boot-1234", 0o444)
  elif invalid in {"receipt_nonce", "receipt_operation", "receipt_null"}:
    receipt = json.loads(ledger.CUTOVER_RECEIPT_PATH.read_text())
    if invalid == "receipt_null":
      receipt = None
    else:
      receipt["nonce" if invalid == "receipt_nonce" else "cutover_id"] = "different-token-1234"
    ledger._atomic_write(ledger.CUTOVER_RECEIPT_PATH, json.dumps(receipt).encode(), 0o600)
  elif invalid == "receipt_symlink":
    target = ledger.CUTOVER_RECEIPT_PATH.with_name("copied-receipt")
    ledger.CUTOVER_RECEIPT_PATH.rename(target)
    ledger.CUTOVER_RECEIPT_PATH.symlink_to(target)
  elif invalid == "oversized_ack":
    ledger._atomic_write(ledger.ACK_PATH, b" " * 65537, 0o444)
  elif invalid == "fifo_ack":
    ledger.ACK_PATH.unlink()
    os.mkfifo(ledger.ACK_PATH, 0o444)
  elif invalid == "malformed":
    ledger._atomic_write(ledger.ACK_PATH, b"{", 0o444)
  elif invalid == "missing_ack":
    ledger.ACK_PATH.unlink()
  elif invalid == "symlink_ack":
    target = ledger.ACK_PATH.with_name("copied-ack")
    ledger.ACK_PATH.rename(target)
    ledger.ACK_PATH.symlink_to(target)
  elif invalid == "symlink_ledger":
    target = ledger.LEDGER_DIR.with_name("copied-ledger")
    ledger.LEDGER_DIR.rename(target)
    ledger.LEDGER_DIR.symlink_to(target, target_is_directory=True)
  elif invalid in {"writable_ack", "writable_ledger"}:
    (ledger.ACK_PATH if invalid == "writable_ack" else ledger.LEDGER_DIR).chmod(0o777)
  elif invalid == "read_error":
    monkeypatch.setattr(host.os, "read", lambda *_a: (_ for _ in ()).throw(PermissionError()))
  elif invalid == "accepted_stat_error":
    stat = host.os.stat
    def deny(path, *args, **kwargs):
      if path == "accepted.json":
        raise PermissionError
      return stat(path, *args, **kwargs)
    monkeypatch.setattr(host.os, "stat", deny)
  elif invalid == "accepted_symlink":
    ledger.ACCEPTED_PATH.symlink_to(tmp_path / "missing")
  elif invalid == "changed_boot":
    read = host.os.read
    boot_reads = [0]
    def changing(fd, size):
      raw = read(fd, size)
      if raw.strip() == b"target-boot-1234":
        boot_reads[0] += 1
        if boot_reads[0] > 1:
          return b"changed-boot-1234"
      return raw
    monkeypatch.setattr(host.os, "read", changing)
  elif invalid == "wrong_owner":
    fstat = host.os.fstat
    def changed_owner(fd):
      fields = list(fstat(fd))
      fields[4] = os.getuid() + 1
      return os.stat_result(fields)
    monkeypatch.setattr(host.os, "fstat", changed_owner)
  assert not host.cutover_boot_consumed(config, transaction["operation_id"])
  assert "outcome" not in host.read_transaction()


def test_missing_retired_receipt_allows_only_degraded_settlement(tmp_path, monkeypatch):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  assert ledger.finalize_cutover(transaction["operation_id"], now=now + 4)
  monkeypatch.setattr(host, "restart_ledger", lambda _c, _cid, _cmd, op, **_k:
                      ledger.finalize_cutover(op, now=now + 5))
  assert host.finish_verified(config, transaction, "cid", transaction["previous_image"],
                              state="rolled_back", code="checkout_failed", message="Restored.")
  assert host.read_json(host.STATUS)["code"] == "handoff_finalize_unconfirmed"
  assert host.read_transaction() is None


def test_new_operation_does_not_inherit_previous_failure_detail(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  host.write_status(config, operation_id="1" * 32, failure_code="old", failure_detail="old error")
  host.write_status(config, operation_id="2" * 32, state="queued")
  status = host.read_json(host.STATUS)
  assert status["failure_code"] is None and status["failure_detail"] is None


def test_consumed_rollback_ack_may_have_different_source_than_original_receipt(tmp_path, monkeypatch):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  assert ledger.rearm_cutover(transaction["operation_id"], now=now + 4)
  assert ledger.begin_boot("rollback-boot-1234", now=now + 5)
  ack = json.loads(ledger.ACK_PATH.read_text())
  receipt = json.loads(ledger.CUTOVER_RECEIPT_PATH.read_text())
  assert ack["source_boot_id"] != receipt["source_boot_id"]
  assert host.cutover_boot_consumed(config, transaction["operation_id"])


def test_consumed_proof_rechecks_pending_authorization_after_snapshot(tmp_path, monkeypatch):
  config, transaction, ledger, _now = _real_cutover(tmp_path, monkeypatch)
  stat = host.os.stat
  checks = [0]

  def appearing(path, *args, **kwargs):
    if path == "accepted.json":
      checks[0] += 1
      if checks[0] == 2:
        return ledger.ACK_PATH.lstat()  # the second lookup now finds a file
    return stat(path, *args, **kwargs)

  monkeypatch.setattr(host.os, "stat", appearing)
  assert not host.cutover_boot_consumed(config, transaction["operation_id"])
  assert checks[0] == 2
