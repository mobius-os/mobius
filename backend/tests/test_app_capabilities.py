"""Owner-reviewable app capability contracts and install binding."""

import json
from pathlib import Path
from unittest.mock import patch

from app import models
from app.app_capabilities import contract_and_digest
from app.app_capabilities import normalize_runtime_capabilities
from app.config import get_settings
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
    "system_app": True,
    "system_prompt": "memory-core.md",
    "permissions": {
      "chat_log_access": "summary",
      "background_agent": True,
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
  assert body["capability_contract"]["background"]["authority"] == (
    "scoped_system_job"
  )
  assert body["capability_contract"]["schema"] == 2
  assert body["capability_contract"]["runtime"] == {}


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
  assert app.system_app is True
  assert app.capability_contract == contract
  assert response.json()["capability_contract"] == contract
  launch.assert_called_once()
  assert "initialization started" in response.json()["warnings"]
