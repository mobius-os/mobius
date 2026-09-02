"""Container replacement request and status boundary tests."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

from app import deployment_control as dc


def _install_control(tmp_path, monkeypatch):
  control = tmp_path / "mobius-rebuild"
  inbox = control / "inbox"
  inbox.mkdir(parents=True)
  (control / "status.json").write_text(
    '{"state":"idle","handoff":"external-cutover-v1",'
    '"runtime_overlay":"active-runtime-v1"}',
    encoding="utf-8",
  )
  monkeypatch.setattr(dc, "_control_dir", lambda: control)
  return control, inbox


def _install_managed_cutover_marker(tmp_path, monkeypatch, boot_id="boot-test"):
  run_dir = tmp_path / "run"
  run_dir.mkdir(exist_ok=True)
  (run_dir / "managed-cutover-ready").write_text(
    f"{boot_id}\n", encoding="utf-8",
  )
  monkeypatch.setenv("MOBIUS_BOOT_ID", boot_id)


def test_managed_request_identifies_the_machine_client(monkeypatch):
  settings = SimpleNamespace(
    mobius_account_origin="https://account.example",
    mobius_sso_enabled=True,
    mobius_sso_client_secret="secret",
    mobius_sso_instance_id="instance",
  )
  captured = []

  def open_request(request, **_kwargs):
    captured.append(request)
    return io.BytesIO(b"{}")

  opener = SimpleNamespace(open=open_request)
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc.urllib.request, "build_opener", lambda *_args: opener)

  assert dc._managed_request("GET", "status") == {}
  headers = {name.lower(): value for name, value in captured[0].header_items()}
  assert headers["user-agent"] == "mobius-managed-deployment/1"
  assert headers["authorization"] == "Bearer secret"
  assert headers["x-mobius-instance-id"] == "instance"


@pytest.mark.parametrize("status", [400, 409])
def test_managed_request_recognizes_only_structured_client_rejections(
  monkeypatch, status,
):
  settings = SimpleNamespace(
    mobius_account_origin="https://account.example",
    mobius_sso_enabled=True,
    mobius_sso_client_secret="secret",
    mobius_sso_instance_id="instance",
  )
  response = urllib.error.HTTPError(
    "https://account.example/start", status, "rejected", {},
    io.BytesIO(b'{"detail":"Replacement handoff rejected."}'),
  )

  def open_request(*_args, **_kwargs):
    raise response

  opener = SimpleNamespace(open=open_request)
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc.urllib.request, "build_opener", lambda *_args: opener)

  with pytest.raises(dc.DeploymentControlError) as exc:
    dc._managed_request("POST", "start", {"operation_id": "replacement"})

  assert exc.value.code == "controller_rejected"
  assert exc.value.status_code == 409


@pytest.mark.parametrize(
  ("status", "body"),
  [
    (409, b"not-json"),
    (502, b'{"detail":"Replacement could not start."}'),
    (503, b'{"detail":"Replacement could not start."}'),
    (504, b'{"detail":"Replacement could not start."}'),
  ],
)
def test_managed_request_keeps_ambiguous_http_failures_ambiguous(
  monkeypatch, status, body,
):
  settings = SimpleNamespace(
    mobius_account_origin="https://account.example",
    mobius_sso_enabled=True,
    mobius_sso_client_secret="secret",
    mobius_sso_instance_id="instance",
  )
  response = urllib.error.HTTPError(
    "https://account.example/start", status, "gateway failure", {},
    io.BytesIO(body),
  )

  def open_request(*_args, **_kwargs):
    raise response

  opener = SimpleNamespace(open=open_request)
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc.urllib.request, "build_opener", lambda *_args: opener)

  with pytest.raises(dc.DeploymentControlError) as exc:
    dc._managed_request("POST", "start", {"operation_id": "replacement"})

  assert exc.value.code == "controller_unavailable"
  assert exc.value.status_code == 503


def test_normalize_rejects_unknown_controller_state():
  with pytest.raises(dc.DeploymentControlError) as exc:
    dc._normalize_status({"state": "doing_magic"})
  assert exc.value.code == "controller_invalid_response"


@pytest.mark.asyncio
async def test_legacy_railway_image_offers_managed_bootstrap(tmp_path, monkeypatch):
  from app import chat
  from app.runner_registry import registry

  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})())
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(
    dc.platform_update,
    "container_replacement_blockers",
    lambda *_args, **_kwargs: [],
  )
  calls = []

  def managed_request(method, suffix, payload=None):
    calls.append((method, suffix, payload))
    if method == "GET":
      return {"mode": "handoff", "state": "idle"}
    if suffix == "bootstrap/prepare":
      return {
        "mode": "bootstrap", "state": "awaiting_bootstrap",
        "operation_id": "bootstrap_123", "handoff_nonce": "nonce",
      }
    return {
      "mode": "bootstrap", "state": "queued",
      "operation_id": "bootstrap_123", "expected_sha": "a" * 40,
    }

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: set())
  began = []
  monkeypatch.setattr(chat, "begin_drain", lambda: began.append(True))

  status = await dc.read_rebuild_status()
  assert status["supported"] is False
  assert status["bootstrap_available"] is True
  assert status["code"] == "controller_upgrade_required"

  started = await dc.request_rebuild()
  assert started["state"] == "queued"
  assert started["bootstrap_available"] is True
  assert began == [True]
  assert calls == [
    ("GET", "status", None),
    ("POST", "bootstrap/prepare", {"expected_sha": "a" * 40}),
    ("POST", "bootstrap/start", {
      "operation_id": "bootstrap_123", "handoff_nonce": "nonce",
    }),
  ]


@pytest.mark.asyncio
async def test_stale_managed_marker_cannot_impersonate_current_boot(
  tmp_path, monkeypatch,
):
  _install_managed_cutover_marker(tmp_path, monkeypatch, boot_id="previous-boot")
  monkeypatch.setenv("MOBIUS_BOOT_ID", "current-boot")
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(
    dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )
  monkeypatch.setattr(
    dc, "_managed_request", lambda method, suffix: {
      "mode": "handoff", "state": "idle",
    },
  )

  status = await dc.read_rebuild_status()

  assert dc.managed_cutover_ready() is False
  assert status["supported"] is False
  assert status["bootstrap_available"] is True
  assert status["code"] == "controller_upgrade_required"


def test_managed_marker_belongs_only_to_matching_boot(tmp_path, monkeypatch):
  _install_managed_cutover_marker(tmp_path, monkeypatch, boot_id="current-boot")
  monkeypatch.setattr(
    dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )

  assert dc.managed_cutover_ready() is True


@pytest.mark.asyncio
async def test_legacy_railway_bootstrap_requires_linked_account(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(
    dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )

  def unlinked(*_args, **_kwargs):
    raise dc.DeploymentControlError("not_configured", "not linked", status_code=409)

  monkeypatch.setattr(dc, "_managed_request", unlinked)

  status = await dc.read_rebuild_status()

  assert status["supported"] is False
  assert status["bootstrap_available"] is False
  assert status["code"] == "not_configured"


@pytest.mark.asyncio
async def test_legacy_railway_bootstrap_does_not_mask_controller_failure(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(
    dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )

  def unavailable(*_args, **_kwargs):
    raise dc.DeploymentControlError(
      "controller_unavailable", "The Möbius account service is unavailable.",
    )

  monkeypatch.setattr(dc, "_managed_request", unavailable)

  status = await dc.read_rebuild_status()

  assert status["supported"] is False
  assert status["bootstrap_available"] is False
  assert status["code"] == "controller_unavailable"
  assert status["message"] == "The Möbius account service is unavailable."


@pytest.mark.asyncio
async def test_legacy_railway_bootstrap_waits_for_idle_chats(tmp_path, monkeypatch):
  from app import chat
  from app.runner_registry import registry

  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})())
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(
    dc.platform_update,
    "container_replacement_blockers",
    lambda *_args, **_kwargs: [],
  )
  calls = []

  def managed_request(method, suffix, payload=None):
    calls.append((method, suffix, payload))
    return {
      "mode": "bootstrap", "state": "awaiting_bootstrap",
      "operation_id": "bootstrap_123", "handoff_nonce": "nonce",
    }

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: {"active-chat"})
  began = []
  monkeypatch.setattr(chat, "begin_drain", lambda: began.append(True))

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()

  assert exc.value.code == "active_chats"
  assert began == []
  assert [suffix for _method, suffix, _payload in calls] == ["bootstrap/prepare"]


@pytest.mark.asyncio
async def test_legacy_bootstrap_reopens_admission_only_after_definitive_rejection(
  monkeypatch,
):
  from app import chat
  from app.runner_registry import registry

  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: set())
  began = []
  cancelled = []
  monkeypatch.setattr(chat, "begin_drain", lambda: began.append(True))
  monkeypatch.setattr(chat, "cancel_idle_drain", lambda: cancelled.append(True))

  def managed_request(method, suffix, _payload=None):
    if suffix == "bootstrap/prepare":
      return {"operation_id": "bootstrap_123", "handoff_nonce": "nonce"}
    raise dc.DeploymentControlError(
      "controller_rejected", "The managed upgrade was rejected.", status_code=409,
    )

  monkeypatch.setattr(dc, "_managed_request", managed_request)

  with pytest.raises(dc.DeploymentControlError):
    await dc._request_managed_bootstrap("a" * 40)

  assert began == [True]
  assert cancelled == [True]


@pytest.mark.asyncio
async def test_railway_status_uses_account_service(tmp_path, monkeypatch):
  _install_managed_cutover_marker(tmp_path, monkeypatch)
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

  _install_managed_cutover_marker(tmp_path, monkeypatch)
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  blocker_calls = []

  def blockers(expected, *, preserve_active_runtime):
    blocker_calls.append((expected, preserve_active_runtime))
    return []

  monkeypatch.setattr(
    dc.platform_update,
    "container_replacement_blockers",
    blockers,
  )
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
  assert blocker_calls == [("a" * 40, False)]
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
  _install_managed_cutover_marker(tmp_path, monkeypatch)
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(
    dc.platform_update,
    "container_replacement_blockers",
    lambda *_args, **_kwargs: [],
  )

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
async def test_pre_start_receipt_timeout_recovers_the_drained_worker(
  tmp_path, monkeypatch,
):
  from app import restart_ledger, restart_util

  _install_control(tmp_path, monkeypatch)
  _install_managed_cutover_marker(tmp_path, monkeypatch)
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(
    dc.platform_update,
    "container_replacement_blockers",
    lambda *_args, **_kwargs: [],
  )

  requests = []

  def managed_request(method, suffix, payload=None):
    requests.append((method, suffix, payload))
    return {
      "state": "awaiting_handoff",
      "operation_id": "replace_12345678",
      "handoff_nonce": "nonce-secret",
    }

  restarts = []

  async def restart():
    restarts.append(True)

  times = iter((0, 0, 16))
  monkeypatch.setattr(
    dc, "time", SimpleNamespace(monotonic=lambda: next(times)),
  )
  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: False)
  monkeypatch.setattr(
    restart_util, "prepare_managed_container_cutover", lambda _id: asyncio.sleep(0),
  )
  monkeypatch.setattr(restart_util, "restart_this_worker", restart)

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  await asyncio.sleep(0)

  assert exc.value.code == "controller_unavailable"
  assert requests == [(
    "POST", "prepare", {"expected_sha": "a" * 40},
  )]
  assert restarts == [True]


@pytest.mark.asyncio
async def test_definitive_rejection_owns_recovery_until_restart_settles(
  tmp_path, monkeypatch,
):
  from app import restart_ledger, restart_util

  _install_control(tmp_path, monkeypatch)
  _install_managed_cutover_marker(tmp_path, monkeypatch)
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "a" * 40)
  monkeypatch.setattr(
    dc.platform_update,
    "container_replacement_blockers",
    lambda *_args, **_kwargs: [],
  )

  def managed_request(method, suffix, _payload=None):
    if suffix == "prepare":
      return {
        "state": "awaiting_handoff",
        "operation_id": "replace_12345678",
        "handoff_nonce": "nonce-secret",
      }
    raise dc.DeploymentControlError("controller_rejected", "rejected")

  started = asyncio.Event()
  release = asyncio.Event()

  async def restart():
    started.set()
    await release.wait()

  owned = set()
  monkeypatch.setattr(dc, "_managed_recovery_tasks", owned)
  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: True)
  monkeypatch.setattr(
    restart_util, "prepare_managed_container_cutover", lambda _id: asyncio.sleep(0),
  )
  monkeypatch.setattr(restart_util, "restart_this_worker", restart)

  with pytest.raises(dc.DeploymentControlError, match="rejected"):
    await dc.request_rebuild()
  await started.wait()

  assert len(owned) == 1
  task = next(iter(owned))
  assert not task.done()

  release.set()
  await task
  await asyncio.sleep(0)
  assert owned == set()


@pytest.mark.asyncio
async def test_request_writes_only_the_derived_sha(tmp_path, monkeypatch):
  _control, inbox = _install_control(tmp_path, monkeypatch)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")
  monkeypatch.setattr(dc, "_expected_upstream_sha", lambda: "c" * 40)
  seen = {}

  def blockers(expected, *, preserve_active_runtime):
    seen["expected"] = expected
    seen["preserve_active_runtime"] = preserve_active_runtime
    return []

  monkeypatch.setattr(
    dc.platform_update, "container_replacement_blockers", blockers,
  )

  status = await dc.request_rebuild()

  assert status["state"] == "queued"
  assert json.loads((inbox / "request.json").read_text()) == {
    "version": 1, "expected_sha": "c" * 40,
  }
  assert seen == {
    "expected": "c" * 40,
    "preserve_active_runtime": True,
  }


@pytest.mark.asyncio
async def test_queued_request_masks_the_previous_terminal_status(tmp_path, monkeypatch):
  control, inbox = _install_control(tmp_path, monkeypatch)
  (control / "status.json").write_text(
    '{"state":"succeeded","operation_id":"old",'
    '"handoff":"external-cutover-v1",'
    '"runtime_overlay":"active-runtime-v1"}', encoding="utf-8",
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
    dc.platform_update,
    "container_replacement_blockers",
    lambda _expected, *, preserve_active_runtime: ["Dockerfile"],
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
