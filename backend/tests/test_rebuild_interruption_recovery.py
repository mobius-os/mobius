"""Adversarial host recovery with real journals/ledger and a stateful Docker fake."""

import json
import os
import subprocess

import pytest

from tests.test_mobius_rebuild_host import host, _real_cutover


class PowerLoss(BaseException):
  pass


@pytest.fixture
def incident(tmp_path, monkeypatch):
  config, tx, ledger, now = _real_cutover(tmp_path, monkeypatch, consume=False)
  tx["phase"] = "replacement_started"
  host.write_transaction(tx)
  clock = [0.0]
  monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(host.time, "time", lambda: now + 3 + clock[0])
  monkeypatch.setattr(host.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
  original_rearm = host.rearm_rollback
  monkeypatch.setattr(host, "rearm_rollback", lambda c, op, **kwargs:
                      original_rearm(c, op, trusted_uid=os.getuid(), trusted_gid=os.getgid(), **kwargs))
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda _cid: (b"stranded", b"", False, False))
  monkeypatch.setattr(host, "verify_served_generation", lambda *_a: None)
  monkeypatch.setattr(host, "retain_images", lambda *_a: None)
  monkeypatch.setattr(host, "adopt_from_image", lambda *_a: "test")

  class Docker:
    cid = "a" * 64
    image = tx["target_image"]
    state = "created"
    started = None
    creates = 0
    boots = 0
    stale = False
    errors = 0
    crash = None
    calls = []

    def boundary(self, boundary):
      if self.crash == boundary:
        self.crash = None
        raise PowerLoss(boundary)

    def command(self, args, **kwargs):
      self.calls.append((list(args), kwargs))
      output = ""
      if args[1] == "compose":
        if "ps" in args:
          output = "f" * 64 if self.stale else self.cid
        elif "up" in args:
          assert kwargs["timeout"] == host.COMPOSE_MUTATION_SECONDS
          if "--no-start" not in args:
            assert kwargs["env"]["MOBIUS_IMAGE"] == host.TARGET_TAG
            # Compose deleted the previous container but never started target.
            self.cid, self.image, self.state = "a" * 64, tx["target_image"], "created"
            self.started = None
            self.stale = True
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
          self.boundary("create-before")
          self.creates += 1
          self.cid = f"{self.creates:064x}"
          self.image, self.state, self.started = tx["previous_image"], "created", None
          self.boundary("create-after")
        else:
          pytest.fail(str(args))
      elif args[1:3] == ["container", "inspect"]:
        if self.stale or self.errors:
          self.stale = False
          self.errors = max(0, self.errors - 1)
          raise subprocess.CalledProcessError(1, args, stderr="No such container")
        assert args[-1] == self.cid
        if args[-2] == "{{json .}}":
          output = json.dumps({"Id": self.cid, "Image": self.image, "RestartCount": 0,
                               "State": {"Status": self.state, "StartedAt":
                                         "0001-01-01T00:00:00Z" if self.started is None
                                         else "2026-10-09T16:00:00Z"}})
        else:
          health = "healthy" if self.started is not None and clock[0] - self.started >= 203 else "starting"
          output = f"{self.image} {self.state}" + (f" {health}" if self.state == "running" else "")
      elif args[1] == "start":
        self.boundary("start-before")
        assert args[-1] == self.cid and self.started is None
        self.boots += 1
        self.state, self.started = "running", clock[0]
        ledger.begin_boot(f"rollback-boot-{self.boots:08d}", now=host.time.time())
        self.boundary("start-after")
      elif args[1] == "exec":
        assert args[-2] == "finalize-cutover"
        ledger.finalize_cutover(args[-1], now=host.time.time())
      elif args[1] not in {"tag", "ps", "rm"}:
        pytest.fail(str(args))
      return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

  docker = Docker()
  monkeypatch.setattr(host, "docker_command", docker.command)
  return config, tx, ledger, docker, clock


def test_compose_timeout_and_stale_deleted_id_recover_slow_rollback(incident, monkeypatch):
  config, tx, ledger, docker, clock = incident
  host.clear_transaction()
  inbox = config["control_dir"] / "inbox"
  (inbox / "request.json").write_text(json.dumps({
    "version": 2, "expected_sha": tx["expected_sha"], "nonce": tx["request_nonce"],
  }))
  # Keep this operation tied to the real drained ledger prepared by the fixture.
  monkeypatch.setattr(host.uuid, "uuid4", lambda: type("UUID", (), {"hex": tx["operation_id"]})())
  monkeypatch.setattr(host, "app_container", lambda _c: ("f" * 64, tx["previous_image"]))
  monkeypatch.setattr(host, "require_pull_space", lambda *_a: None)
  monkeypatch.setattr(host, "record_pulled_image", lambda *_a: None)
  monkeypatch.setattr(host, "request_drain", lambda *_a: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template:
                      tx["expected_sha"] if "revision" in template else
                      host.IMAGE_SOURCE if "source" in template else
                      "amd64" if "Architecture" in template else tx["target_image"])
  original = docker.command
  monkeypatch.setattr(host, "docker_command", lambda args, **kwargs:
                      subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                      if args[1] == "pull" else original(args, **kwargs))
  assert host.run() == 1
  assert host.read_transaction()["phase"] == "replacement_started"
  assert docker.state == "created" and docker.boots == 0
  assert host.reconcile() == 0
  assert clock[0] >= 203
  assert docker.boots == 1 and docker.image == tx["previous_image"]
  assert host.read_json(host.STATUS)["state"] == "rolled_back"
  assert not host.TRANSACTION.exists()
  assert not ledger.ACCEPTED_PATH.exists()
  host.reconcile()
  assert docker.boots == 1


@pytest.mark.parametrize("crash", [
  "creating-before", "creating-after", "prepared-before", "prepared-after",
  "starting-before", "starting-after", "outcome-before", "outcome-after",
  "create-before", "create-after", "start-before", "start-after", "rearm-before", "rearm-after",
])
def test_power_loss_at_every_rollback_boundary_never_repeats_boot(incident, monkeypatch, crash):
  config, tx, ledger, docker, _clock = incident
  docker.crash = crash
  persist = host.write_transaction

  def write(value):
    stage = "outcome" if value.get("outcome") else value.get("rollback_stage", "other")
    docker.boundary(f"{stage}-before")
    persist(value)
    docker.boundary(f"{stage}-after")

  rearm = host.rearm_rollback

  def arm(*args, **kwargs):
    docker.boundary("rearm-before")
    result = rearm(*args, **kwargs)
    docker.boundary("rearm-after")
    return result

  monkeypatch.setattr(host, "write_transaction", write)
  monkeypatch.setattr(host, "rearm_rollback", arm)
  with pytest.raises(PowerLoss):
    host.recover(config, tx)
  assert host.TRANSACTION.exists()
  host.reconcile()
  assert docker.boots == 1
  assert host.read_json(host.STATUS)["state"] == "rolled_back"
  assert not host.TRANSACTION.exists()
  assert not ledger.ACCEPTED_PATH.exists()
  before = docker.creates, docker.boots
  host.reconcile()
  assert (docker.creates, docker.boots) == before


@pytest.mark.parametrize("boundary", [
  "outcome-1-before", "outcome-1-after", "finalize-before", "finalize-after",
  "outcome-2-before", "outcome-2-after", "outcome-3-before", "outcome-3-after",
  "status-before", "status-after", "clear-before", "clear-after",
])
def test_settlement_interruption_preserves_consumed_boot_and_honest_outcome(
  incident, monkeypatch, boundary,
):
  config, tx, ledger, docker, _clock = incident
  docker.crash = boundary
  persist, publish, clear = host.write_transaction, host.write_status, host.clear_transaction
  finalize = host.restart_ledger
  outcomes = [0]

  def write(value):
    if value.get("outcome"):
      outcomes[0] += 1
      stage = f"outcome-{outcomes[0]}"
      docker.boundary(f"{stage}-before")
      persist(value)
      docker.boundary(f"{stage}-after")
    else:
      persist(value)

  def finish(*args, **kwargs):
    if args[2] != "finalize-cutover":
      return finalize(*args, **kwargs)
    docker.boundary("finalize-before")
    result = finalize(*args, **kwargs)
    docker.boundary("finalize-after")
    return result

  def status(*args, **kwargs):
    if kwargs.get("state") == "rolled_back":
      docker.boundary("status-before")
      publish(*args, **kwargs)
      docker.boundary("status-after")
    else:
      publish(*args, **kwargs)

  def remove():
    docker.boundary("clear-before")
    clear()
    docker.boundary("clear-after")

  monkeypatch.setattr(host, "write_transaction", write)
  monkeypatch.setattr(host, "restart_ledger", finish)
  monkeypatch.setattr(host, "write_status", status)
  monkeypatch.setattr(host, "clear_transaction", remove)
  with pytest.raises(PowerLoss):
    host.recover(config, tx)
  assert docker.boots == 1
  assert ledger.ACK_PATH.exists() and not ledger.ACCEPTED_PATH.exists()
  host.reconcile()
  host.reconcile()
  assert docker.boots == 1 and not host.TRANSACTION.exists()
  outcome = host.read_json(host.STATUS)
  assert outcome["state"] == "rolled_back"
  # Once the preliminary outcome is durable, recovery does not invent proof
  # that finalization completed in the gap before its second durable write.
  if boundary in {"outcome-1-after", "finalize-before", "finalize-after", "outcome-2-before"}:
    assert outcome["code"] == "handoff_finalize_unconfirmed"


def test_transient_daemon_failure_retries_on_next_reconcile(incident):
  config, tx, _ledger, docker, _clock = incident
  docker.errors = 3
  host.recover(config, tx)
  assert docker.boots == 0 and host.TRANSACTION.exists()
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  host.reconcile()
  assert docker.boots == 1 and not host.TRANSACTION.exists()


def test_power_loss_loses_docker_metadata_but_cannot_replay_consumed_ledger(incident):
  config, tx, ledger, docker, _clock = incident
  docker.crash = "start-after"
  with pytest.raises(PowerLoss):
    host.recover(config, tx)
  assert ledger.ACK_PATH.exists() and not ledger.ACCEPTED_PATH.exists()
  ack = ledger.ACK_PATH.read_bytes()
  # Moby starts the process before checkpointing StartedAt/HasBeenStartedBefore.
  # Power loss can restore this metadata even though begin-boot was durable.
  docker.state, docker.started = "created", None
  host.reconcile()
  assert docker.boots == 1
  assert ledger.ACK_PATH.read_bytes() == ack
  assert host.TRANSACTION.exists()


@pytest.mark.parametrize("later", ["created", "healthy"])
def test_delayed_compose_source_observation_does_not_become_a_rollback(incident, later):
  config, tx, ledger, docker, _clock = incident
  docker.image, docker.state, docker.started = tx["previous_image"], "running", -203
  accepted = ledger.ACCEPTED_PATH.read_bytes()
  host.recover(config, tx)
  assert host.read_transaction()["phase"] == "replacement_started"
  assert ledger.ACCEPTED_PATH.read_bytes() == accepted
  assert docker.creates == docker.boots == 0
  docker.image = tx["target_image"]
  if later == "healthy":
    assert ledger.begin_boot("delayed-target-boot", now=host.time.time())
  else:
    docker.state, docker.started = "created", None
  host.reconcile()
  assert not host.TRANSACTION.exists()
  if later == "healthy":
    assert docker.creates == docker.boots == 0
    assert host.read_json(host.STATUS)["state"] == "succeeded"
  else:
    assert docker.creates == docker.boots == 1
    assert host.read_json(host.STATUS)["state"] == "rolled_back"


@pytest.mark.parametrize("state", ["running", "restarting"])
def test_target_with_large_data_gets_more_than_180_seconds_to_become_ready(incident, monkeypatch, state):
  config, tx, ledger, docker, clock = incident
  assert ledger.begin_boot("target-boot-12345678", now=host.time.time())
  docker.state, docker.started = state, 0

  def sleep(delay):
    clock[0] += delay
    if clock[0] >= 203:
      docker.state = "running"

  monkeypatch.setattr(host.time, "sleep", sleep)
  host.recover(config, tx)
  assert clock[0] >= 203
  assert docker.creates == docker.boots == 0
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert not host.TRANSACTION.exists()


def test_missing_target_can_create_one_rollback(incident):
  config, tx, _ledger, docker, _clock = incident
  docker.cid = ""
  host.recover(config, tx)
  assert docker.boots == 1 and not host.TRANSACTION.exists()


def test_consumed_target_authorizes_exactly_one_rollback(incident):
  config, tx, ledger, docker, _clock = incident
  assert ledger.begin_boot("target-boot-12345678", now=host.time.time())
  docker.state, docker.started = "exited", 0
  host.recover(config, tx)
  assert docker.boots == 1 and not host.TRANSACTION.exists()
  ack = json.loads(ledger.ACK_PATH.read_text())
  assert ack["source_boot_id"] == "target-boot-12345678"
  assert ack["target_boot_id"] == "rollback-boot-00000001"


@pytest.mark.parametrize("consumed", [False, True])
def test_legacy_created_rollback_requires_unconsumed_ledger(incident, consumed):
  config, tx, ledger, docker, _clock = incident
  tx["phase"] = "rollback_started"
  host.write_transaction(tx)
  docker.image = tx["previous_image"]
  if consumed:
    ledger.begin_boot("rollback-already-booted", now=host.time.time())
  host.recover(config, tx)
  assert docker.boots == (0 if consumed else 1)
  assert host.TRANSACTION.exists() == consumed


def test_daemon_restart_after_rollback_never_rearms_or_recreates(incident):
  config, tx, ledger, docker, _clock = incident
  docker.crash = "start-after"
  with pytest.raises(PowerLoss):
    host.recover(config, tx)
  assert not ledger.begin_boot("daemon-autonomous-restart", now=host.time.time())
  host.reconcile()
  assert docker.boots == 1 and docker.creates == 1
  assert host.read_json(host.STATUS)["code"] == "handoff_boot_unconfirmed"
  assert host.TRANSACTION.exists()


@pytest.mark.parametrize("corruption", ["nonce", "action", "source", "nan", "symlink", "mode"])
def test_host_rearm_rejects_untrusted_or_mismatched_authorization(incident, corruption):
  config, tx, ledger, _docker, _clock = incident
  path = ledger.ACCEPTED_PATH
  original = path.read_bytes()
  if corruption == "symlink":
    path.unlink()
    path.symlink_to(ledger.CUTOVER_RECEIPT_PATH)
  elif corruption == "mode":
    path.chmod(0o666)
  else:
    if corruption == "nan":
      path = ledger.CUTOVER_RECEIPT_PATH
    value = json.loads(path.read_text())
    field, replacement = {
      "nonce": ("nonce", "wrong-nonce-1234"), "action": ("action", "ordinary"),
      "source": ("source_boot_id", "stale-boot-1234"), "nan": ("accepted_at", float("nan")),
    }[corruption]
    value[field] = replacement
    path.chmod(0o644)
    path.write_text(json.dumps(value))
  assert not host.rearm_rollback(config, tx["operation_id"])
  if corruption == "nan":
    assert ledger.ACCEPTED_PATH.read_bytes() == original


@pytest.mark.parametrize("age, authorized", [(601, True), (3590, True), (3601, False)])
def test_rearm_refreshes_pending_acceptance_but_never_expired_receipt(incident, age, authorized):
  config, tx, ledger, docker, clock = incident
  clock[0] = age
  host.recover(config, tx)
  assert docker.boots == 1 and docker.state == "running"
  assert ledger.ACK_PATH.exists() == authorized
  assert host.TRANSACTION.exists() != authorized
  if not authorized:
    assert host.read_json(host.STATUS)["code"] == "handoff_boot_unconfirmed"
    host.reconcile()
    assert docker.boots == 1 and host.TRANSACTION.exists()


@pytest.mark.parametrize("stage", ["prepared", "starting"])
@pytest.mark.parametrize("state", ["exited", "dead", "missing"])
def test_lost_or_attempted_rollback_cannot_be_recreated(incident, stage, state):
  config, tx, _ledger, docker, _clock = incident
  tx.update(phase="rollback_started", rollback_stage=stage, rollback_container=docker.cid)
  host.write_transaction(tx)
  docker.image = tx["previous_image"]
  docker.state, docker.started = state, 0
  if state == "missing":
    docker.cid = ""
  host.recover(config, tx)
  host.reconcile()
  assert docker.creates == docker.boots == 0
  assert host.TRANSACTION.exists()


@pytest.mark.parametrize("delay,continued", [(601, True), (3590, True), (5400, False)])
def test_delayed_preboot_retry_refreshes_only_unexpired_handoff(incident, delay, continued):
  config, tx, ledger, docker, clock = incident
  docker.crash = "start-before"
  with pytest.raises(PowerLoss):
    host.recover(config, tx)
  assert host.read_transaction()["rollback_stage"] == "starting"
  assert docker.boots == 0
  receipt = ledger.CUTOVER_RECEIPT_PATH.read_bytes()
  clock[0] += delay
  host.reconcile()
  assert docker.boots == 1
  assert ledger.ACK_PATH.exists() == continued
  assert host.TRANSACTION.exists() != continued
  if not continued:
    assert ledger.CUTOVER_RECEIPT_PATH.read_bytes() == receipt
    assert host.read_json(host.STATUS)["code"] == "handoff_boot_unconfirmed"
  host.reconcile()
  assert docker.boots == 1


@pytest.mark.parametrize("boundary", ["prepared-before", "prepared-after", "rearm-after", "starting-after"])
def test_interruption_during_delayed_acceptance_refresh_recovers(incident, monkeypatch, boundary):
  config, tx, ledger, docker, clock = incident
  docker.crash = "start-before"
  with pytest.raises(PowerLoss):
    host.recover(config, tx)
  clock[0] += 601
  docker.crash = boundary
  persist = host.write_transaction
  def write(value):
    stage = value.get("rollback_stage", "other")
    docker.boundary(f"{stage}-before")
    persist(value)
    docker.boundary(f"{stage}-after")
  rearm = host.rearm_rollback
  def arm(*args, **kwargs):
    result = rearm(*args, **kwargs)
    if not kwargs.get("witness_only"):
      docker.boundary("rearm-after")
    return result
  monkeypatch.setattr(host, "write_transaction", write)
  monkeypatch.setattr(host, "rearm_rollback", arm)
  with pytest.raises(PowerLoss):
    host.reconcile()
  host.reconcile()
  assert docker.boots == 1 and ledger.ACK_PATH.exists()
  assert not host.TRANSACTION.exists()


def test_duplicate_containers_never_choose_or_mutate_arbitrarily(incident, monkeypatch):
  config, tx, ledger, docker, _clock = incident
  accepted = ledger.ACCEPTED_PATH.read_bytes()
  original = docker.command
  ambiguous = [True]
  def command(args, **kwargs):
    if ambiguous[0] and args[1] == "compose" and "ps" in args:
      return subprocess.CompletedProcess(args, 0, stdout=docker.cid + "\n" + "f" * 64, stderr="")
    return original(args, **kwargs)
  monkeypatch.setattr(host, "docker_command", command)
  host.recover(config, tx)
  host.reconcile()
  assert docker.creates == docker.boots == 0
  assert ledger.ACCEPTED_PATH.read_bytes() == accepted
  assert host.TRANSACTION.exists()
  ambiguous[0] = False
  host.reconcile()
  assert docker.boots == 1 and not host.TRANSACTION.exists()
