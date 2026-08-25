"""Container replacement request and status boundary tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import deployment_control as dc


def _install_control(tmp_path, monkeypatch):
  control = tmp_path / "mobius-rebuild"
  inbox = control / "inbox"
  inbox.mkdir(parents=True)
  (control / "status.json").write_text(
    '{"state":"idle","handoff":"external-cutover-v1"}',
    encoding="utf-8",
  )
  monkeypatch.setattr(dc, "_control_dir", lambda: control)
  return control, inbox


def test_normalize_rejects_unknown_controller_state():
  with pytest.raises(dc.DeploymentControlError) as exc:
    dc._normalize_status({"state": "doing_magic"})
  assert exc.value.code == "controller_invalid_response"


@pytest.mark.asyncio
async def test_railway_requires_baked_managed_cutover_support(tmp_path, monkeypatch):
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})())
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(dc.platform_update, "container_replacement_blockers", lambda: [])

  status = await dc.read_rebuild_status()
  assert status["supported"] is False
  assert status["code"] == "controller_upgrade_required"

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  assert exc.value.code == "controller_upgrade_required"


@pytest.mark.asyncio
async def test_railway_status_uses_account_service(tmp_path, monkeypatch):
  (tmp_path / "run").mkdir()
  (tmp_path / "run" / "managed-cutover-ready").touch()
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_managed_request", lambda method, suffix: {
    "operation_id": "replace_123", "state": "deploying",
    "expected_sha": "a" * 40, "message": "Railway is replacing the container.",
  })

  status = await dc.read_rebuild_status()

  assert status["supported"] is True
  assert status["deployment"] == "railway"
  assert status["state"] == "replacing"
  assert status["expected_sha"] == "a" * 40


@pytest.mark.asyncio
async def test_request_rebuild_selects_managed_railway_handoff(tmp_path, monkeypatch):
  from app import restart_ledger, restart_util

  (tmp_path / "run").mkdir()
  (tmp_path / "run" / "managed-cutover-ready").touch()
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(dc.platform_update, "container_replacement_blockers", lambda: [])
  calls = []

  def managed_request(method, suffix, payload=None):
    calls.append((method, suffix, payload))
    if suffix == "prepare":
      return {
        "operation_id": "replace_12345678", "handoff_nonce": "nonce-secret",
        "state": "awaiting_handoff", "expected_sha": "a" * 40,
      }
    return {
      "operation_id": "replace_12345678", "state": "queued",
      "expected_sha": "a" * 40, "message": "Replacement queued.",
    }

  async def prepare(cutover_id):
    assert cutover_id == "replace_12345678"
    return {"status": "prepared"}

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: True)
  monkeypatch.setattr(restart_util, "prepare_managed_container_cutover", prepare)

  status = await dc.request_rebuild()

  assert status["deployment"] == "railway"
  assert status["state"] == "queued"
  assert calls == [
    ("POST", "prepare", {"expected_sha": "a" * 40}),
    ("POST", "start", {
      "operation_id": "replace_12345678", "handoff_nonce": "nonce-secret",
    }),
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("error_code", "restart_count"),
  [("controller_rejected", 1), ("controller_unavailable", 0),
   ("controller_invalid_response", 0)],
)
async def test_managed_start_failure_restarts_only_after_definitive_rejection(
  tmp_path, monkeypatch, error_code, restart_count,
):
  from app import restart_ledger, restart_util

  _install_control(tmp_path, monkeypatch)
  (tmp_path / "run").mkdir()
  (tmp_path / "run" / "managed-cutover-ready").touch()
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(dc.platform_update, "container_replacement_blockers", lambda: [])

  def managed_request(method, suffix, payload=None):
    if suffix == "prepare":
      return {
        "state": "prepared",
        "operation_id": "replace_12345678",
        "handoff_nonce": "nonce-secret",
      }
    raise dc.DeploymentControlError(error_code, "start failed")

  restarts = []

  async def restart():
    restarts.append(True)

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: True)
  monkeypatch.setattr(restart_util, "prepare_managed_container_cutover", lambda _id: asyncio.sleep(0))
  monkeypatch.setattr(restart_util, "restart_this_worker", restart)

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  await asyncio.sleep(0)

  assert exc.value.code == error_code
  assert len(restarts) == restart_count


@pytest.mark.asyncio
async def test_request_writes_only_the_derived_sha(tmp_path, monkeypatch):
  _control, inbox = _install_control(tmp_path, monkeypatch)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "c" * 40)
  monkeypatch.setattr(dc.platform_update, "container_replacement_blockers", lambda: [])

  status = await dc.request_rebuild()

  assert status["state"] == "queued"
  assert json.loads((inbox / "request.json").read_text()) == {
    "version": 1, "expected_sha": "c" * 40,
  }


@pytest.mark.asyncio
async def test_queued_request_masks_the_previous_terminal_status(tmp_path, monkeypatch):
  control, inbox = _install_control(tmp_path, monkeypatch)
  (control / "status.json").write_text(
    '{"state":"succeeded","operation_id":"old",'
    '"handoff":"external-cutover-v1"}', encoding="utf-8",
  )
  (inbox / "request.json").write_text(json.dumps({
    "version": 1, "expected_sha": "e" * 40,
  }), encoding="utf-8")
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")

  status = await dc.read_rebuild_status()

  assert status["state"] == "queued"
  assert status["expected_sha"] == "e" * 40


@pytest.mark.asyncio
async def test_request_refuses_local_runtime_changes(tmp_path, monkeypatch):
  _control, inbox = _install_control(tmp_path, monkeypatch)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "d" * 40)
  monkeypatch.setattr(
    dc.platform_update, "container_replacement_blockers", lambda: ["Dockerfile"],
  )

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()

  assert exc.value.code == "local_runtime_changes"
  assert not (inbox / "request.json").exists()


@pytest.mark.asyncio
async def test_request_refuses_unknown_target(tmp_path, monkeypatch):
  _install_control(tmp_path, monkeypatch)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: None)

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  assert exc.value.code == "target_unavailable"


@pytest.mark.asyncio
async def test_status_reports_unconfigured_self_host(monkeypatch):
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(dc, "_configured", lambda: False)

  status = await dc.read_rebuild_status()

  assert status["supported"] is False
  assert status["code"] == "not_configured"


@pytest.mark.asyncio
async def test_legacy_host_helper_is_visible_but_cannot_queue_replacement(
  tmp_path, monkeypatch,
):
  control, inbox = _install_control(tmp_path, monkeypatch)
  (control / "status.json").write_text('{"state":"idle"}', encoding="utf-8")
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")

  status = await dc.read_rebuild_status()

  assert status["supported"] is False
  assert status["code"] == "controller_upgrade_required"
  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  assert exc.value.code == "controller_upgrade_required"
  assert not (inbox / "request.json").exists()


def test_prepare_path_requires_matching_root_owned_operation(tmp_path, monkeypatch):
  control, _inbox = _install_control(tmp_path, monkeypatch)
  operation = "a" * 32
  (control / "status.json").write_text(json.dumps({
    "state": "preparing", "operation_id": operation,
  }), encoding="utf-8")

  assert dc.replacement_ready_path(operation) == \
    control / "inbox" / f"ready-{operation}"
  with pytest.raises(dc.DeploymentControlError):
    dc.replacement_ready_path("b" * 32)
