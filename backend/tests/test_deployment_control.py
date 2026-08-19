"""Provider-neutral rebuild selection and response-contract tests."""

from __future__ import annotations

import pytest

from app import deployment_control as dc


def test_normalize_managed_running_status():
  status = dc._normalize_status(
    {
      "job_id": "rbl_123",
      "state": "DEPLOYING",
      "expected_sha": "a" * 40,
      "updated_at": "2026-08-18T22:00:00Z",
    },
    deployment="railway",
  )

  assert status == {
    "supported": True,
    "deployment": "railway",
    "operation_id": "rbl_123",
    "state": "replacing",
    "expected_sha": "a" * 40,
    "code": None,
    "message": None,
    "updated_at": "2026-08-18T22:00:00Z",
  }


def test_normalize_rejects_unknown_controller_state():
  with pytest.raises(dc.DeploymentControlError) as exc:
    dc._normalize_status({"state": "doing_magic"}, deployment="self_hosted")
  assert exc.value.code == "controller_invalid_response"


@pytest.mark.asyncio
async def test_request_rebuild_selects_railway_and_derives_target(monkeypatch):
  calls = []

  async def managed(operation, expected_sha=None):
    calls.append((operation, expected_sha))
    return {"job_id": "rbl_1", "state": "running"}

  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "b" * 40)
  monkeypatch.setattr(dc, "_managed_call", managed)

  status = await dc.request_rebuild()

  assert calls == [("rebuild", "b" * 40)]
  assert status["deployment"] == "railway"
  assert status["state"] == "preparing"
  assert status["expected_sha"] == "b" * 40


@pytest.mark.asyncio
async def test_request_rebuild_selects_self_host_and_derives_target(monkeypatch):
  calls = []

  async def self_host(operation, expected_sha=None):
    calls.append((operation, expected_sha))
    return {"operation_id": "host_1", "state": "queued"}

  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda: "self_hosted",
  )
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "c" * 40)
  monkeypatch.setattr(dc, "_self_host_call", self_host)

  status = await dc.request_rebuild()

  assert calls == [("rebuild", "c" * 40)]
  assert status["deployment"] == "self_hosted"
  assert status["operation_id"] == "host_1"


@pytest.mark.asyncio
async def test_request_rebuild_refuses_unknown_target_before_adapter(monkeypatch):
  called = False

  async def self_host(*_args, **_kwargs):
    nonlocal called
    called = True
    return {}

  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda: "self_hosted",
  )
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: None)
  monkeypatch.setattr(dc, "_self_host_call", self_host)

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()

  assert exc.value.code == "target_unavailable"
  assert called is False


@pytest.mark.asyncio
async def test_status_reports_unconfigured_self_host_without_ssh(monkeypatch):
  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda: "self_hosted",
  )
  monkeypatch.setattr(dc, "_self_host_connection", lambda: None)

  status = await dc.read_rebuild_status()

  assert status["supported"] is False
  assert status["state"] == "idle"
  assert status["code"] == "not_configured"


@pytest.mark.asyncio
async def test_active_duplicate_is_returned_not_reinterpreted(monkeypatch):
  async def managed(operation, expected_sha=None):
    assert operation == "rebuild"
    return {
      "job_id": "same-job",
      "state": "waiting",
      "expected_sha": expected_sha,
    }

  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "d" * 40)
  monkeypatch.setattr(dc, "_managed_call", managed)

  first = await dc.request_rebuild()
  second = await dc.request_rebuild()

  assert first["operation_id"] == second["operation_id"] == "same-job"
  assert first["state"] == second["state"] == "queued"
