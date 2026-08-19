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
