"""Owner-reviewable app capability contracts and install binding."""

import json

import pytest
from pathlib import Path
from unittest.mock import patch

from app import models
from app.app_capabilities import contract_and_digest
from app.app_capabilities import contract_with_runtime_capabilities
from app.app_capabilities import diff_contracts
from app.app_capabilities import normalize_runtime_capabilities
from app.config import get_settings
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from test_app_fixtures import create_local_app, write_local_source
from tests.test_apps_install import (  # noqa: F401
  JSX,
  _bypass_cron_scaffold,
  _fake_async_client,
  _stub_resolver_run_chat,
  bypass_url_validation,
)


def _manifest(**over):
  manifest = {
    "id": "memory",
    "name": "Memory",
    "version": "2.0.0",
    "description": "On-demand durable memory",
    "entry": "index.jsx",
    "source_files": ["memory-core.md"],
    "system_prompt": "memory-core.md",
    "permissions": {
      "chat_log_access": "summary",
      "shared_memory": "write",
    },
    "schedule": {
      "job": "memory-job.sh",
      "default": "30 5 * * *",
      "initialize_on_install": True,
    },
  }
  manifest.update(over)
  return manifest


def test_legacy_closed_defaults_do_not_manufacture_capability_changes():
  candidate, _digest = contract_and_digest(_manifest())
  legacy = json.loads(json.dumps(candidate))
  legacy["schema"] = 4
  legacy.pop("public")
  for field in (
    "github_connect",
    "connections_manage",
    "connect_manage",
    "identity_manage",
    "railway_manage",
    "helper_activity_read",
  ):
    legacy["data"].pop(field)

  assert diff_contracts(legacy, candidate) == {
    "unknown_previous": False,
    "added": [],
    "removed": [],
    "changed": [],
    "widens": False,
  }


@pytest.mark.parametrize(
  "legacy_public",
  [
    None,
    False,
    {"network": []},
    {"network": [], "storage": None},
    {"network": [], "storage": {}},
    {"network": [], "storage": {"read": False}},
  ],
)
def test_legacy_public_storage_closed_shapes_equal_current_default(legacy_public):
  candidate, _digest = contract_and_digest(_manifest())
  installed = json.loads(json.dumps(candidate))
  if legacy_public is None:
    installed.pop("public")
  else:
    installed["public"] = legacy_public

  assert diff_contracts(installed, candidate) == {
    "unknown_previous": False,
    "added": [],
    "removed": [],
    "changed": [],
    "widens": False,
  }


@pytest.mark.parametrize(
  ("path", "value"),
  [
    (("data", "connect_manage"), True),
    (("data", "identity_manage"), True),
    (("data", "railway_manage"), True),
    (("data", "helper_activity_read"), True),
    (("public", "storage", "read"), True),
    (("public", "storage", "write_prefix"), "public/submissions/"),
  ],
)
def test_legacy_receipt_still_reports_real_new_grants(path, value):
  installed, _digest = contract_and_digest(_manifest())
  installed["schema"] = 4
  installed.pop("public")
  for field in (
    "connect_manage", "identity_manage", "railway_manage", "helper_activity_read",
  ):
    installed["data"].pop(field)
  candidate = json.loads(json.dumps(installed))
  candidate["schema"] = 6
  cursor = candidate
  for part in path[:-1]:
    cursor = cursor.setdefault(part, {})
  cursor[path[-1]] = value

  report = diff_contracts(installed, candidate)

  assert report["unknown_previous"] is False
  assert ".".join(path) in [*report["added"], *report["changed"]]


def test_future_closed_default_does_not_manufacture_a_permission_change():
  candidate, _digest = contract_and_digest(_manifest())
  candidate["data"]["future_permission"] = False
  installed = json.loads(json.dumps(candidate))
  installed["data"].pop("future_permission")

  assert diff_contracts(installed, candidate) == {
    "unknown_previous": False,
    "added": [],
    "removed": [],
    "changed": [],
    "widens": False,
  }


def test_preview_returns_server_derived_contract_and_digest(
  client, auth, bypass_url_validation,
):
  base = "https://capability.test/memory/"
  manifest = _manifest()
  responses = {base + "mobius.json": (200, json.dumps(manifest).encode())}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    response = client.post(
      "/api/apps/preview",
      headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert response.status_code == 200, response.text
  body = response.json()
  contract, digest = contract_and_digest(manifest)
  assert body["capability_contract"] == contract
  assert body["capability_digest"] == digest
  assert body["capability_contract"]["agent"]["system_prompt"]["scope"] == (
    "chats_started_while_installed"
  )
  assert body["capability_contract"]["agent"]["system_prompt"]["activation"] == (
    "chat_start"
  )
  assert body["capability_contract"]["background"] == {
    "job": "memory-job.sh",
    "mode": "scheduled",
    "cron": "30 5 * * *",
    "user_configurable": False,
    "initialize_on_install": True,
  }
  assert body["capability_contract"]["schema"] == 6
  assert body["capability_contract"]["runtime"] == {}
  assert body["capability_contract"]["public"] == {
    "network": [],
    "storage": {"read": False, "write_prefix": None},
  }


@pytest.mark.parametrize("service_id", [None, "explicit-service"])
def test_service_is_an_explicit_reviewed_runtime_not_an_implicit_import_hook(service_id):
  manifest = _manifest(
    source_files=["memory-core.md", "service.py"],
    service={"entry": "service.py", "access": "public",
             **({"id": service_id} if service_id else {})},
  )
  validate_manifest_contract(manifest)
  contract, _digest = contract_and_digest(manifest)
  assert contract["service"] == {
    "id": service_id or "memory",
    "entry": "service.py",
    "access": "public",
    "protocol": "json-v1",
    "max_request_bytes": 8 * 1024 * 1024,
    "max_response_bytes": 8 * 1024 * 1024,
  }

  for service in (
    {"entry": "nested/service.py"},
    {"entry": "service.sh"},
    {"entry": "missing.py"},
    {"entry": "service.py", "access": "world"},
  ):
    with pytest.raises(ManifestContractError):
      validate_manifest_contract(_manifest(
        source_files=["memory-core.md", "service.py"], service=service,
      ))


def test_agent_activities_bind_only_declared_source_commands():
  manifest = _manifest(
    source_files=["memory-core.md", "lookup.py"],
    agent_activities={
      "lookup": {
        "entry": "lookup.py", "arguments": 2, "running_label": "Searching",
      },
    },
  )
  validate_manifest_contract(manifest)
  contract, _digest = contract_and_digest(manifest)
  assert "agent_activities" not in contract, (
    "presentation metadata must not become a data/network permission"
  )
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(_manifest(
      source_files=["lookup.py"],
      agent_activities={
        "search": {
          "entry": "lookup.py", "arguments": 2, "running_label": "Searching",
        },
        "read": {
          "entry": "lookup.py", "arguments": 4, "running_label": "Reading",
        },
      },
    ))

  for activity in (
    {"entry": "missing.py", "arguments": 2, "running_label": "Searching"},
    {"entry": "../lookup.py", "arguments": 2, "running_label": "Searching"},
    {"entry": "lookup.py", "arguments": -1, "running_label": "Searching"},
    {"entry": "lookup.py", "arguments": 2, "running_label": ""},
    {"entry": "lookup.py", "arguments": 2, "running_label": "Searching",
     "domain": "memory"},
  ):
    with pytest.raises(ManifestContractError):
      validate_manifest_contract(_manifest(
        source_files=["memory-core.md", "lookup.py"],
        agent_activities={"lookup": activity},
      ))


def test_service_transition_aliases_are_explicit_bounded_contract_data():
  manifest = _manifest(
    source_files=["memory-core.md", "service.py"],
    service={
      "id": "social", "aliases": ["common"],
      "entry": "service.py", "access": "public",
    },
  )
  validate_manifest_contract(manifest)
  contract, _digest = contract_and_digest(manifest)
  assert contract["service"]["id"] == "social"
  assert contract["service"]["aliases"] == ["common"]

  for aliases in (
    ["social"], ["common", "common"],
    ["one", "two", "three", "four", "five"],
  ):
    invalid = _manifest(
      source_files=["memory-core.md", "service.py"],
      service={"id": "social", "aliases": aliases, "entry": "service.py"},
    )
    with pytest.raises(ManifestContractError):
      validate_manifest_contract(invalid)

  implicit = _manifest(
    source_files=["memory-core.md", "service.py"],
    service={"aliases": ["common"], "entry": "service.py"},
  )
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(implicit)


def test_runtime_capability_is_independently_versioned_and_bounded():
  manifest = _manifest(capabilities={
    "media.microphone.capture": {
      "version": 1,
      "reason": "Record a custom drum pad.",
      "limits": {"max_duration_ms": 8_000},
    },
  })
  runtime = normalize_runtime_capabilities(manifest)
  assert runtime == {
    "media.microphone.capture": {
      "version": 1,
      "kind": "session",
      "title": "Record audio",
      "description": "Use the device microphone while this app is visible.",
      "risk": "device",
      "lifecycle": "active_frame",
      "reason": "Record a custom drum pad.",
      "limits": {"max_duration_ms": 8_000},
    },
  }


def test_camera_capture_has_reviewed_duration_and_byte_ceilings():
  manifest = _manifest(capabilities={
    "media.camera.capture": {
      "version": 1,
      "reason": "Capture a room walkthrough for a private 3D scene.",
      "limits": {
        "max_duration_ms": 180_000,
        "max_bytes": 192 * 1024 * 1024,
      },
    },
  })

  runtime = normalize_runtime_capabilities(manifest)

  assert runtime == {
    "media.camera.capture": {
      "version": 1,
      "kind": "session",
      "title": "Record video",
      "description": (
        "Use a device camera, and optionally its microphone, while this app is visible."
      ),
      "risk": "device",
      "lifecycle": "active_frame",
      "reason": "Capture a room walkthrough for a private 3D scene.",
      "limits": {
        "max_duration_ms": 180_000,
        "max_bytes": 192 * 1024 * 1024,
      },
    },
  }


def test_camera_capture_limits_are_validated_and_bound_into_the_digest():
  base = {
    "media.camera.capture": {
      "version": 1,
      "reason": "Record a short video.",
      "limits": {
        "max_duration_ms": 30_000,
        "max_bytes": 8 * 1024 * 1024,
      },
    },
  }
  _contract, digest = contract_and_digest(_manifest(capabilities=base))
  changed = json.loads(json.dumps(base))
  changed["media.camera.capture"]["limits"]["max_bytes"] += 1
  _changed_contract, changed_digest = contract_and_digest(
    _manifest(capabilities=changed),
  )

  assert digest != changed_digest

  for limits in (
    {"max_duration_ms": 300_001, "max_bytes": 8 * 1024 * 1024},
    {"max_duration_ms": 30_000, "max_bytes": 256 * 1024 * 1024 + 1},
  ):
    with pytest.raises(ValueError, match="must be between"):
      normalize_runtime_capabilities(_manifest(capabilities={
        "media.camera.capture": {
          "version": 1,
          "reason": "Record a short video.",
          "limits": limits,
        },
      }))


def test_screen_control_is_reviewed_as_an_app_owned_background_session():
  runtime = normalize_runtime_capabilities(_manifest(capabilities={
    "workspace.screen-control": {
      "version": 1,
      "reason": "Investigate problems in this M\u00f6bius tab.",
    },
  }))
  assert runtime == {
    "workspace.screen-control": {
      "version": 1,
      "kind": "session",
      "title": "Control this M\u00f6bius screen",
      "description": (
        "Let this app's support chat inspect and control the current M\u00f6bius tab."
      ),
      "risk": "device",
      "lifecycle": "background",
      "reason": "Investigate problems in this M\u00f6bius tab.",
      "limits": {},
    },
  }


def test_device_asset_cache_is_client_only_and_reviewed_by_size():
  runtime = normalize_runtime_capabilities(_manifest(capabilities={
    "device.asset-cache": {
      "version": 1,
      "reason": "Keep an on-device speech model in this browser.",
      "limits": {
        "max_bytes": 256 * 1024 * 1024,
        "max_asset_bytes": 256 * 1024 * 1024,
        "max_chunk_bytes": 8 * 1024 * 1024,
      },
    },
  }))

  device_cache = runtime["device.asset-cache"]
  assert device_cache["risk"] == "storage"
  assert device_cache["lifecycle"] == "active_frame"
  assert device_cache["description"] == (
    "Download verified app assets into this browser's private storage."
  )
  assert device_cache["limits"]["max_bytes"] == 256 * 1024 * 1024


def test_device_storage_is_a_small_reviewed_invoke_capability():
  runtime = normalize_runtime_capabilities(_manifest(capabilities={
    "device.storage": {
      "version": 1,
      "reason": "Remember this visitor's bookings in this browser.",
      "limits": {"max_bytes": 32 * 1024},
    },
  }))

  storage = runtime["device.storage"]
  assert storage["kind"] == "invoke"
  assert storage["risk"] == "storage"
  assert storage["lifecycle"] == "active_frame"
  assert storage["limits"] == {"max_bytes": 32 * 1024}


def test_speech_capabilities_separate_model_management_from_generation():
  runtime = normalize_runtime_capabilities(_manifest(capabilities={
    "device.speech-models": {
      "version": 1,
      "reason": "Manage the shared voice library on this device.",
    },
    "media.speech": {
      "version": 1,
      "reason": "Read reports aloud with a selected local voice.",
      "limits": {"max_text_chars": 20_000},
    },
  }))

  assert runtime["device.speech-models"]["risk"] == "storage"
  assert runtime["device.speech-models"]["lifecycle"] == "active_frame"
  assert runtime["media.speech"]["risk"] == "device"
  assert runtime["media.speech"]["lifecycle"] == "background"
  assert runtime["media.speech"]["limits"] == {"max_text_chars": 20_000}


def test_explicit_local_runtime_acceptance_preserves_store_contract():
  installed = _manifest(capabilities={
    "device.asset-cache": {"version": 1},
  })
  contract, _ = contract_and_digest(installed)
  candidate = contract_with_runtime_capabilities(contract, _manifest(capabilities={
    "media.speech": {
      "version": 1,
      "reason": "Read reports with the shared local voice.",
      "limits": {"max_text_chars": 50_000},
    },
  }))

  assert candidate is not None
  assert candidate["runtime"] == normalize_runtime_capabilities(_manifest(capabilities={
    "media.speech": {
      "version": 1,
      "reason": "Read reports with the shared local voice.",
      "limits": {"max_text_chars": 50_000},
    },
  }))
  assert {key: value for key, value in candidate.items() if key != "runtime"} == {
    key: value for key, value in contract.items() if key != "runtime"
  }
  assert contract["runtime"] == normalize_runtime_capabilities(installed)


def test_runtime_capability_rejects_unknown_name_version_and_limits():
  for capabilities, message in (
    ({"device.telepathy": {"version": 1}}, "Unknown capability"),
    ({"media.microphone.capture": {"version": 2}}, "requires version 1"),
    ({
      "media.microphone.capture": {
        "version": 1, "limits": {"max_duration_ms": 60_001},
      },
    }, "must be between"),
  ):
    try:
      normalize_runtime_capabilities(_manifest(capabilities=capabilities))
    except ValueError as exc:
      assert message in str(exc)
    else:
      raise AssertionError("invalid capability declaration was accepted")


def test_local_app_create_normalizes_runtime_capability(client, auth):
  app = create_local_app(
    client, auth,
    name="Recorder",
    description="Records one sound",
    capabilities={
      "media.microphone.capture": {
        "version": 1,
        "reason": "Record a custom sound",
        "limits": {"max_duration_ms": 8000},
      },
    },
  )
  runtime = app["capability_contract"]["runtime"]
  assert runtime["media.microphone.capture"]["version"] == 1
  assert runtime["media.microphone.capture"]["limits"] == {
    "max_duration_ms": 8000,
  }


def test_local_app_capability_replacement_is_explicit(client, auth):
  created = create_local_app(
    client, auth, name="Recorder",
    capabilities={"media.microphone.capture": {"version": 1}},
  )
  manifest_path = Path(created["source_dir"]) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["capabilities"] = {}
  manifest_path.write_text(json.dumps(manifest))
  response = client.post(
    "/api/apps/apply", headers=auth,
    json={"source_dir": created["source_dir"]},
  )
  assert response.status_code == 200, response.text
  assert response.json()["app"]["capability_contract"]["runtime"] == {}


def test_local_app_rejects_unknown_runtime_capability(client, auth):
  source = write_local_source(
    Path(get_settings().data_dir) / "apps" / "unsafe-declaration",
    name="Unsafe declaration",
    capabilities={"device.telepathy": {"version": 1}},
  )
  response = client.post(
    "/api/apps/apply", headers=auth, json={"source_dir": str(source)},
  )

  assert response.status_code == 422
  assert "Unknown capability" in response.json()["detail"]["message"]


def test_digest_mismatch_rejects_before_fetching_code_or_mutating(
  client, auth, db, bypass_url_validation,
):
  base = "https://capability.test/memory/"
  manifest = _manifest()
  requested_urls: list[str] = []

  class FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    def stream(self, method, url, **kwargs):
      requested_urls.append(url)
      from tests.test_apps_install import _StreamCtx
      if url == base + "mobius.json":
        return _StreamCtx(200, json.dumps(manifest).encode())
      return _StreamCtx(500, b"unexpected code fetch")

  with patch("app.install.httpx.AsyncClient", return_value=FakeClient()):
    response = client.post(
      "/api/apps/install",
      headers=auth,
      json={
        "manifest_url": base + "mobius.json",
        "reviewed_capability_digest": "0" * 64,
      },
    )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "capability_changed"
  assert requested_urls == [base + "mobius.json"]
  assert db.query(models.App).count() == 0




@pytest.mark.asyncio
async def test_bootstrap_initialization_waits_for_backend_readiness(
  db, bypass_url_validation,
):
  """Bootstrap and interactive installs share launch ownership but not timing."""
  from app.install import install_from_manifest

  base = "https://capability.test/bootstrap-memory/"
  manifest = _manifest(id="bootstrap-memory", name="Bootstrap Memory")
  _contract, digest = contract_and_digest(manifest)
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "memory-core.md": (200, b"Retrieve memory only on demand."),
    base + "memory-job.sh": (200, b"#!/bin/sh\nexit 0\n"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch("app.app_jobs.launch_app_job") as launch:
    result = await install_from_manifest(
      db,
      base + "mobius.json",
      None,
      None,
      source="bootstrap",
      reviewed_capability_digest=digest,
    )
  app = result.app
  mode = result.mode
  warnings = result.warnings

  assert mode == "install"
  source_dir = Path(app.source_dir)
  launch.assert_called_once_with(
    app.id, source_dir / "memory-job.sh", source_dir,
    wait_for_ready=True,
  )
  assert "initialization waiting for startup readiness" in warnings


def test_matching_digest_is_persisted_with_explicit_system_identity(
  client, auth, db, bypass_url_validation,
):
  base = "https://capability.test/memory/"
  manifest = _manifest()
  contract, digest = contract_and_digest(manifest)
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "memory-core.md": (200, b"Retrieve memory only on demand."),
    base + "memory-job.sh": (200, b"#!/bin/sh\nexit 0\n"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch("app.app_jobs.launch_app_job") as launch:
    response = client.post(
      "/api/apps/install",
      headers=auth,
      json={
        "manifest_url": base + "mobius.json",
        "reviewed_capability_digest": digest,
      },
    )

  assert response.status_code == 201, response.text
  app = db.query(models.App).filter(models.App.slug == "memory").one()
  assert app.capability_contract == contract
  assert response.json()["capability_contract"] == contract
  launch.assert_called_once_with(
    app.id, Path(app.source_dir) / "memory-job.sh", Path(app.source_dir),
    wait_for_ready=True,
  )
  assert "initialization waiting for startup readiness" in response.json()["warnings"]


def test_model_catalog_changes_are_not_access_changes():
  """Renaming or adding models is what a provider offers, not what it reaches;
  only its endpoint or credential is an access change."""
  from app.app_capabilities import diff_contracts

  def contract(**provider):
    return {"schema": 1, "model_provider": {
      "name": "Evolve", "base_url": "https://models.example/v1",
      "secret_name": "EVOLVE_KEY",
      "models": [{"id": "inkling"}], "default_model": "inkling", **provider,
    }}

  renamed = diff_contracts(contract(), contract(
    name="Evolve AI", models=[{"id": "evolve"}, {"id": "evolve-mini"}],
    default_model="evolve",
  ))
  assert renamed == {
    "unknown_previous": False, "added": [], "removed": [], "changed": [],
    "widens": False,
  }
  moved = diff_contracts(contract(), contract(base_url="https://other.example/v1"))
  assert moved["changed"] == ["model_provider.base_url"]


def _contract(permissions=None, **over):
  manifest = _manifest(**over)
  manifest["permissions"].update(permissions or {})
  contract, _digest = contract_and_digest(manifest)
  return contract


def test_update_review_asks_only_when_access_widens():
  plain = _contract()
  github = _contract(permissions={"github_access": True})
  reader = _contract(permissions={"cross_app_access": "read"})
  writer = _contract(permissions={"cross_app_access": "write"})

  assert diff_contracts(plain, github)["widens"] is True
  assert diff_contracts(github, plain)["removed"] == ["data.github_access"]
  assert diff_contracts(github, plain)["widens"] is False
  assert diff_contracts(reader, writer)["widens"] is True
  assert diff_contracts(writer, reader)["widens"] is False


def test_shrinking_a_list_narrows_and_growing_it_widens():
  one = _contract(skills=["alpha"])
  two = _contract(skills=["alpha", "beta"])
  other = _contract(skills=["gamma"])

  assert diff_contracts(two, one)["widens"] is False
  assert diff_contracts(one, two)["widens"] is True
  assert diff_contracts(one, other)["widens"] is True


def _speech(**limits):
  return {"media.speech": {"version": 1, "reason": "Read aloud.", "limits": limits}}


def test_removing_or_raising_a_limit_widens_but_adding_or_lowering_one_does_not():
  capped = _contract(capabilities=_speech(max_text_chars=1000))
  lower = _contract(capabilities=_speech(max_text_chars=500))
  uncapped = _contract(capabilities=_speech())
  assert diff_contracts(capped, uncapped)["widens"] is True
  assert diff_contracts(lower, capped)["widens"] is True
  assert diff_contracts(capped, lower)["widens"] is False
  assert diff_contracts(uncapped, capped)["widens"] is False


def test_dropping_a_whole_capability_with_its_limit_narrows():
  capped = _contract(capabilities=_speech(max_text_chars=1000))

  report = diff_contracts(capped, _contract())

  assert report["removed"]
  assert report["widens"] is False


@pytest.mark.parametrize("reason", ["Speak the current page.", None])
def test_runtime_reason_changes_do_not_widen_access(reason):
  before = _contract(capabilities=_speech(max_text_chars=1000))
  declaration = _speech(max_text_chars=1000)
  declaration["media.speech"]["reason"] = reason
  after = _contract(capabilities=declaration)

  report = diff_contracts(before, after)

  assert report["widens"] is False
  assert "runtime.media.speech.reason" in report["changed"] + report["removed"]


def test_runtime_version_and_unknown_reason_changes_still_require_review():
  before = _contract(capabilities=_speech(max_text_chars=1000))
  after = json.loads(json.dumps(before))
  after["runtime"]["media.speech"]["version"] = 2
  assert diff_contracts(before, after)["widens"] is True

  # Unknown capabilities have unknown semantics, even on a reason-like path.
  before["runtime"]["future.capability"] = {"reason": "Original."}
  after = json.loads(json.dumps(before))
  after["runtime"]["future.capability"]["reason"] = "Changed."
  assert diff_contracts(before, after)["widens"] is True


def test_offline_contract_changes_never_ask_for_access():
  online = _contract()
  offline = _contract(offline_capable=True, offline={"reads": True, "writes": "none"})

  report = diff_contracts(online, offline)
  assert report["added"] == report["removed"] == report["changed"] == []
  assert report["widens"] is False


def test_legacy_contract_requires_review_even_without_changed_paths():
  report = diff_contracts(None, _contract())
  assert report["unknown_previous"] is True
  assert report["widens"] is True


@pytest.mark.parametrize(("field", "old", "new"), [
  ("future_access", "write", "read"),
  ("future_limits", {"ceiling": 5}, None),
  ("future_denylist", ["private"], None),
])
def test_unrecognized_access_values_and_paths_fail_closed(field, old, new):
  before = _contract()
  after = json.loads(json.dumps(before))
  before["data"][field] = old
  if new is not None:
    after["data"][field] = new

  assert diff_contracts(before, after)["widens"] is True


def test_revoking_chat_logs_and_job_secrets_does_not_widen_access():
  before = _contract(permissions={"chat_log_access": "summary_with_deleted",
                                  "job_secret_read": ["one", "two"]})
  after = _contract(permissions={"chat_log_access": "none",
                                 "job_secret_read": ["one"]})
  report = diff_contracts(before, after)
  assert report["widens"] is False
  assert "data.chat_logs.redaction" not in report["changed"]


def test_provider_destination_change_requires_review():
  before = _contract(model_provider={"base_url": "https://a.example/v1"})
  after = _contract(model_provider={"base_url": "https://b.example/v1"})
  assert diff_contracts(before, after)["widens"] is True


@pytest.mark.parametrize('names', [True, 'bot-token', ['../secret'], ['a', 'a'],
                                  ['a'] * 17, [None], ['a b'], ['a' * 65]])
def test_job_secret_permission_rejects_invalid_names(names):
  with pytest.raises(ManifestContractError, match='job_secret_read'):
    validate_manifest_contract(_manifest(permissions={'job_secret_read': names}))


def test_job_secret_permission_is_reviewed_normalized_and_omission_revokes():
  from types import SimpleNamespace
  from app.app_capabilities import contract_from_app_state
  base, base_hash = contract_and_digest(_manifest(permissions={}))
  empty, empty_hash = contract_and_digest(_manifest(permissions={'job_secret_read': []}))
  assert base == empty and base_hash == empty_hash
  manifest = _manifest(permissions={'job_secret_read': ['tg-2', 'tg-1']})
  validate_manifest_contract(manifest)
  granted, digest = contract_and_digest(manifest)
  assert digest != base_hash
  assert granted['data']['job_secret_read'] == ['tg-1', 'tg-2']
  assert 'data.job_secret_read' in diff_contracts(base, granted)['added']
  app = SimpleNamespace(capability_contract=granted)
  accepted = contract_from_app_state(app, contract_permissions=manifest['permissions'])
  assert accepted['data']['job_secret_read'] == ['tg-1', 'tg-2']
  assert contract_from_app_state(app)['data']['job_secret_read'] == ['tg-1', 'tg-2']
  revoked = contract_from_app_state(app, contract_permissions={})
  assert 'job_secret_read' not in revoked['data']
