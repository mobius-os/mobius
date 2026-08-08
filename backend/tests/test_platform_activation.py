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


def test_dependency_and_baked_runtime_never_degrade_to_restart_only():
  # Python deps now apply in place (a rebuild is no longer forced), but they are
  # still MORE than a bare restart. Frontend deps and baked-runtime inputs still
  # require a new image.
  expected = {
    "backend/requirements.txt": "dependency_sync",
    "backend/requirements.lock": "dependency_sync",
    "frontend/package-lock.json": "image_rebuild",
    "backend/scripts/entrypoint.sh": "image_rebuild",
    "backend/runtime/restart_ledger.py": "image_rebuild",
    "backend/recovery_target/targetd.py": "image_rebuild",
    "protected-files.txt": "image_rebuild",
  }
  for path, level in expected.items():
    impact = activation.classify_activation([path], deployment="self_hosted")
    assert impact["level"] == level, path
    assert impact["level"] not in {"live", "server_restart"}, path


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


def test_python_dependencies_apply_in_place_not_via_rebuild():
  impact = activation.classify_activation(
    ["backend/requirements.lock"], deployment="self_hosted",
  )
  assert impact["level"] == "dependency_sync"
  assert any("in place" in line for line in impact["guidance"])

  # A backend code change alongside a dep bump resolves to the dep-sync boundary
  # (which already includes a restart), not a rebuild.
  mixed = activation.classify_activation(
    ["backend/requirements.txt", "backend/app/main.py"], deployment="self_hosted",
  )
  assert mixed["level"] == "dependency_sync"

  # But a Dockerfile change in the same update still forces a rebuild.
  with_dockerfile = activation.classify_activation(
    ["backend/requirements.txt", "Dockerfile"], deployment="self_hosted",
  )
  assert with_dockerfile["level"] == "image_rebuild"


def test_dependency_sync_requires_an_import_probe():
  assert activation.backend_import_probe_required(["backend/requirements.lock"])
