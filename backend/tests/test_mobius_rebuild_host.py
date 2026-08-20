"""Trust-boundary tests for the separately installed host rebuild worker."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "mobius-rebuild-host.py"
SPEC = importlib.util.spec_from_file_location("mobius_rebuild_host", SCRIPT)
assert SPEC and SPEC.loader
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


def _config(root: Path) -> dict:
  return {
    "version": 2,
    "directory": str(root),
    "project": "mobius",
    "service": "app",
    "files": [str(root / "compose.yml"), str(root / "image.override.yml")],
    "image": "ghcr.io/mobius-os/mobius",
    "source_commit": "a" * 40,
  }


def _snapshot(tmp_path: Path) -> Path:
  config_path = tmp_path / "config.json"
  (tmp_path / "compose.yml").write_text("services: {}\n", encoding="utf-8")
  (tmp_path / "image.override.yml").write_text("services: {}\n", encoding="utf-8")
  return config_path


def test_frozen_config_accepts_only_the_fixed_snapshot_files(tmp_path: Path):
  config_path = _snapshot(tmp_path)

  result = host.validate_config(
    _config(tmp_path), config_path=config_path, trusted_uid=os.getuid(),
  )

  assert result["files"] == [
    str(tmp_path / "compose.yml"), str(tmp_path / "image.override.yml"),
  ]


def test_frozen_config_rejects_group_writable_input(tmp_path: Path):
  config_path = _snapshot(tmp_path)
  (tmp_path / "compose.yml").chmod(0o620)

  with pytest.raises(ValueError, match="not root-controlled"):
    host.validate_config(
      _config(tmp_path), config_path=config_path, trusted_uid=os.getuid(),
    )


def test_frozen_config_rejects_symlinked_input(tmp_path: Path):
  config_path = _snapshot(tmp_path)
  target = tmp_path / "mutable.yml"
  target.write_text("services: {}\n", encoding="utf-8")
  (tmp_path / "compose.yml").unlink()
  (tmp_path / "compose.yml").symlink_to(target)

  with pytest.raises(ValueError, match="may not use symlinks"):
    host.validate_config(
      _config(tmp_path), config_path=config_path, trusted_uid=os.getuid(),
    )


def test_frozen_config_rejects_checkout_paths(tmp_path: Path):
  config_path = _snapshot(tmp_path)
  value = _config(tmp_path)
  value["files"] = [str(tmp_path / "compose.yml")]

  with pytest.raises(ValueError, match="invalid deployment directory"):
    host.validate_config(
      value, config_path=config_path, trusted_uid=os.getuid(),
    )


def test_active_status_requires_a_live_worker_or_systemd_unit(monkeypatch):
  current = {"operation_id": "a" * 32, "state": "queued"}
  monkeypatch.setattr(host, "_lock_is_held", lambda _path: False)
  monkeypatch.setattr(host, "_unit_is_running", lambda _operation: False)

  assert host.rebuild_is_running(current) is False

  monkeypatch.setattr(host, "_unit_is_running", lambda _operation: True)
  assert host.rebuild_is_running(current) is True


def test_worker_lock_alone_proves_rebuild_is_running(monkeypatch):
  monkeypatch.setattr(host, "_lock_is_held", lambda _path: True)
  monkeypatch.setattr(host, "_unit_is_running", lambda _operation: False)

  assert host.rebuild_is_running({"operation_id": None}) is True


def test_status_reconciles_an_abandoned_active_job(monkeypatch):
  current = {
    "operation_id": "a" * 32, "state": "verifying",
    "updated_at": "2020-01-01T00:00:00+00:00",
  }
  monkeypatch.setattr(host, "read_json", lambda _path: current)
  monkeypatch.setattr(host, "rebuild_is_running", lambda _current: False)
  written = {}
  monkeypatch.setattr(host, "write_status", lambda **fields: written.update(fields) or fields)

  result = host.status()

  assert result["state"] == "failed"
  assert result["code"] == "worker_interrupted"


def test_status_preserves_a_newly_queued_job_during_systemd_handoff(monkeypatch):
  current = {
    "operation_id": "a" * 32, "state": "queued",
    "updated_at": host.now(),
  }
  monkeypatch.setattr(host, "read_json", lambda _path: current)
  monkeypatch.setattr(host, "rebuild_is_running", lambda _current: False)

  assert host.status() is current


def test_worker_restores_the_previous_container_after_replacement_throws(
  tmp_path: Path, monkeypatch,
):
  operation = "b" * 32
  expected = "c" * 40
  request = tmp_path / "request.json"
  request.write_text(
    f'{{"operation_id":"{operation}","expected_sha":"{expected}"}}',
    encoding="utf-8",
  )
  monkeypatch.setattr(host, "STATE_DIR", tmp_path)
  monkeypatch.setattr(host, "LOCK", tmp_path / "worker.lock")
  monkeypatch.setattr(host, "CONFIG", tmp_path / "config.json")
  monkeypatch.setattr(host, "read_json", lambda path: (
    {"operation_id": operation, "expected_sha": expected}
    if path == request else {"config": True}
  ))
  monkeypatch.setattr(host, "validate_config", lambda _value: {"image": "official"})
  monkeypatch.setattr(host.subprocess, "run", lambda *args, **kwargs: None)
  monkeypatch.setattr(host, "inspect_value", lambda _image, template: (
    expected if "revision" in template else host.IMAGE_SOURCE if "source" in template else "new-digest"
  ))
  monkeypatch.setattr(host, "container_image", lambda _config: "old-digest")
  calls = []
  def compose(_config, *args, image=None, **_kwargs):
    calls.append(image)
    if image == "new-digest":
      raise RuntimeError("replacement failed")
  monkeypatch.setattr(host, "compose", compose)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  statuses = []
  monkeypatch.setattr(host, "write_status", lambda **fields: statuses.append(fields) or fields)

  assert host.worker(request) == 1
  assert calls == ["new-digest", "old-digest"]
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == "rebuild_failed"
