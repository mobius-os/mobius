"""Trust and failure-boundary tests for the installed host worker."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
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

  broad_chown = source.index("chown -R mobius:mobius /data")
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

  monkeypatch.setattr(host.subprocess, "run", execute)
  host.verify_served_generation("cid", "a" * 40)

  def mounted(args, **_kwargs):
    payload = (
      '{"sha":"' + "a" * 40 + '"}' if "curl" in args else
      '[{"Destination":"/app/runtime"}]'
    )
    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

  monkeypatch.setattr(host.subprocess, "run", mounted)
  with pytest.raises(RuntimeError, match="image's protected runtime"):
    host.verify_served_generation("cid", "a" * 40)


def test_replacement_verifies_provenance_before_retiring_chat_handoff():
  source = SCRIPT.read_text(encoding="utf-8")
  run = source[source.index("def run()") : source.index("def reconcile()")]
  healthy = run.index("if not wait_healthy(config_value)")
  provenance = run.index("verify_served_generation(", healthy)
  finalize = run.index('"finalize-cutover"', provenance)

  assert healthy < provenance < finalize


def _worker_paths(tmp_path: Path, monkeypatch):
  state = tmp_path / "state"
  inbox = tmp_path / "control" / "inbox"
  state.mkdir()
  inbox.mkdir(parents=True)
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "LOCK", state / "replace.lock")
  monkeypatch.setattr(host, "STATUS", state / "status.json")
  monkeypatch.setattr(host, "IMAGES", state / "images.json")
  monkeypatch.setattr(host, "TRANSACTION", state / "transaction.json")
  data = tmp_path / "data"
  data.mkdir()
  config = {
    "project": "mobius", "control_dir": inbox.parent, "data_dir": data,
  }
  monkeypatch.setattr(host, "config", lambda: config)
  # The forward floor check reads Docker; by default the target is compatible.
  monkeypatch.setattr(host, "rollback_image_level", lambda _image: 1)
  monkeypatch.setattr(host, "live_database_floor", lambda _cid: 0)
  return config, inbox


def test_compose_never_mounts_a_generated_runtime(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  calls = []

  def execute(args, **kwargs):
    calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  monkeypatch.setattr(host.subprocess, "run", execute)
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
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
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
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
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
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
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
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
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




def test_replacement_drains_then_rolls_back_after_cutover_error(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "d" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "old"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "new"
  ))
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
  monkeypatch.setattr(host, "rollback_preflight", lambda *_args: None)
  monkeypatch.setattr(host, "wait_ready", lambda *_args, **_kwargs: ("ready", {"ready": True}))
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 1
  assert order == [
    "drain",
    f"compose:{host.TARGET_TAG}",
    f"compose:{host.ROLLBACK_TAG}",
  ]
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == "replacement_failed"


def test_success_reports_when_chat_handoff_receipt_cannot_be_retired(
  tmp_path, monkeypatch,
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
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
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

  assert host.run() == 0
  assert statuses[-1]["state"] == "succeeded"
  assert statuses[-1]["code"] == "handoff_finalize_failed"
  assert "could not verify and retire" in statuses[-1]["message"]


@pytest.mark.parametrize(
  ("rearmed", "finalized", "expected_code", "message_fragment"),
  [
    (False, False, "handoff_rearm_failed", "may need manual Resume"),
    (True, False, "handoff_finalize_failed", "could not verify and retire"),
  ],
)
def test_healthy_rollback_reports_degraded_chat_handoff(
  tmp_path, monkeypatch, rearmed, finalized, expected_code, message_fragment,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  operation = "a" * 32
  expected = "b" * 40
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "old"))
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  monkeypatch.setattr(host, "rollback_preflight", lambda *_args: None)
  monkeypatch.setattr(host, "wait_ready", lambda *_args, **_kwargs: ("ready", {"ready": True}))

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
  monkeypatch.setattr(host.subprocess, "run", execute)

  result = host.request_drain(
    {"data_dir": data, "control_dir": data / "mobius-rebuild"},
    operation,
    "container",
  )

  assert result is None
  assert order == ["open-cutover", "prepare", "accept-cutover"]


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


def test_an_image_below_the_database_floor_is_refused_before_any_drain(
    tmp_path, monkeypatch,
):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "e" * 40
  (inbox / "request.json").write_text(
    f'{{"version":2,"expected_sha":"{expected}","nonce":"{"1" * 32}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "current"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "older"
  ))
  monkeypatch.setattr(host, "rollback_image_level", lambda _image: 0)
  monkeypatch.setattr(host, "live_database_floor", lambda cid: 1 if cid == "cid" else None)
  discarded = []
  monkeypatch.setattr(host, "discard_pulled_image", discarded.append)
  for seam in ("request_drain", "compose", "write_transaction"):
    monkeypatch.setattr(host, seam, lambda *_a, _s=seam, **_k: (_ for _ in ()).throw(
      AssertionError(f"{_s} must not run for an image below the floor")))
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )
  assert host.run() == 1
  assert statuses[-1]["state"] == "failed"
  assert statuses[-1]["code"] == "newer_version_required"
  assert discarded == [f"{host.IMAGE}:sha-{expected}"]
  assert host.read_transaction() is None


def test_an_unreadable_floor_refuses_the_replacement(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "f" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "current"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "newer"
  ))

  def unreadable(_cid):
    raise RuntimeError("the database compatibility floor could not be read (error=hot_journal)")

  monkeypatch.setattr(host, "live_database_floor", unreadable)
  monkeypatch.setattr(host, "request_drain", lambda *_a: (_ for _ in ()).throw(
    AssertionError("an unreadable floor must not drain")))
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )
  assert host.run() == 1
  assert statuses[-1]["state"] == "failed"
  assert "floor could not be read" in statuses[-1]["message"]


def test_the_live_floor_is_read_by_the_app_user_inside_the_serving_container(monkeypatch):
  seen = []

  def execute(args, **_kwargs):
    seen.append(args)
    return subprocess.CompletedProcess(args, 0, stdout="floor=1\n", stderr="")

  monkeypatch.setattr(host.subprocess, "run", execute)
  assert host.live_database_floor("cid") == 1
  assert seen[0][:5] == ["docker", "exec", "-u", "mobius", "cid"]
  assert seen[0][-1] == host.ROLLBACK_DATABASE
