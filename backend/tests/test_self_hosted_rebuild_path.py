"""Inbox-to-worker contract for Settings replacement without Connect."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from app import deployment_control as dc


SCRIPT = Path(__file__).parents[2] / "scripts" / "mobius-rebuild-host.py"
SPEC = importlib.util.spec_from_file_location("mobius_rebuild_host_portable", SCRIPT)
assert SPEC and SPEC.loader
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


@pytest.mark.asyncio
async def test_reviewed_settings_request_reaches_host_worker_without_connect(
  tmp_path, monkeypatch,
):
  """The durable inbox is the integration seam; no remote-control app exists."""
  control = tmp_path / "data" / "mobius-rebuild"
  inbox = control / "inbox"
  state = tmp_path / "state"
  inbox.mkdir(parents=True)
  state.mkdir()
  (control / "status.json").write_text(
    '{"state":"idle","handoff":"external-cutover-v1","request_versions":[1,2]}', encoding="utf-8",
  )
  monkeypatch.setattr(dc, "_control_dir", lambda: control)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  bound = []
  monkeypatch.setattr(
    dc.platform_update, "bind_update_operation",
    lambda target, operation, **_kw: bound.append(operation),
  )

  target = "c" * 40
  outcome = await dc._request_self_hosted_rebuild(
    expected_sha=target, final_check=lambda: None,
  )
  assert outcome["state"] == "queued"

  data = tmp_path / "data"
  config = {"project": "mobius", "control_dir": control, "data_dir": data}
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "LOCK", state / "replace.lock")
  monkeypatch.setattr(host, "STATUS", state / "status.json")
  monkeypatch.setattr(host, "IMAGES", state / "images.json")
  monkeypatch.setattr(host, "TRANSACTION", state / "transaction.json")
  monkeypatch.setattr(host, "config", lambda: config)
  containers = iter([("old-container", "sha256:old"), ("new-container", "sha256:new")])
  monkeypatch.setattr(host, "app_container", lambda _config: next(containers))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(
    host, "docker_command",
    lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
  )
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    target if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else
    "sha256:new"
  ))
  events = []
  monkeypatch.setattr(host, "request_drain", lambda *_args: events.append("drain"))
  monkeypatch.setattr(
    host, "compose",
    lambda _config, *args, image=None, **_kwargs:
      events.append(("compose", args, image)),
  )
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  monkeypatch.setattr(
    host, "verify_served_generation",
    lambda cid, sha: events.append(("verified", cid, sha)),
  )
  monkeypatch.setattr(host, "restart_ledger", lambda *_args, **_kwargs: True)
  # Like the mocked ledger commands, this integration fixture represents a
  # boot that consumed the accepted handoff; real evidence is tested separately.
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda *_args, **_kwargs: True)

  assert host.run() == 0
  assert not (inbox / "request.json").exists()
  assert events[0] == "drain"
  assert events[1][0] == "compose"
  # Compose starts the verified image through the helper-owned pinned tag.
  assert events[1][2] == host.TARGET_TAG
  assert events[2] == ("verified", "new-container", target)
  status = json.loads((control / "status.json").read_text(encoding="utf-8"))
  assert status["state"] == "succeeded"
  assert status["expected_sha"] == target
  # The helper echoes the exact request the app bound to the update.
  assert bound == [{"controller": "host", "id": status["request_nonce"]}]
  assert outcome["request_nonce"] == status["request_nonce"]


@pytest.mark.asyncio
async def test_real_host_recovery_journal_remains_owned_when_restore_cannot_start(
  tmp_path, monkeypatch,
):
  """Current worker and caller share the same isolated durable status path."""
  control, inbox = tmp_path / "data" / "mobius-rebuild", tmp_path / "data" / "mobius-rebuild" / "inbox"
  state = tmp_path / "host-state"
  inbox.mkdir(parents=True)
  state.mkdir()
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "STATUS", state / "status.json")
  monkeypatch.setattr(host, "TRANSACTION", state / "transaction.json")
  monkeypatch.setattr(dc, "_control_dir", lambda: control)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  target, nonce, operation_id = "c" * 40, "e" * 32, "a" * 32
  operation = {"controller": "host", "id": nonce}
  prepared_path = tmp_path / "prepared-update.json"
  prepared_path.write_text(json.dumps({
    "state": "prepared", "target": target, "operation": operation,
    "requires_image": True,
  }), encoding="utf-8")
  monkeypatch.setattr(dc.platform_update, "PREPARED_UPDATE_PATH", prepared_path)
  monkeypatch.setattr(dc.platform_update, "RECONCILE_LOCK", tmp_path / "reconcile.lock")
  journal = host.transaction_record(
    operation_id, target, nonce, "sha256:" + "1" * 64, "sha256:" + "2" * 64,
  )
  host.write_transaction(journal)

  def restore_unavailable(args, **_kwargs):
    raise subprocess.CalledProcessError(1, args)

  monkeypatch.setattr(host.subprocess, "Popen", restore_unavailable)
  host.recover({
    "project": "recovery-test", "control_dir": control, "data_dir": tmp_path / "data",
  }, journal)
  assert host.read_transaction()["operation_id"] == operation_id
  queued = {"version": 2, "expected_sha": target, "nonce": "f" * 32}
  (inbox / "request.json").write_text(json.dumps(queued), encoding="utf-8")
  status = await dc.read_rebuild_status()
  assert (status["state"], status["request_nonce"], status["operation_id"]) == (
    "needs_recovery", nonce, operation_id,
  )
  assert dc.platform_update.read_prepared_update()["operation"] == operation
  for action in (
    dc.release_ended_binding,
    lambda: dc._request_self_hosted_rebuild(expected_sha=target, final_check=lambda: None),
    dc.withdraw_unclaimed_host_request,
  ):
    with pytest.raises(dc.DeploymentControlError) as exc:
      await action()
    assert exc.value.code == "recovery_required"
  assert host.read_transaction()["operation_id"] == operation_id
  assert dc.platform_update.read_prepared_update()["operation"] == operation
  assert json.loads((inbox / "request.json").read_text(encoding="utf-8")) == queued
