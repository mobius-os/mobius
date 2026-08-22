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


def test_topology_refuses_a_different_checkout(tmp_path):
  other = tmp_path / "other"
  other.mkdir()
  compose = other / "docker-compose.yml"
  compose.write_text("services: {}\n", encoding="utf-8")

  with pytest.raises(ValueError, match="different checkout"):
    topology.compose_files(tmp_path, other, str(compose))
