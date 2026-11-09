"""Settings-to-host-worker contract for self-hosted replacement without Connect.

The app half (``deployment_control``) and the host half (``mobius-rebuild-host``)
meet only at the durable inbox and the published status. Both run for real
here, together with the installed boot admission gate and the frozen restart
ledger; only the Docker daemon is the asynchronous fake shared with
``test_admission_host_recovery``.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from app import deployment_control as dc
from tests.test_admission_host_recovery import (
  NEW_IMAGE, OLD_IMAGE, ROOT, SOURCE, SOURCE_BOOT, Docker, legacy_for, load,
)


TARGET = "c" * 40
NOW = 1001.0


class SettingsDocker(Docker):
  """The incident fake plus the daemon surface of a fresh official replacement:
  registry pull, image metadata, and the source container's ledger/drain execs."""

  def __init__(self, host, legacy, topology, clock, docker_root):
    super().__init__(host, None, legacy, topology, clock)
    self.docker_root = docker_root
    # Immutable local image store: Id -> OCI labels and architecture.
    self.images = {
      OLD_IMAGE: {"revision": "d" * 40, "source": host.IMAGE_SOURCE, "arch": "amd64"},
    }
    # The registry publishes exactly one immutable image per release tag.
    self.registry = {
      f"{host.IMAGE}:sha-{TARGET}": (
        NEW_IMAGE, {"revision": TARGET, "source": host.IMAGE_SOURCE, "arch": "amd64"},
      ),
    }
    # Every daemon command in order, including those the base fake answers.
    self.refs, self.log, self.pulls = {}, [], []

  def _image(self, ref):
    digest = self.refs.get(ref, ref)
    return digest, self.images.get(digest)

  def command(self, args, **kwargs):
    args = list(args)
    self.log.append(args)
    check = kwargs.get("check", True)
    if args[1] == "pull":
      digest, meta = self.registry[args[2]]
      self.pulls.append(args[2])
      self.images[digest], self.refs[args[2]] = meta, digest
      return self._reply(args)
    if args[1] == "tag":
      digest, meta = self._image(args[2])
      assert meta is not None
      self.refs[args[3]] = digest
      return self._reply(args)
    if args[1] == "info":
      return self._reply(args, str(self.docker_root))
    if args[1:3] == ["image", "rm"]:
      return self._reply(args)
    if args[1:3] == ["image", "inspect"] and args[-2] != "{{json .Config}}":
      template = args[-2]
      digest, meta = self._image(args[-1])
      if meta is None:
        return self._reply(args, code=1, error=f"Error: No such image: {args[-1]}", check=check)
      output = (digest if template == "{{.Id}}" else
                str(64 * 1024 * 1024) if template == "{{.Size}}" else
                meta["revision"] if "image.revision" in template else
                meta["source"] if "image.source" in template else
                meta["arch"] if template == "{{.Architecture}}" else None)
      assert output is not None, template
      return self._reply(args, output)
    if args[1:3] == ["container", "inspect"] and args[-2] == "{{json .Mounts}}":
      return self._reply(args, json.dumps(self.containers[args[-1]]["Mounts"]))
    if args[1] == "exec" and args[-2] != "finalize-cutover":
      cid, invocation = args[2], args[3:]
      assert self.containers[cid]["State"]["Status"] == "running"
      if invocation[:3] == ["python3", "-P", "/app/runtime/restart_ledger.py"]:
        command, operation = invocation[3:]
        assert cid == SOURCE and command in {"open-cutover", "accept-cutover"}
        action = getattr(self.legacy, command.replace("-", "_"))
        return self._reply(args, code=0 if action(operation, now=self.clock[0]) else 1, check=check)
      if invocation[:2] == ["python3", "/data/platform/backend/scripts/prepare-container-cutover.py"]:
        # The drained source app writes the platform half of the handoff for
        # the boot the ledger recorded; the root challenge is the other half.
        assert cid == SOURCE
        self.legacy.INTENT_PATH.write_text(json.dumps({
          "version": 1, "action": "external_cutover", "cutover_id": invocation[2],
          "nonce": "drained-chats-nonce-12345678",
          "source_boot_id": self.legacy.BOOT_PATH.read_text().strip(),
          "created_at": self.clock[0],
        }))
        return self._reply(args)
      if invocation[:2] == ["curl", "-fsS"]:
        # A container serves the revision baked into its immutable image.
        revision = self.images[self.containers[cid]["Image"]]["revision"]
        return self._reply(args, json.dumps({"sha": revision}))
      raise AssertionError(f"unexpected exec: {args}")
    return super().command(args, **kwargs)

  def index(self, predicate):
    return next(i for i, args in enumerate(self.log) if predicate(args))


@pytest.fixture
def deployment(tmp_path, monkeypatch):
  """One installed host helper (revision-5 gate present), a running source
  container booted through the frozen ledger, and the app's control files."""
  host = load(ROOT / "scripts/mobius-rebuild-host.py", "settings_path_host")
  gate_module = load(ROOT / "scripts/mobius-boot-admission.py", "settings_path_gate")
  data, state, config_root = (tmp_path / name for name in ("data", "host-state", "host-config"))
  for path in (data, state, config_root):
    path.mkdir(mode=0o700)
  control = data / "mobius-rebuild"
  (control / "inbox").mkdir(parents=True)
  paths = {"STATE_DIR": state, "CONFIG": config_root / "config.json", "COMPOSE": config_root / "compose.json",
           "OVERRIDE": config_root / "image.override.json", "STATUS": state / "status.json",
           "LOCK": state / "replace.lock", "TRANSACTION": state / "transaction.json",
           "IMAGES": state / "images.json", "FAILED_TARGET_LOG": state / "failed-target.json",
           "ADMISSION_ROOT": state / "admission", "ADMISSION_CODE": ROOT / "scripts/mobius-boot-admission.py"}
  for name, path in paths.items():
    monkeypatch.setattr(host, name, path)
  host.ADMISSION_ROOT.mkdir(mode=0o700)
  config = {"version": 4, "project": "original", "data_dir": data, "control_dir": control,
            "deployment_name": "original-app", "name_source": "explicit"}
  topology = {"name": "original", "services": {"app": {
    "image": OLD_IMAGE, "container_name": "original-app", "networks": {"default": {}},
    "volumes": [{"type": "bind", "source": str(data), "target": "/data"}],
    "restart": "unless-stopped",
  }}, "networks": {"default": {"name": "original_default"}}}
  host.COMPOSE.write_text(json.dumps(topology))
  host.OVERRIDE.write_text(json.dumps({"services": {"app": {"image": OLD_IMAGE}}}))
  legacy = legacy_for(data)
  assert not legacy.begin_boot(SOURCE_BOOT, now=NOW - 1)
  clock = [NOW]
  docker = SettingsDocker(host, legacy, topology, clock, tmp_path)
  stores = {}

  def admission_store(config_value, transaction):
    # The installer-pinned gate, trusted for this unprivileged test user. The
    # fake daemon enters through the store of the operation that owns it.
    operation = transaction["operation_id"]
    if operation not in stores:
      stores[operation] = gate_module.AdmissionStore(
        host.ADMISSION_ROOT / operation, config_value["data_dir"], os.getuid(), os.getgid())
    docker.gate = stores[operation]
    return stores[operation]

  adopted = []
  real_docker_command = host.docker_command
  monkeypatch.delenv("MOBIUS_REBUILD_LAUNCHER", raising=False)
  monkeypatch.setattr(host, "config", lambda: config)
  monkeypatch.setattr(host, "admission_store", admission_store)
  monkeypatch.setattr(host, "docker_command", docker.command)
  monkeypatch.setattr(host, "shutil", SimpleNamespace(
    disk_usage=lambda _path: SimpleNamespace(free=8 * 1024 ** 3)))
  monkeypatch.setattr(host.time, "time", lambda: clock[0])
  monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(host.time, "sleep", docker.sleep)
  monkeypatch.setattr(host, "_bounded_docker_logs", lambda *_: (b"fake target evidence", b"", False, False))
  monkeypatch.setattr(host, "adopt_from_image", lambda image: adopted.append(image) or "adopted")
  proof = host.cutover_boot_consumed
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda c, op, **kwargs:
                      proof(c, op, trusted_uid=os.getuid(), trusted_gid=os.getgid(), **kwargs))

  # App half: same control directory, a prepared image-requiring update, and
  # no managed (Connect/Railway) controller reachable at all.
  prepared = tmp_path / "prepared-update.json"
  prepared.write_text(json.dumps({"state": "prepared", "target": TARGET, "requires_image": True}))
  monkeypatch.setattr(dc, "_control_dir", lambda: control)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(dc.platform_update, "PREPARED_UPDATE_PATH", prepared)
  monkeypatch.setattr(dc.platform_update, "RECONCILE_LOCK", tmp_path / "reconcile.lock")

  def no_connect(*_args, **_kwargs):
    raise AssertionError("self-hosted replacement must not use a managed controller")

  monkeypatch.setattr(dc, "_managed_request", no_connect)
  # Synthetic targets are not commits here; they require what is served.
  served = dc._required_helper_protocol()
  monkeypatch.setattr(dc, "_required_helper_protocol", lambda target_sha=None, repo=None: served)
  # The installer certifies its completed protocol, then publishes capability;
  # not a hand-written status file.
  certified = tmp_path / "helper-protocol"
  certified.write_text(dc._REQUIRED_HELPER_PROTOCOL.read_text())
  certified.chmod(0o600)
  monkeypatch.setattr(host, "HELPER_PROTOCOL", certified)
  installed = host.installed_helper_protocol
  monkeypatch.setattr(host, "installed_helper_protocol",
                      lambda: installed(trusted_uid=os.getuid()))
  assert host.publish_capabilities() == 0
  return SimpleNamespace(host=host, legacy=legacy, docker=docker, stores=stores, config=config,
                         control=control, inbox=control / "inbox", adopted=adopted, clock=clock,
                         real_docker_command=real_docker_command)


@pytest.mark.asyncio
async def test_reviewed_settings_request_reaches_host_worker_without_connect(deployment):
  """The durable inbox is the integration seam; no remote-control app exists."""
  d = deployment
  host, legacy, docker = d.host, d.legacy, d.docker
  outcome = await dc._request_self_hosted_rebuild(expected_sha=TARGET, final_check=lambda: None)
  nonce = outcome["request_nonce"]
  assert outcome["state"] == "queued" and len(nonce) == 32
  operation = {"controller": "host", "id": nonce}
  # Bound before the helper can see it; the inbox holds exactly that request.
  assert dc.platform_update.read_prepared_update()["operation"] == operation
  assert json.loads((d.inbox / "request.json").read_text()) == {
    "version": 2, "expected_sha": TARGET, "nonce": nonce,
  }
  queued = await dc.read_rebuild_status()
  assert (queued["state"], queued["request_nonce"]) == ("queued", nonce)
  assert not docker.log  # nothing reached the daemon before the worker ran

  assert host.run() == 0

  # The worker claimed exactly this request and left no claim debris.
  assert not os.path.lexists(d.inbox / "request.json")
  assert not list(d.control.glob(".request-*"))
  assert not host.TRANSACTION.exists()
  [op] = d.stores
  gate = d.stores[op].observe()

  # It pulled the exact release tag once and ran the immutable image it
  # verified, never the mutable tag.
  assert docker.pulls == [f"{host.IMAGE}:sha-{TARGET}"]
  assert docker.refs[host.ROLLBACK_TAG] == OLD_IMAGE
  [(target_cid, project, token, _document)] = docker.creates
  container = docker.containers[target_cid]
  assert container["Image"] == NEW_IMAGE
  assert container["Config"]["Labels"]["io.mobius.admission.token"] == token
  assert container["Config"]["Labels"]["io.mobius.admission.operation"] == op
  assert container["Config"]["Entrypoint"][:5] == [
    "python3", "-I", "-S", host.ADMISSION_CODE_TARGET, "enter"]
  assert project == "mobius-admission-" + token

  # Order: open challenge, drain, accept, fence the source, create, Start,
  # then provenance on the admitted target and only then finalization.
  ledger = lambda command: lambda a: a[1] == "exec" and command in a
  steps = [
    docker.index(ledger("open-cutover")),
    docker.index(lambda a: a[1] == "exec" and any("prepare-container-cutover" in p for p in a)),
    docker.index(ledger("accept-cutover")),
    docker.index(lambda a: a[1:3] == ["container", "rm"] and a[-1] == SOURCE),
    docker.index(lambda a: a[1] == "compose" and "up" in a),
    docker.index(lambda a: a[1] == "start"),
    docker.index(lambda a: a[1] == "exec" and a[2] == target_cid and "curl" in a),
    docker.index(ledger("finalize-cutover")),
  ]
  assert steps == sorted(steps)
  assert docker.removes == [SOURCE] and docker.starts == [target_cid]

  # Boot admission: one target generation, entered once, as the continuation
  # of the source boot's drained handoff. No rollback was ever allocated.
  assert docker.entries == [(target_cid, "continuation", True)]
  target_slot = gate["slots"]["target"]
  assert [a["cid"] for a in target_slot["attempts"]] == [target_cid]
  assert target_slot["consumed"]["generation"] == 1
  assert gate["slots"]["rollback"] == {"consumed": None, "attempts": []}
  assert gate["source"] == {"cid": SOURCE, "image": OLD_IMAGE, "boot_id": SOURCE_BOOT}
  assert gate["source_handoff_valid"] is True and gate["operation"] == op
  ack = json.loads(legacy.ACK_PATH.read_text())
  assert ack["cutover_id"] == op and ack["source_boot_id"] == SOURCE_BOOT
  assert ack["target_boot_id"] == target_slot["consumed"]["boot_id"]
  assert ack["nonce"] == gate["receipt"]["nonce"] == "drained-chats-nonce-12345678"
  assert not legacy.ACCEPTED_PATH.exists()
  assert not legacy.CUTOVER_RECEIPT_PATH.exists()  # finalized, never re-armable
  assert d.adopted == [NEW_IMAGE]
  assert json.loads(host.IMAGES.read_text())["rollback_image_id"] == OLD_IMAGE

  # The helper echoes the exact request the app bound to the update.
  status = json.loads((d.control / "status.json").read_text())
  assert (status["state"], status["code"]) == ("succeeded", None)
  assert (status["operation_id"], status["expected_sha"], status["request_nonce"]) == (op, TARGET, nonce)
  seen = await dc.read_rebuild_status()
  assert (seen["state"], seen["expected_sha"], seen["request_nonce"]) == ("succeeded", TARGET, nonce)
  record = dc.platform_update.read_prepared_update()
  assert record["operation"] == operation
  assert dc.platform_update.status_reports_bound_operation(dict(seen), record)

  # At most once: later activations, reconciles and the admitted target's own
  # crash restart neither repeat the replacement nor grant another handoff.
  calls = len(docker.log)
  assert host.run() == 0
  for _ in range(2):
    assert host.reconcile() == 0
  assert docker.pulls == [f"{host.IMAGE}:sha-{TARGET}"] and docker.starts == [target_cid]
  assert len(docker.creates) == 1 and docker.removes == [SOURCE]
  assert not any(a[1] in {"pull", "start", "exec"} or a[1:3] == ["container", "rm"]
                 for a in docker.log[calls:])
  assert docker.complete_start(target_cid)
  assert docker.entries == [(target_cid, "continuation", True), (target_cid, "service_only", False)]
  assert not legacy.ACK_PATH.exists() and not legacy.ACCEPTED_PATH.exists()
  after = json.loads((d.control / "status.json").read_text())
  assert (after["state"], after["operation_id"], after["request_nonce"]) == ("succeeded", op, nonce)


@pytest.mark.parametrize("journal_kind", ["legacy", "admission"])
@pytest.mark.asyncio
async def test_real_host_recovery_journal_remains_owned_when_restore_cannot_start(
  deployment, monkeypatch, journal_kind,
):
  """Current worker and caller share the same isolated durable status path."""
  d = deployment
  host, control, inbox = d.host, d.control, d.inbox
  nonce, operation_id = "e" * 32, "a" * 32
  operation = {"controller": "host", "id": nonce}
  prepared_path = dc.platform_update.PREPARED_UPDATE_PATH
  prepared_path.write_text(json.dumps({
    "state": "prepared", "target": TARGET, "operation": operation,
    "requires_image": True,
  }), encoding="utf-8")
  journal = host.transaction_record(
    operation_id, TARGET, nonce, "sha256:" + "1" * 64, "sha256:" + "2" * 64,
  )
  if journal_kind == "admission":
    journal.update(admission_version=1, source_container=SOURCE)
  host.write_transaction(journal)

  def restore_unavailable(args, **_kwargs):
    raise subprocess.CalledProcessError(1, args)

  # Real Docker transport, with no daemon able to start anything.
  monkeypatch.setattr(host, "docker_command", d.real_docker_command)
  monkeypatch.setattr(host.subprocess, "Popen", restore_unavailable)
  host.recover(d.config, journal)
  assert host.read_transaction()["operation_id"] == operation_id
  queued = {"version": 2, "expected_sha": TARGET, "nonce": "f" * 32}
  (inbox / "request.json").write_text(json.dumps(queued), encoding="utf-8")
  status = await dc.read_rebuild_status()
  assert (status["state"], status["request_nonce"], status["operation_id"]) == (
    "needs_recovery", nonce, operation_id,
  )
  assert dc.platform_update.read_prepared_update()["operation"] == operation
  for action in (
    dc.release_ended_binding,
    lambda: dc._request_self_hosted_rebuild(expected_sha=TARGET, final_check=lambda: None),
    dc.withdraw_unclaimed_host_request,
  ):
    with pytest.raises(dc.DeploymentControlError) as exc:
      await action()
    assert exc.value.code == "recovery_required"
  # The host worker does not bypass the journal either: the queued request
  # stays unclaimed for after recovery.
  assert host.run() == 0
  assert host.read_transaction()["operation_id"] == operation_id
  assert dc.platform_update.read_prepared_update()["operation"] == operation
  assert json.loads((inbox / "request.json").read_text(encoding="utf-8")) == queued
  assert not list(control.glob(".request-*"))
  assert not d.docker.starts and not d.docker.removes and not d.docker.creates
