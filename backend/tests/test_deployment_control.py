"""Container replacement request and status boundary tests."""

from __future__ import annotations

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
async def test_railway_is_explicitly_deferred(monkeypatch):
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")

  status = await dc.read_rebuild_status()
  assert status["supported"] is False
  assert status["code"] == "not_supported"

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  assert exc.value.code == "not_supported"


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
