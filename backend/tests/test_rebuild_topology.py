"""Installer topology resolution for both supported proxy layouts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "rebuild-topology.py"
SPEC = importlib.util.spec_from_file_location("rebuild_topology", SCRIPT)
assert SPEC and SPEC.loader
topology = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(topology)


def test_bundled_caddy_uses_the_running_base_topology(tmp_path):
  base = tmp_path / "docker-compose.yml"
  base.write_text("services: {}\n", encoding="utf-8")

  assert topology.compose_files(tmp_path, tmp_path, str(base)) == [base]
  assert topology.expected_networks({
    "services": {"app": {"networks": {"default": {}}}},
    "networks": {"default": {"name": "mobius_default"}},
  }) == ["mobius_default"]


def test_shared_edge_preserves_overlay_and_external_network(tmp_path):
  base = tmp_path / "docker-compose.yml"
  overlay = tmp_path / "docker-compose.prod.yml"
  base.write_text("services: {}\n", encoding="utf-8")
  overlay.write_text("services: {}\n", encoding="utf-8")

  assert topology.compose_files(
    tmp_path, tmp_path, f"{base},{overlay}",
  ) == [base, overlay]
  assert topology.expected_networks({
    "services": {"app": {"networks": {"default": {}, "edge-mobius": {}}}},
    "networks": {
      "default": {"name": "mobius_default"},
      "edge-mobius": {"name": "edge-mobius", "external": True},
    },
  }) == ["edge-mobius", "mobius_default"]


def test_topology_accepts_a_compose_directory_inside_the_checkout(tmp_path):
  deploy = tmp_path / "deploy"
  deploy.mkdir()
  compose = deploy / "docker-compose.runtime.yml"
  compose.write_text("services: {}\n", encoding="utf-8")

  assert topology.compose_files(tmp_path, deploy, str(compose)) == [compose]


def test_topology_refuses_a_different_checkout(tmp_path):
  trusted = tmp_path / "trusted"
  trusted.mkdir()
  other = tmp_path / "other"
  other.mkdir()
  compose = other / "docker-compose.yml"
  compose.write_text("services: {}\n", encoding="utf-8")

  with pytest.raises(ValueError, match="different checkout"):
    topology.compose_files(trusted, other, str(compose))


def test_environment_files_accept_absolute_regular_files(tmp_path):
  first = tmp_path / "first.env"
  second = tmp_path / "second.env"
  first.write_text("FIRST=1\n", encoding="utf-8")
  second.write_text("SECOND=2\n", encoding="utf-8")

  assert topology.environment_files(f"{first},{second}") == [first, second]
  assert topology.environment_files("") == []


def test_environment_files_reject_relative_paths(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  Path("relative.env").write_text("VALUE=1\n", encoding="utf-8")

  with pytest.raises(ValueError, match="absolute paths"):
    topology.environment_files("relative.env")


def test_environment_files_reject_symlinks(tmp_path):
  target = tmp_path / "target.env"
  target.write_text("VALUE=1\n", encoding="utf-8")
  linked = tmp_path / "linked.env"
  linked.symlink_to(target)

  with pytest.raises(ValueError, match="symlinks"):
    topology.environment_files(str(linked))
