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
    '{"state":"idle","handoff":"external-cutover-v1"}', encoding="utf-8",
  )
  monkeypatch.setattr(dc, "_control_dir", lambda: control)
  monkeypatch.setattr(dc.platform_activation, "deployment_kind", lambda: "self_hosted")

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
  monkeypatch.setattr(host, "config", lambda: config)
  containers = iter([("old-container", "sha256:old"), ("new-container", "sha256:new")])
  monkeypatch.setattr(host, "app_container", lambda _config: next(containers))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(
    host.subprocess, "run",
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

  assert host.run() == 0
  assert not (inbox / "request.json").exists()
  assert events[0] == "drain"
  assert events[1][0] == "compose"
  assert events[1][2] == f"{host.IMAGE}:sha-{target}"
  assert events[2] == ("verified", "new-container", target)
  status = json.loads((control / "status.json").read_text(encoding="utf-8"))
  assert status["state"] == "succeeded"
  assert status["expected_sha"] == target
