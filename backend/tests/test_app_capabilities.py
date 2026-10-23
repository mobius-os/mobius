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
from test_app_fixtures import accept_app_consent, create_local_app, write_local_source
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
    (("public", "storage", "read"), True),
    (("public", "storage", "write_prefix"), "public/submissions/"),
  ],
)
def test_legacy_receipt_still_reports_real_new_grants(path, value):
  installed, _digest = contract_and_digest(_manifest())
  installed["schema"] = 4
  installed.pop("public")
  for field in ("connect_manage", "identity_manage", "railway_manage"):
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


def test_service_transfer_ceiling_is_reviewed_and_defaults_closed():
  base = _manifest(source_files=["service.py"], service={"entry": "service.py"})
  default, _ = contract_and_digest(base)
  enlarged = dict(base, service={"entry": "service.py", "max_bytes": 60 * 1024 * 1024})
  granted, _ = contract_and_digest(enlarged)
  assert granted["service"]["max_request_bytes"] == 60 * 1024 * 1024
  assert granted["service"]["max_response_bytes"] == 60 * 1024 * 1024
  assert diff_contracts(default, granted)["widens"] is True
  assert diff_contracts(granted, default)["widens"] is False
  for value in (True, False, 0, -1, 60 * 1024 * 1024 + 1, 1.5, "62914560", None):
    with pytest.raises(ManifestContractError, match="service.max_bytes"):
      validate_manifest_contract(dict(base, service={"entry": "service.py", "max_bytes": value}))


def test_service_revocation_never_restores_an_absent_transfer_ceiling():
  manifest = _manifest(source_files=["service.py"],
                       service={"entry": "service.py", "max_bytes": 1024})
  capped, _ = contract_and_digest(manifest)
  revoked = {key: value for key, value in capped.items() if key != "service"}
  assert diff_contracts(capped, revoked)["widens"] is False

  # Removing only the explicit ceiling keeps the service granted and restores
  # its larger default; unlike revocation, that does require owner review.
  legacy = dict(capped, service={key: value for key, value in capped["service"].items()
                               if key not in {"max_request_bytes", "max_response_bytes"}})
  assert diff_contracts(capped, legacy)["widens"] is True


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


def test_device_folders_is_a_device_capability_with_reviewed_read_limits():
  runtime = normalize_runtime_capabilities(_manifest(capabilities={
    "device.folders": {
      "version": 1,
      "reason": "Read letter scans from a folder on this computer.",
      "limits": {"max_file_bytes": 128 * 1024 * 1024},
    },
  }))

  folders = runtime["device.folders"]
  assert folders["kind"] == "invoke"
  assert folders["risk"] == "device"
  assert folders["lifecycle"] == "active_frame"
  assert folders["limits"] == {
    "max_file_bytes": 128 * 1024 * 1024,
    "max_entries": 5000,
  }
  with pytest.raises(ValueError, match="max_file_bytes"):
    normalize_runtime_capabilities(_manifest(capabilities={
      "device.folders": {"version": 1, "limits": {"max_file_bytes": 4 * 1024 ** 3}},
    }))


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
  from app.install import install_from_manifest, prepare_package

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
      prepared=await prepare_package(
        base + "mobius.json", reviewed_capability_digest=digest,
      ),
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
  # A Store app is dormant until accepted, so its first run waits for that.
  launch.assert_not_called()
  with patch("app.routes.app_consent.launch_app_job") as first_run:
    accept_app_consent(client, auth, app.id)
  first_run.assert_called_once_with(
    app.id, Path(app.source_dir) / "memory-job.sh", Path(app.source_dir),
  )


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


MIB = 1024 * 1024


def _routed(route_limits, *, max_bytes=60 * MIB, **service):
  declared = {"entry": "service.py", "access": "public", "max_bytes": max_bytes, **service}
  if route_limits is not None:
    declared["route_limits"] = route_limits
  return _manifest(source_files=["memory-core.md", "service.py"], service=declared)


def _limit(method, path, max_bytes=MIB):
  return {"method": method, "path": path, "max_bytes": max_bytes}


def test_service_route_limits_accept_exact_bounded_canonical_routes():
  limits = [
    _limit("POST", "/activity"), _limit("GET", "/actor"), _limit("DELETE", "/actor"),
    _limit("PATCH", "/a/b-c_d.e~f/" + "x" * 499, 1),  # exactly 512 characters
    *(_limit("PUT", f"/r{index}") for index in range(12)),
  ]
  validate_manifest_contract(_routed(limits))
  # The default 8 MiB grant bounds a route ceiling when max_bytes is omitted.
  default = _manifest(source_files=["memory-core.md", "service.py"], service={
    "entry": "service.py", "route_limits": [_limit("GET", "/actor", 8 * MIB)],
  })
  validate_manifest_contract(default)


@pytest.mark.parametrize("route_limits", [
  {"GET /actor": MIB},
  [_limit("GET", f"/r{index}") for index in range(17)],
  ["GET /actor"],
  [{"method": "GET", "path": "/actor"}],
  [{**_limit("GET", "/actor"), "max_response_bytes": MIB}],
  [_limit("get", "/actor")],
  [_limit("HEAD", "/actor")],
  [_limit(None, "/actor")],
  [_limit("GET", "actor")],
  [_limit("GET", "/")],
  [_limit("GET", "/actor/")],
  [_limit("GET", "//actor")],
  [_limit("GET", "/a//b")],
  [_limit("GET", "/a/./b")],
  [_limit("GET", "/a/../b")],
  [_limit("GET", "/a/%2e%2e")],
  [_limit("GET", "/a%2Fb")],
  [_limit("GET", "/actor?x=1")],
  [_limit("GET", "/actor#x")],
  [_limit("GET", "/act or")],
  [_limit("GET", "/act\tor")],
  [_limit("GET", "/act or")],
  [_limit("GET", "/act\x00or")],
  [_limit("GET", "/act\x7for")],
  [_limit("GET", "/" + "x" * 512)],
  [_limit("GET", 7)],
  [_limit("GET", "/actor", True)],
  [_limit("GET", "/actor", 0)],
  [_limit("GET", "/actor", 1.5)],
  [_limit("GET", "/actor", "1024")],
  [_limit("GET", "/actor", 60 * MIB + 1)],
  [_limit("GET", "/actor"), _limit("GET", "/actor", 512)],
], ids=lambda value: json.dumps(value)[:40])
def test_malformed_service_route_limits_fail_manifest_review(route_limits):
  with pytest.raises(ManifestContractError, match="service.route_limits"):
    validate_manifest_contract(_routed(route_limits))


def test_route_limit_cannot_exceed_its_own_service_grant():
  with pytest.raises(ManifestContractError, match="service.route_limits"):
    validate_manifest_contract(_routed([_limit("GET", "/actor", 2048)], max_bytes=1024))
  with pytest.raises(ManifestContractError, match="service.route_limits"):
    validate_manifest_contract(_manifest(source_files=["memory-core.md", "service.py"], service={
      "entry": "service.py", "route_limits": [_limit("GET", "/actor", 8 * MIB + 1)],
    }))


def test_route_limits_contract_is_sorted_stable_and_absent_when_empty():
  shuffled = [_limit("POST", "/b"), _limit("GET", "/b", 2048), _limit("POST", "/a", 4096)]
  contract, digest = contract_and_digest(_routed(shuffled))
  assert contract["service"]["route_limits"] == [
    {"method": "POST", "path": "/a", "max_request_bytes": 4096, "max_response_bytes": 4096},
    {"method": "GET", "path": "/b", "max_request_bytes": 2048, "max_response_bytes": 2048},
    {"method": "POST", "path": "/b", "max_request_bytes": MIB, "max_response_bytes": MIB},
  ]
  _again, reordered_digest = contract_and_digest(_routed(list(reversed(shuffled))))
  assert reordered_digest == digest

  # A service without ceilings keeps its pre-existing review digest exactly.
  legacy, legacy_digest = contract_and_digest(_routed(None))
  empty, empty_digest = contract_and_digest(_routed([]))
  assert "route_limits" not in legacy["service"] and "route_limits" not in empty["service"]
  assert empty_digest == legacy_digest
  assert set(legacy["service"]) == {
    "id", "entry", "access", "protocol", "max_request_bytes", "max_response_bytes",
  }
  assert diff_contracts(legacy, empty) == {
    "unknown_previous": False, "widens": False, "added": [], "removed": [], "changed": [],
  }


def test_unrelated_row_projection_preserves_accepted_route_limits():
  from types import SimpleNamespace
  from app.app_capabilities import contract_from_app_state

  accepted, _digest = contract_and_digest(_routed([_limit("GET", "/actor"), _limit("POST", "/activity")]))
  app = SimpleNamespace(capability_contract=accepted)
  assert contract_from_app_state(app)["service"] == accepted["service"]

  # A malformed accepted list stays exactly as accepted, so the runtime keeps
  # refusing it; an unrelated metadata write must not silently widen routes.
  malformed = json.loads(json.dumps(accepted))
  malformed["service"]["route_limits"] = [{"method": "GET", "path": "actor"}]
  assert contract_from_app_state(SimpleNamespace(capability_contract=malformed))["service"][
    "route_limits"
  ] == [{"method": "GET", "path": "actor"}]

  # An explicit local manifest owns the service: its ceilings replace the
  # accepted ones, and an explicit service removal removes them with it.
  local = contract_from_app_state(app, service={
    "id": "memory", "entry": "service.py", "access": "public", "max_bytes": 60 * MIB,
    "route_limits": [_limit("GET", "/actor", 1024)],
  })
  assert local["service"]["route_limits"] == [
    {"method": "GET", "path": "/actor", "max_request_bytes": 1024, "max_response_bytes": 1024},
  ]
  assert "service" not in contract_from_app_state(app, service=None)


def test_route_limit_review_narrows_on_add_or_lower_and_widens_on_loss_or_raise():
  def contract(limits, **service):
    return contract_and_digest(_routed(limits, **service))[0]

  none = contract(None)
  actor = contract([_limit("GET", "/actor")])
  both = contract([_limit("GET", "/actor"), _limit("POST", "/activity")])
  lower = contract([_limit("GET", "/actor", 1024)])

  assert diff_contracts(none, actor)["widens"] is False  # introduce
  assert diff_contracts(actor, both)["widens"] is False  # introduce another
  assert diff_contracts(actor, lower)["widens"] is False  # lower
  assert diff_contracts(lower, actor)["widens"] is True  # raise
  assert diff_contracts(both, actor)["widens"] is True  # remove one
  assert diff_contracts(actor, none)["widens"] is True  # remove all; service stays
  assert diff_contracts(actor, contract([_limit("GET", "/actors")]))["widens"] is True  # rename
  assert diff_contracts(actor, contract([_limit("POST", "/actor")]))["widens"] is True  # method

  revoked = {key: value for key, value in actor.items() if key != "service"}
  assert diff_contracts(actor, revoked)["widens"] is False  # complete revocation

  # The whole-grant increase is still classified on its own.
  small = contract([_limit("GET", "/actor", 1024)], max_bytes=8 * MIB)
  assert diff_contracts(small, lower)["widens"] is True



def _accepted(method="GET", path="/actor", request=1, response=1, **extra):
  return {"method": method, "path": path, "max_request_bytes": request,
          "max_response_bytes": response, **extra}


@pytest.mark.parametrize("malformed", [
  "1024",
  {"GET /actor": 1},
  [None],
  [_accepted(path=[])],
  [_accepted(method={})],
  [_accepted(method={}, path=[])],
  [_accepted(response=True)],
  [_accepted(response=1.0)],
  [_accepted(request=True, response=True)],
  [_accepted(request=1.0, response=1.0)],
  [_accepted(request=0, response=0)],
  [_accepted(request=-1, response=-1)],
  [_accepted(request=1, response=2)],
  [{"method": "GET", "path": "/actor", "max_request_bytes": 1}],
  [_accepted(extra=True)],
  [_accepted(method=None)],
  [_accepted(method="get")],
  [_accepted(method="HEAD")],
  [_accepted(path="actor")],
  [_accepted(path="/actor/")],
  [_accepted(path="/a//b")],
  [_accepted(path="/a/../b")],
  [_accepted(path="/a%2Fb")],
  [_accepted(path="/act or")],
  [_accepted(), _accepted(request=2, response=2)],
  [_accepted(path=f"/r{index}") for index in range(17)],
], ids=lambda value: json.dumps(value)[:48])
def test_malformed_accepted_route_limits_widen_in_both_directions_without_error(malformed):
  # Each malformed list carries a ceiling no larger than the valid one, so
  # only strict shape reading (not a size comparison) can classify it.
  actor, _digest = contract_and_digest(_routed([_limit("GET", "/actor", 1024)]))
  broken = json.loads(json.dumps(actor))
  broken["service"]["route_limits"] = malformed
  assert diff_contracts(actor, broken)["widens"] is True
  assert diff_contracts(broken, actor)["widens"] is True
  # Complete revocation still never widens, whatever was stored before.
  revoked = {key: value for key, value in broken.items() if key != "service"}
  assert diff_contracts(broken, revoked)["widens"] is False


def test_route_limit_review_uses_the_runtime_reading_of_accepted_ceilings():
  from app.manifest_contract import accepted_service_route_limits

  accepted, _digest = contract_and_digest(_routed([_limit("POST", "/b"), _limit("GET", "/a", 2048)]))
  limits = accepted["service"]["route_limits"]
  assert accepted_service_route_limits(limits) == {("GET", "/a"): 2048, ("POST", "/b"): MIB}
  # The runtime additionally bounds each ceiling by the service's own grant.
  assert accepted_service_route_limits(limits, MIB) == {("GET", "/a"): 2048, ("POST", "/b"): MIB}
  assert accepted_service_route_limits(limits, MIB - 1) is None
  assert accepted_service_route_limits([]) == {}


@pytest.mark.parametrize(("request_bytes", "response_bytes"), [
  (1, True), (1, 1.0), (True, True), (1.0, 1.0), (True, 1), (1.0, 1),
], ids=["response-bool", "response-float", "both-bool", "both-float", "request-bool", "request-float"])
def test_equal_looking_bool_or_float_ceiling_is_a_reviewed_change(request_bytes, response_bytes):
  # Python equates 1, True and 1.0, so a value-equality diff would report no
  # change at all against a valid integer ceiling of 1.
  valid, _digest = contract_and_digest(_routed([_limit("GET", "/actor", 1)]))
  lookalike = json.loads(json.dumps(valid))
  lookalike["service"]["route_limits"][0].update(
    max_request_bytes=request_bytes, max_response_bytes=response_bytes,
  )
  for installed, candidate in ((valid, lookalike), (lookalike, valid)):
    report = diff_contracts(installed, candidate)
    assert report["changed"] == ["service.route_limits"]
    assert report["widens"] is True
  assert diff_contracts(lookalike, lookalike) == {
    "unknown_previous": False, "widens": True, "added": [], "removed": [], "changed": [],
  }
  revoked = {key: value for key, value in lookalike.items() if key != "service"}
  assert diff_contracts(lookalike, revoked)["widens"] is False


@pytest.mark.parametrize("stored", [
  None, False, {}, {"GET /actor": 1}, "1024",
  [_accepted(path=[])], [_accepted(response=True)],
], ids=lambda value: json.dumps(value)[:40])
def test_unreadable_route_limits_need_review_even_when_unchanged_or_pruned(stored):
  # null, false and {} are pruned like an absent grant, but the runtime
  # refuses them; only absence and [] mean "no route ceilings".
  plain, _digest = contract_and_digest(_routed(None))
  unreadable = json.loads(json.dumps(plain))
  unreadable["service"]["route_limits"] = stored
  assert diff_contracts(unreadable, unreadable)["widens"] is True
  assert diff_contracts(plain, unreadable)["widens"] is True
  assert diff_contracts(unreadable, plain)["widens"] is True
  revoked = {key: value for key, value in unreadable.items() if key != "service"}
  assert diff_contracts(unreadable, revoked)["widens"] is False


def test_readable_identical_absent_or_empty_route_limits_need_no_review():
  plain, plain_digest = contract_and_digest(_routed(None))
  capped, _digest = contract_and_digest(_routed([_limit("GET", "/actor", 1)]))
  empty = json.loads(json.dumps(plain))
  empty["service"]["route_limits"] = []
  unchanged = {
    "unknown_previous": False, "widens": False, "added": [], "removed": [], "changed": [],
  }
  assert diff_contracts(plain, plain) == unchanged
  assert diff_contracts(capped, json.loads(json.dumps(capped))) == unchanged
  assert diff_contracts(plain, empty) == unchanged
  assert diff_contracts(empty, plain) == unchanged
  assert contract_and_digest(_routed([]))[1] == plain_digest


@pytest.mark.parametrize("revoked_service", [
  "omitted", None, False, {},
  {"id": None, "entry": None, "access": False, "aliases": [], "route_limits": None},
  {"route_limits": []}, {"route_limits": False},
], ids=json.dumps)
@pytest.mark.parametrize("stored", [None, {}, "1024", [_accepted(response=True)]],
                         ids=lambda value: json.dumps(value)[:40])
def test_complete_service_revocation_never_widens_from_unreadable_route_limits(
  revoked_service, stored,
):
  installed, _digest = contract_and_digest(_routed(None))
  installed["service"]["route_limits"] = stored
  candidate = {key: value for key, value in installed.items() if key != "service"}
  if revoked_service != "omitted":
    candidate["service"] = revoked_service
  assert diff_contracts(installed, candidate)["widens"] is False
  # Malformed limits on a service that actually remains are still reviewed.
  assert diff_contracts(installed, installed)["widens"] is True


@pytest.mark.parametrize("stored", ["1024", {"GET /actor": 1}, [_accepted(path=[])]],
                         ids=lambda value: json.dumps(value)[:40])
def test_unreadable_route_limits_alone_keep_a_service_under_review(stored):
  # A remaining non-closed route field is still a service the runtime must
  # refuse, so it is never mistaken for complete revocation.
  installed, _digest = contract_and_digest(_routed([_limit("GET", "/actor", 1)]))
  assert diff_contracts(installed, {"schema": 6, "service": {"route_limits": stored}})["widens"] is True




_OMITTED = object()
_TRANSFER_FIELDS = ("max_request_bytes", "max_response_bytes", "route_limits")
_ROUTE_SHAPES = {
  "no-routes": _OMITTED, "empty-routes": [], "routes": [_accepted(request=1, response=1)],
}
_REFUSED_WHOLE_GRANTS = [
  (1024.0, 1024.0), (True, True), (1024, 1024.0), (1024, True), (1024.0, 1024),
  (1024, 2048), (2048, 1024), (0, 0), (-1, -1),
  (60 * MIB + 1, 60 * MIB + 1), ("1024", "1024"), (None, None),
  (1024, _OMITTED), (_OMITTED, 1024),
]


def _grant_id(value):
  return "omitted" if value is _OMITTED else json.dumps(value)


def _stored_grant(route_limits, *, request=1024, response=1024):
  """An accepted service carrying raw stored whole and route ceilings.

  `_OMITTED` drops a field, as in a service accepted before it existed.
  """
  contract = json.loads(json.dumps(contract_and_digest(_routed(None, max_bytes=1024))[0]))
  service = contract["service"]
  for key, value in zip(_TRANSFER_FIELDS, (request, response, route_limits)):
    if value is _OMITTED:
      service.pop(key, None)
    else:
      service[key] = value
  return contract


def _transfer_json(contract):
  """The stored transfer fields exactly, so 1, 1.0 and True stay distinct."""
  service = contract["service"]
  return json.dumps({key: service[key] for key in _TRANSFER_FIELDS if key in service}, sort_keys=True)


def _runtime_refuses(contract):
  from types import SimpleNamespace
  from fastapi import HTTPException
  from app import app_services

  try:
    app_services.service_contract(
      SimpleNamespace(capability_contract=contract, manifest_url=None), access="public",
    )
  except HTTPException as exc:
    assert exc.status_code == 503
    return True
  return False


def _unrelated_projection(contract, **explicit):
  """An owner row write that does not touch the service, unless `explicit` does."""
  from types import SimpleNamespace
  from app.app_capabilities import contract_from_app_state

  return contract_from_app_state(SimpleNamespace(capability_contract=contract), **explicit)


def test_route_ceiling_above_the_whole_grant_needs_review_unchanged_recovered_or_introduced():
  # The runtime bounds every route ceiling by the service's whole grant, so
  # this stored service is disabled; ranking 2048 -> 1024 as "lower" would
  # silently enable it, and an unchanged copy is still one the runtime refuses.
  over = _stored_grant([_accepted(request=2048, response=2048)])
  fit = _stored_grant([_accepted(request=1024, response=1024)])
  assert _runtime_refuses(over) and not _runtime_refuses(fit)

  assert diff_contracts(over, over) == {
    "unknown_previous": False, "widens": True, "added": [], "removed": [], "changed": [],
  }
  recovery = diff_contracts(over, fit)
  assert recovery["changed"] == ["service.route_limits"] and recovery["widens"] is True
  assert diff_contracts(fit, over)["widens"] is True  # reverse: raise past the grant

  # Lowering the whole grant beneath an unchanged route ceiling disables the
  # service too, and restoring it re-enables it; both need review.
  roomy = _stored_grant([_accepted(request=2048, response=2048)], request=2048, response=2048)
  assert not _runtime_refuses(roomy)
  lowered = diff_contracts(roomy, over)
  assert lowered["changed"] == ["service.max_request_bytes", "service.max_response_bytes"]
  assert lowered["widens"] is True
  assert diff_contracts(over, roomy)["widens"] is True

  # An unrelated row write keeps the refused pair exactly, so it stays off.
  projected = _unrelated_projection(over)
  assert _transfer_json(projected) == _transfer_json(over) and _runtime_refuses(projected)


def test_route_ceiling_equal_to_the_whole_grant_is_readable_and_needs_no_review():
  unchanged = {
    "unknown_previous": False, "widens": False, "added": [], "removed": [], "changed": [],
  }
  fit = _stored_grant([_accepted(request=1024, response=1024)])
  assert diff_contracts(fit, json.loads(json.dumps(fit))) == unchanged
  assert diff_contracts(fit, _stored_grant([_accepted(request=512, response=512)]))["widens"] is False

  top = 60 * MIB
  ceiling = _stored_grant([_accepted(request=top, response=top)], request=top, response=top)
  assert not _runtime_refuses(ceiling)
  assert diff_contracts(ceiling, json.loads(json.dumps(ceiling))) == unchanged


def test_route_ceilings_of_a_legacy_service_are_bounded_by_its_8_mib_default():
  legacy_fit = _stored_grant(
    [_accepted(request=8 * MIB, response=8 * MIB)], request=_OMITTED, response=_OMITTED,
  )
  legacy_over = _stored_grant(
    [_accepted(request=8 * MIB + 1, response=8 * MIB + 1)], request=_OMITTED, response=_OMITTED,
  )
  assert not _runtime_refuses(legacy_fit) and _runtime_refuses(legacy_over)
  assert diff_contracts(legacy_fit, json.loads(json.dumps(legacy_fit)))["widens"] is False
  assert diff_contracts(legacy_over, legacy_over)["widens"] is True
  assert diff_contracts(legacy_over, legacy_fit)["widens"] is True  # recovery enables it
  assert diff_contracts(legacy_fit, legacy_over)["widens"] is True


@pytest.mark.parametrize("routes", list(_ROUTE_SHAPES))
@pytest.mark.parametrize(("request_bytes", "response_bytes"), _REFUSED_WHOLE_GRANTS, ids=_grant_id)
def test_whole_grant_the_runtime_refuses_stays_refused_and_reviewed_with_any_route_shape(
  routes, request_bytes, response_bytes,
):
  # Every route ceiling here fits the valid grant, so only reading the whole
  # grant exactly as the runtime does can tell this service is disabled.
  refused = _stored_grant(_ROUTE_SHAPES[routes], request=request_bytes, response=response_bytes)
  fit = _stored_grant(_ROUTE_SHAPES[routes])
  assert _runtime_refuses(refused) and not _runtime_refuses(fit)

  assert diff_contracts(refused, refused)["widens"] is True
  assert diff_contracts(refused, fit)["widens"] is True  # recovery re-enables it
  assert diff_contracts(fit, refused)["widens"] is True
  revoked = {key: value for key, value in refused.items() if key != "service"}
  assert diff_contracts(refused, revoked)["widens"] is False

  # An unrelated row write neither repairs nor mutates the accepted grant.
  accepted = json.dumps(refused, sort_keys=True)
  projected = _unrelated_projection(refused)
  assert json.dumps(refused, sort_keys=True) == accepted
  assert _transfer_json(projected) == _transfer_json(refused)
  assert _runtime_refuses(projected)


@pytest.mark.parametrize(("request_bytes", "response_bytes"), [
  (1.0, 1.0), (True, True), (1, 1.0), (1, True), (1.0, 1), (True, 1),
], ids=_grant_id)
def test_bool_or_float_whole_grant_lookalike_is_a_reported_reviewed_change(
  request_bytes, response_bytes,
):
  # Python equates 1, True and 1.0, so value equality would hide the change
  # that disables (or, reversed, re-enables) the service.
  valid = _stored_grant(_OMITTED, request=1, response=1)
  lookalike = _stored_grant(_OMITTED, request=request_bytes, response=response_bytes)
  assert _runtime_refuses(lookalike) and not _runtime_refuses(valid)
  differing = [
    f"service.{key}"
    for key, value in (("max_request_bytes", request_bytes), ("max_response_bytes", response_bytes))
    if type(value) is not int
  ]
  for installed, candidate in ((valid, lookalike), (lookalike, valid)):
    report = diff_contracts(installed, candidate)
    assert report["changed"] == differing and report["widens"] is True
  assert diff_contracts(lookalike, lookalike) == {
    "unknown_previous": False, "widens": True, "added": [], "removed": [], "changed": [],
  }
  assert _transfer_json(_unrelated_projection(lookalike)) == _transfer_json(lookalike)


@pytest.mark.parametrize("routes", list(_ROUTE_SHAPES))
def test_legacy_whole_grant_absence_is_its_8_mib_default_and_survives_projection(routes):
  legacy = _stored_grant(_ROUTE_SHAPES[routes], request=_OMITTED, response=_OMITTED)
  explicit = _stored_grant(_ROUTE_SHAPES[routes], request=8 * MIB, response=8 * MIB)
  assert not _runtime_refuses(legacy)
  assert diff_contracts(legacy, json.loads(json.dumps(legacy))) == {
    "unknown_previous": False, "widens": False, "added": [], "removed": [], "changed": [],
  }
  # Spelling the same default out, or dropping it again, grants nothing new.
  assert diff_contracts(legacy, explicit)["widens"] is False
  assert diff_contracts(explicit, legacy)["widens"] is False

  projected = _unrelated_projection(legacy)
  assert _transfer_json(projected) == _transfer_json(legacy)
  assert "max_request_bytes" not in projected["service"]
  assert "max_response_bytes" not in projected["service"]


def test_unrelated_projection_keeps_the_accepted_grant_while_an_explicit_service_replaces_it():
  accepted = _stored_grant([_accepted(request=512, response=512)])
  assert _unrelated_projection(accepted)["service"] == accepted["service"]

  refused = _stored_grant(_OMITTED, request=1024, response=2048)
  stored = json.dumps(refused, sort_keys=True)
  # A deliberate local service declaration is re-derived from manifest form,
  # and the re-enabled service it produces is reviewed.
  declared = _unrelated_projection(refused, service={
    "id": refused["service"]["id"], "entry": "service.py", "access": "public", "max_bytes": 2048,
  })
  assert declared["service"]["max_request_bytes"] == declared["service"]["max_response_bytes"] == 2048
  assert "route_limits" not in declared["service"]
  assert not _runtime_refuses(declared)
  assert diff_contracts(refused, declared)["widens"] is True
  # An explicit removal removes the whole service, refused limits included.
  assert "service" not in _unrelated_projection(refused, service=None)
  assert json.dumps(refused, sort_keys=True) == stored


@pytest.mark.parametrize("revoked_service", [
  "omitted", None, False, {},
  {"id": None, "entry": None, "access": False, "aliases": [], "route_limits": None},
  {"route_limits": []}, {"max_request_bytes": None, "max_response_bytes": None},
], ids=json.dumps)
@pytest.mark.parametrize(("route_limits", "request_bytes", "response_bytes"), [
  ([_accepted(request=2048, response=2048)], 1024, 1024),
  ([_accepted(request=8 * MIB + 1, response=8 * MIB + 1)], _OMITTED, _OMITTED),
  ([_accepted(request=1, response=1)], 1024.0, 1024.0),
  (_OMITTED, 1024.0, 1024.0),
  (_OMITTED, True, True),
  ([], 1024, 2048),
], ids=["route-over-grant", "route-over-legacy-default", "routes-float-grant",
        "float-grant", "bool-grant", "empty-routes-unequal-grant"])
def test_complete_service_revocation_never_widens_from_transfer_limits_the_runtime_refuses(
  revoked_service, route_limits, request_bytes, response_bytes,
):
  installed = _stored_grant(route_limits, request=request_bytes, response=response_bytes)
  assert _runtime_refuses(installed)
  candidate = {key: value for key, value in installed.items() if key != "service"}
  if revoked_service != "omitted":
    candidate["service"] = revoked_service
  assert diff_contracts(installed, candidate)["widens"] is False
  # The same refused service that actually remains is still reviewed.
  assert diff_contracts(installed, installed)["widens"] is True
