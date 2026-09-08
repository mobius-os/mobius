"""Container replacement request and status boundary tests."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from types import SimpleNamespace
import urllib.error

import pytest

from app import deployment_control as dc


_TEST_DIGEST = "sha256:" + "d" * 64


def _install_latest_release(monkeypatch, sha="a" * 40):
  async def latest():
    return {
      "build_sha": sha,
      "image_digest": _TEST_DIGEST,
      "image_ref": f"ghcr.io/mobius-os/mobius:sha-{sha}@{_TEST_DIGEST}",
    }

  monkeypatch.setattr(dc, "latest_official_release", latest)
  monkeypatch.setattr(
    dc.platform_update,
    "official_image_rebuild_blockers",
    lambda *_args, **_kwargs: [],
  )


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
async def test_latest_official_release_requires_verified_sha_and_digest(
  monkeypatch,
):
  monkeypatch.setattr(dc, "_managed_request", lambda *_args: {
    "build_sha": "b" * 40,
    "image_digest": "sha256:" + "c" * 64,
    "image_ref": "immutable-ref",
  })

  release = await dc.latest_official_release()

  assert release == {
    "build_sha": "b" * 40,
    "image_digest": "sha256:" + "c" * 64,
    "image_ref": "immutable-ref",
  }

  monkeypatch.setattr(dc, "_managed_request", lambda *_args: {
    "build_sha": "b" * 40,
    "image_digest": "mutable",
  })
  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.latest_official_release()
  assert exc.value.code == "controller_invalid_response"


def test_managed_status_preserves_controller_failure_detail():
  status = dc._normalize_managed_status({
    "state": "rolled_back",
    "expected_sha": "a" * 40,
    "message": "Previous deployment restored.",
    "error": "Public URL served the wrong build SHA.",
  })

  assert status["state"] == "rolled_back"
  assert status["error"] == "Public URL served the wrong build SHA."
  assert status["release_source"] == "latest_ghcr"


@pytest.mark.asyncio
async def test_reviewed_rebuild_uses_exact_plan_target_and_digest(monkeypatch):
  target = "b" * 40
  digest = "sha256:" + "c" * 64
  captured = {}

  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda: "railway",
  )
  monkeypatch.setattr(
    dc.platform_update,
    "reviewed_container_rebuild_plan",
    lambda **plan: {
      "target_sha": plan["target_sha"],
      "image_digest": plan["image_digest"],
      "local_base_sha": plan["current_sha"],
      "activation": {
        "level": "image_rebuild", "deployment": "railway",
        "reasons": [], "guidance": [],
      },
      "blockers": [],
    },
  )

  async def start(expected_sha, expected_digest, *, final_check):
    captured.update(sha=expected_sha, digest=expected_digest)
    final_check()
    return {"state": "queued"}

  monkeypatch.setattr(dc, "_request_managed_rebuild", start)

  status = await dc.request_reviewed_rebuild(
    db=None,
    plan_id="a" * 64,
    current_sha="1" * 40,
    target_sha=target,
    image_digest=digest,
  )

  assert status["state"] == "queued"
  assert captured == {"sha": target, "digest": digest}


@pytest.mark.asyncio
async def test_reviewed_immutable_plan_does_not_reconsult_moving_ghcr_main(
  monkeypatch,
):
  target = "b" * 40
  digest = "sha256:" + "c" * 64
  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda: "railway",
  )

  async def moving_main_must_not_be_read():
    raise AssertionError("immutable review reconsulted the moving main tag")

  monkeypatch.setattr(dc, "latest_official_release", moving_main_must_not_be_read)
  monkeypatch.setattr(
    dc.platform_update,
    "reviewed_container_rebuild_plan",
    lambda **plan: {
      "target_sha": plan["target_sha"],
      "image_digest": plan["image_digest"],
      "local_base_sha": plan["current_sha"],
      "activation": {
        "level": "image_rebuild", "deployment": "railway",
        "reasons": [], "guidance": [],
      },
      "blockers": [],
    },
  )

  async def start(expected_sha, expected_digest, *, final_check):
    final_check()
    return {
      "state": "queued", "expected_sha": expected_sha,
      "image_digest": expected_digest,
    }

  monkeypatch.setattr(dc, "_request_managed_rebuild", start)

  result = await dc.request_reviewed_rebuild(
    db=None,
    plan_id="a" * 64,
    current_sha="1" * 40,
    target_sha=target,
    image_digest=digest,
  )

  assert result["expected_sha"] == target
  assert result["image_digest"] == digest


@pytest.mark.asyncio
async def test_self_hosted_reviewed_rebuild_applies_then_queues_host_rebuild(
  monkeypatch,
):
  # Self-hosted has no GHCR digest: the reviewed image update applies the source
  # in place (advancing the upstream marker to the target), then queues the host
  # rebuild for the matching sha-<target> image — one confirmation.
  target = "b" * 40
  calls = []

  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda *a, **k: "self_hosted",
  )

  async def ready_status():
    return {"supported": True, "state": "idle"}

  monkeypatch.setattr(dc, "read_rebuild_status", ready_status)

  async def fake_apply(db, **plan):
    calls.append(("apply", plan))
    return {
      "state": dc.platform_update.PlatformUpdateState.ACTIVATION_NEEDED.value,
      "activation": {"level": "image_rebuild", "deployment": "self_hosted"},
    }

  monkeypatch.setattr(dc.platform_update, "apply_platform_update", fake_apply)

  async def fake_request_rebuild():
    calls.append(("rebuild", None))
    return {"state": "queued", "expected_sha": target, "supported": True}

  monkeypatch.setattr(dc, "request_rebuild", fake_request_rebuild)

  result = await dc.request_reviewed_rebuild(
    db=SimpleNamespace(),
    plan_id="a" * 64,
    current_sha="1" * 40,
    target_sha=target,
    image_digest=None,
  )

  assert result["state"] == "queued"
  assert result["expected_sha"] == target
  # Apply ran with the reviewed plan, then the host rebuild was queued — in order.
  assert [name for name, _ in calls] == ["apply", "rebuild"]
  assert calls[0][1]["target_sha"] == target


@pytest.mark.asyncio
async def test_self_hosted_reviewed_rebuild_surfaces_apply_conflict(monkeypatch):
  # If applying the source hits a conflict, nothing is rebuilt; the apply result
  # is returned so the review sheet renders its conflict surface.
  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda *a, **k: "self_hosted",
  )

  async def ready_status():
    return {"supported": True, "state": "idle"}

  monkeypatch.setattr(dc, "read_rebuild_status", ready_status)

  async def fake_apply(db, **plan):
    return {
      "state": dc.platform_update.PlatformUpdateState.CONFLICT.value,
      "chat_id": "chat-1",
    }

  monkeypatch.setattr(dc.platform_update, "apply_platform_update", fake_apply)

  async def must_not_rebuild():
    raise AssertionError("a conflicted apply must not queue a rebuild")

  monkeypatch.setattr(dc, "request_rebuild", must_not_rebuild)

  result = await dc.request_reviewed_rebuild(
    db=SimpleNamespace(),
    plan_id="a" * 64,
    current_sha="1" * 40,
    target_sha="b" * 40,
    image_digest=None,
  )

  assert result["state"] == "conflict"


@pytest.mark.asyncio
async def test_self_hosted_reviewed_rebuild_fails_closed_when_host_not_ready(
  monkeypatch,
):
  # The host readiness check runs BEFORE any source is applied, so an unconfigured
  # host never leaves a half-applied update behind.
  monkeypatch.setattr(
    dc.platform_activation, "deployment_kind", lambda *a, **k: "self_hosted",
  )

  async def unconfigured_status():
    return {
      "supported": False,
      "code": "not_configured",
      "message": "Finish the one-time host setup.",
    }

  monkeypatch.setattr(dc, "read_rebuild_status", unconfigured_status)

  async def must_not_apply(db, **plan):
    raise AssertionError("source was applied before verifying host readiness")

  monkeypatch.setattr(dc.platform_update, "apply_platform_update", must_not_apply)

  with pytest.raises(dc.DeploymentControlError) as excinfo:
    await dc.request_reviewed_rebuild(
      db=SimpleNamespace(),
      plan_id="a" * 64,
      current_sha="1" * 40,
      target_sha="b" * 40,
      image_digest=None,
    )

  assert excinfo.value.code == "not_configured"


@pytest.mark.asyncio
async def test_managed_prepare_rejects_mismatched_release_echo(
  tmp_path, monkeypatch,
):
  _install_managed_cutover_marker(tmp_path, monkeypatch)
  monkeypatch.setattr(
    dc,
    "get_settings",
    lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )

  def managed_request(_method, suffix, _payload=None):
    assert suffix == "prepare"
    return {
      "state": "awaiting_handoff",
      "operation_id": "replace_12345678",
      "handoff_nonce": "nonce-secret",
      "expected_sha": "a" * 40,
      "image_digest": "sha256:" + "e" * 64,
    }

  monkeypatch.setattr(dc, "_managed_request", managed_request)

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc._request_managed_rebuild("a" * 40, _TEST_DIGEST)

  assert exc.value.code == "controller_invalid_response"


@pytest.mark.asyncio
async def test_managed_start_rejects_mismatched_release_echo(
  tmp_path, monkeypatch,
):
  from app import restart_ledger, restart_util

  _install_managed_cutover_marker(tmp_path, monkeypatch)
  monkeypatch.setattr(
    dc,
    "get_settings",
    lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )

  def managed_request(_method, suffix, _payload=None):
    if suffix == "prepare":
      return {
        "state": "awaiting_handoff",
        "operation_id": "replace_12345678",
        "handoff_nonce": "nonce-secret",
        "expected_sha": "a" * 40,
        "image_digest": _TEST_DIGEST,
      }
    return {
      "state": "queued",
      "operation_id": "replace_12345678",
      "expected_sha": "b" * 40,
      "image_digest": _TEST_DIGEST,
    }

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: True)
  monkeypatch.setattr(
    restart_util,
    "prepare_managed_container_cutover",
    lambda _id: asyncio.sleep(0),
  )

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc._request_managed_rebuild("a" * 40, _TEST_DIGEST)

  assert exc.value.code == "controller_invalid_response"


@pytest.mark.asyncio
async def test_managed_final_validation_runs_after_drain_and_before_start(
  tmp_path, monkeypatch,
):
  from app import restart_ledger, restart_util

  _install_managed_cutover_marker(tmp_path, monkeypatch)
  monkeypatch.setattr(
    dc,
    "get_settings",
    lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )
  events = []

  def managed_request(_method, suffix, _payload=None):
    events.append(suffix)
    if suffix == "prepare":
      return {
        "state": "awaiting_handoff",
        "operation_id": "replace_12345678",
        "handoff_nonce": "nonce-secret",
        "expected_sha": "a" * 40,
        "image_digest": _TEST_DIGEST,
      }
    return {
      "state": "queued",
      "operation_id": "replace_12345678",
      "expected_sha": "a" * 40,
      "image_digest": _TEST_DIGEST,
    }

  async def drain(_operation_id):
    events.append("drain")

  def final_check():
    events.append("final_check")

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: True)
  monkeypatch.setattr(restart_util, "prepare_managed_container_cutover", drain)

  result = await dc._request_managed_rebuild(
    "a" * 40,
    _TEST_DIGEST,
    final_check=final_check,
  )

  assert result["state"] == "queued"
  assert events == ["prepare", "drain", "final_check", "start"]


@pytest.mark.asyncio
async def test_legacy_railway_image_offers_managed_bootstrap(tmp_path, monkeypatch):
  from app import chat

  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})())
  _install_latest_release(monkeypatch)
  calls = []

  def managed_request(method, suffix, payload=None):
    calls.append((method, suffix, payload))
    if method == "GET":
      return {"mode": "handoff", "state": "idle"}
    if suffix == "bootstrap/prepare":
      return {
        "mode": "bootstrap", "state": "awaiting_bootstrap",
        "operation_id": "bootstrap_123", "handoff_nonce": "nonce",
        "expected_sha": "a" * 40, "image_digest": _TEST_DIGEST,
      }
    return {
      "mode": "bootstrap", "state": "queued",
      "operation_id": "bootstrap_123", "expected_sha": "a" * 40,
      "image_digest": _TEST_DIGEST,
    }

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  began = []
  monkeypatch.setattr(
    chat, "begin_idle_drain", lambda: began.append(True) or True,
  )

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
    ("POST", "bootstrap/prepare", {
      "expected_sha": "a" * 40, "expected_digest": _TEST_DIGEST,
    }),
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

  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})())
  _install_latest_release(monkeypatch)
  calls = []

  def managed_request(method, suffix, payload=None):
    calls.append((method, suffix, payload))
    return {
      "mode": "bootstrap", "state": "awaiting_bootstrap",
      "operation_id": "bootstrap_123", "handoff_nonce": "nonce",
      "expected_sha": "a" * 40, "image_digest": _TEST_DIGEST,
    }

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  began = []
  monkeypatch.setattr(
    chat, "begin_idle_drain", lambda: began.append(True) or False,
  )

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()

  assert exc.value.code == "active_chats"
  assert began == [True]
  assert [suffix for _method, suffix, _payload in calls] == ["bootstrap/prepare"]


@pytest.mark.asyncio
async def test_legacy_bootstrap_reopens_admission_only_after_definitive_rejection(
  monkeypatch,
):
  from app import chat

  began = []
  cancelled = []
  monkeypatch.setattr(
    chat, "begin_idle_drain", lambda: began.append(True) or True,
  )
  monkeypatch.setattr(chat, "cancel_idle_drain", lambda: cancelled.append(True))

  def managed_request(method, suffix, _payload=None):
    if suffix == "bootstrap/prepare":
      return {
        "operation_id": "bootstrap_123", "handoff_nonce": "nonce",
        "expected_sha": "a" * 40, "image_digest": _TEST_DIGEST,
      }
    raise dc.DeploymentControlError(
      "controller_rejected", "The managed upgrade was rejected.", status_code=409,
    )

  monkeypatch.setattr(dc, "_managed_request", managed_request)

  with pytest.raises(dc.DeploymentControlError):
    await dc._request_managed_bootstrap("a" * 40, _TEST_DIGEST)

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
  _install_latest_release(monkeypatch)
  request_thread = threading.get_ident()
  blocker_threads = []

  def blockers(*_args, **_kwargs):
    blocker_threads.append(threading.get_ident())
    return []

  monkeypatch.setattr(
    dc.platform_update, "official_image_rebuild_blockers", blockers,
  )
  calls = []

  def managed_request(method, suffix, payload=None):
    calls.append((method, suffix, payload))
    if suffix == "prepare":
      return {
        "operation_id": "replace_12345678", "handoff_nonce": "nonce-secret",
        "state": "awaiting_handoff", "expected_sha": "a" * 40,
        "image_digest": _TEST_DIGEST,
      }
    return {
      "operation_id": "replace_12345678", "state": "queued",
      "expected_sha": "a" * 40, "image_digest": _TEST_DIGEST,
      "message": "Replacement queued.",
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
  assert blocker_threads
  assert all(thread_id != request_thread for thread_id in blocker_threads)
  assert calls == [
    ("POST", "prepare", {
      "expected_sha": "a" * 40, "expected_digest": _TEST_DIGEST,
    }),
    ("POST", "start", {
      "operation_id": "replace_12345678", "handoff_nonce": "nonce-secret",
    }),
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("error_code", "restart_count", "reconcile_count"),
  [("controller_rejected", 1, 0), ("controller_unavailable", 0, 1),
   ("controller_invalid_response", 0, 1)],
)
async def test_managed_start_failure_restarts_only_after_definitive_rejection(
  tmp_path, monkeypatch, error_code, restart_count, reconcile_count,
):
  from app import restart_ledger, restart_util

  _install_control(tmp_path, monkeypatch)
  _install_managed_cutover_marker(tmp_path, monkeypatch)
  settings = type("S", (), {"data_dir": str(tmp_path)})()
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "railway")
  monkeypatch.setattr(dc, "get_settings", lambda: settings)
  _install_latest_release(monkeypatch)

  def managed_request(method, suffix, payload=None):
    if suffix == "prepare":
      return {
        "state": "prepared",
        "operation_id": "replace_12345678",
        "handoff_nonce": "nonce-secret",
        "expected_sha": "a" * 40,
        "image_digest": _TEST_DIGEST,
      }
    raise dc.DeploymentControlError(error_code, "start failed")

  restarts = []
  reconciliations = []

  async def restart():
    restarts.append(True)

  monkeypatch.setattr(dc, "_managed_request", managed_request)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "request_managed_cutover", lambda **_kw: None)
  monkeypatch.setattr(restart_ledger, "authorized_cutover_challenge", lambda _id: True)
  monkeypatch.setattr(restart_ledger, "accepted_cutover_receipt", lambda _id: True)
  monkeypatch.setattr(restart_util, "prepare_managed_container_cutover", lambda _id: asyncio.sleep(0))
  monkeypatch.setattr(restart_util, "restart_this_worker", restart)
  monkeypatch.setattr(
    dc,
    "_schedule_ambiguous_start_reconciliation",
    lambda operation_id, nonce, _restart: reconciliations.append(
      (operation_id, nonce),
    ),
  )

  with pytest.raises(dc.DeploymentControlError) as exc:
    await dc.request_rebuild()
  await asyncio.sleep(0)

  assert exc.value.code == error_code
  assert len(restarts) == restart_count
  assert len(reconciliations) == reconcile_count
  if reconcile_count:
    assert reconciliations == [("replace_12345678", "nonce-secret")]


@pytest.mark.asyncio
async def test_ambiguous_start_recovers_only_after_atomic_cancel_wins(
  monkeypatch,
):
  requests = []

  def managed_request(method, suffix, payload=None):
    requests.append((method, suffix, payload))
    return {
      "operation_id": "replace_12345678",
      "state": "failed",
      "cancelled": True,
    }

  monkeypatch.setattr(
    dc,
    "_managed_request",
    managed_request,
  )
  async def no_sleep(_seconds):
    return None

  monkeypatch.setattr(dc.asyncio, "sleep", no_sleep)
  restarts = []

  async def restart():
    restarts.append(True)

  await dc._reconcile_ambiguous_managed_start(
    "replace_12345678", "nonce-secret", restart,
  )

  assert restarts == [True]
  assert requests == [(
    "POST",
    "cancel",
    {
      "operation_id": "replace_12345678",
      "handoff_nonce": "nonce-secret",
    },
  )]


@pytest.mark.asyncio
async def test_ambiguous_start_leaves_accepted_operation_to_account(
  monkeypatch,
):
  monkeypatch.setattr(
    dc,
    "_managed_request",
    lambda method, suffix, payload=None: {
      "operation_id": "replace_12345678", "state": "queued",
      "cancelled": False,
    },
  )
  async def no_sleep(_seconds):
    return None

  monkeypatch.setattr(dc.asyncio, "sleep", no_sleep)
  restarts = []

  async def restart():
    restarts.append(True)

  await dc._reconcile_ambiguous_managed_start(
    "replace_12345678", "nonce-secret", restart,
  )

  assert restarts == []


@pytest.mark.asyncio
async def test_ambiguous_start_retries_stale_or_malformed_cancel_responses(
  monkeypatch,
):
  responses = iter([
    {"operation_id": "replace_stale", "cancelled": True},
    {"operation_id": "replace_12345678", "state": "failed"},
    {"operation_id": "replace_12345678", "cancelled": True},
  ])
  requests = []

  def managed_request(method, suffix, payload=None):
    requests.append((method, suffix, payload))
    return next(responses)

  monkeypatch.setattr(dc, "_managed_request", managed_request)

  async def no_sleep(_seconds):
    return None

  monkeypatch.setattr(dc.asyncio, "sleep", no_sleep)
  restarts = []

  async def restart():
    restarts.append(True)

  await dc._reconcile_ambiguous_managed_start(
    "replace_12345678", "nonce-secret", restart,
  )

  assert len(requests) == 3
  assert restarts == [True]


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
  _install_latest_release(monkeypatch)

  requests = []

  def managed_request(method, suffix, payload=None):
    requests.append((method, suffix, payload))
    return {
      "state": "awaiting_handoff",
      "operation_id": "replace_12345678",
      "handoff_nonce": "nonce-secret",
      "expected_sha": "a" * 40,
      "image_digest": _TEST_DIGEST,
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
    "POST", "prepare", {
      "expected_sha": "a" * 40, "expected_digest": _TEST_DIGEST,
    },
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
  _install_latest_release(monkeypatch)

  def managed_request(method, suffix, _payload=None):
    if suffix == "prepare":
      return {
        "state": "awaiting_handoff",
        "operation_id": "replace_12345678",
        "handoff_nonce": "nonce-secret",
        "expected_sha": "a" * 40,
        "image_digest": _TEST_DIGEST,
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
