"""Activation classification is one ordered contract across deployments."""

from pathlib import Path

from app import platform_activation as activation


def test_ordered_levels_choose_the_highest_effective_action():
  impact = activation.classify_activation(
    ["frontend/src/App.jsx", "backend/app/main.py", "Dockerfile"],
    deployment="self_hosted",
  )

  assert impact["level"] == "image_rebuild"
  assert {reason["code"] for reason in impact["reasons"]} == {
    "live_source", "server_runtime", "container_image_definition",
  }


def test_deployment_specific_inputs_share_contract_without_fake_commands():
  caddy_on_railway = activation.classify_activation(
    ["Caddyfile"], deployment="railway",
  )
  railway_on_self_hosted = activation.classify_activation(
    ["railway.toml"], deployment="self_hosted",
  )

  assert caddy_on_railway["level"] == "live"
  assert caddy_on_railway["reasons"] == []
  assert railway_on_self_hosted["level"] == "live"
  assert railway_on_self_hosted["reasons"] == []


def test_baked_runtime_and_dependencies_never_degrade_to_restart_only():
  for path in (
    "backend/requirements.txt",
    "frontend/package-lock.json",
    "backend/scripts/entrypoint.sh",
    "backend/runtime/restart_ledger.py",
    "backend/recovery_target/targetd.py",
    "protected-files.txt",
  ):
    impact = activation.classify_activation([path], deployment="self_hosted")
    assert impact["level"] == "image_rebuild", path


def test_dependency_fingerprint_comes_from_the_image_rules():
  root = Path(__file__).resolve().parents[2]
  paths = activation.dependency_fingerprint_paths(root)

  assert paths == sorted({
    "Dockerfile",
    "backend/legacy_runtime/jose/__init__.py",
    "backend/legacy_runtime/verify_jose.py",
    "backend/requirements.lock",
    "backend/requirements.txt",
    "frontend/package-lock.json",
    "frontend/package.json",
  })
