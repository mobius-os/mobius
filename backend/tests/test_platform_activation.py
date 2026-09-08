"""Activation classification is one ordered contract across deployments."""

import json
import re
import subprocess
import sys
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


def test_railway_config_guidance_does_not_promise_an_image_rebuild():
  impact = activation.classify_activation(
    ["railway.toml"], deployment="railway",
  )

  assert impact["level"] == "container_recreate"
  guidance = " ".join(impact["guidance"])
  assert "finish this change in Railway" in guidance
  assert "Möbius will rebuild Railway" not in guidance


def test_dependency_and_baked_runtime_never_degrade_to_restart_only():
  # Python deps now apply in place (a rebuild is no longer forced), but they are
  # still MORE than a bare restart. Baked-runtime inputs still require a new
  # image. (Frontend deps now apply in place too — see the dedicated test.)
  expected = {
    "backend/requirements.txt": "dependency_sync",
    "backend/requirements.lock": "dependency_sync",
    "backend/scripts/entrypoint.sh": "image_rebuild",
    "backend/scripts/init_skills.py": "image_rebuild",
    "backend/scripts/seed-skills/platform-maintenance.md": "image_rebuild",
    "backend/runtime": "image_rebuild",
    "backend/runtime/restart_ledger.py": "image_rebuild",
    "protected-files.txt": "image_rebuild",
  }
  for path, level in expected.items():
    impact = activation.classify_activation([path], deployment="self_hosted")
    assert impact["level"] == level, path
    assert impact["level"] not in {"live", "server_restart"}, path


def test_frontend_dependencies_apply_in_place_not_via_rebuild():
  # A frontend dependency bump is installed in place during Apply (npm ci) and
  # the shell is rebuilt live — it no longer forces a container/image rebuild,
  # mirroring the Python dependency precedent.
  for path in ("frontend/package.json", "frontend/package-lock.json"):
    for deployment in ("self_hosted", "railway"):
      impact = activation.classify_activation([path], deployment=deployment)
      assert impact["level"] == "live", (path, deployment)

  # It still contributes to the image dependency fingerprint (node_modules are
  # baked at image-build time), so a rebuild's image content stays reproducible.
  root = Path(__file__).resolve().parents[2]
  fingerprint = activation.dependency_fingerprint_paths(root)
  assert "frontend/package.json" in fingerprint
  assert "frontend/package-lock.json" in fingerprint

  # A frontend dep bump does not trigger the backend import probe.
  assert not activation.backend_import_probe_required(
    ["frontend/package-lock.json"],
  )

  # But a Dockerfile change in the same update still forces a rebuild.
  with_dockerfile = activation.classify_activation(
    ["frontend/package-lock.json", "Dockerfile"], deployment="self_hosted",
  )
  assert with_dockerfile["level"] == "image_rebuild"


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


def test_only_image_owned_bootstrap_scripts_require_a_rebuild():
  assert activation.classify_activation([
    "backend/scripts/goal_plan.py",
  ])["level"] == "live"
  assert activation.classify_activation([
    "backend/scripts/rebuild_shell.sh",
  ])["level"] == "live"
  assert activation.classify_activation([
    "backend/scripts/mapi",
  ])["level"] == "live"
  assert activation.classify_activation([
    "backend/scripts/pm-commit",
  ])["level"] == "server_restart"
  assert activation.classify_activation([
    "scripts/mobius-rebuild-host.py",
  ])["level"] == "host_maintenance"


def test_bootstrap_allowlist_covers_entrypoint_app_script_references():
  root = Path(__file__).resolve().parents[2]
  entrypoint = (root / "backend/scripts/entrypoint.sh").read_text(
    encoding="utf-8",
  )
  referenced = set(re.findall(
    r"/app/scripts/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)",
    entrypoint,
  ))
  # pm-commit is seeded from the image, then deliberately refreshed from the
  # live checkout by FastAPI startup; it needs one server restart, not an image.
  # mapi is an image fallback only: the installed symlink already targets the
  # live checkout, so changing that target takes effect immediately.
  image_names = {Path(path).name for path in activation.IMAGE_BOOTSTRAP_SCRIPTS}
  assert referenced == (image_names - {"entrypoint.sh"}) | {"pm-commit", "mapi"}
  assert activation.classify_activation([
    "backend/scripts/entrypoint.sh",
  ])["level"] == "image_rebuild"
  for name in referenced - {"pm-commit", "mapi"}:
    assert activation.classify_activation([
      f"backend/scripts/{name}",
    ])["level"] == "image_rebuild", name


def test_image_inputs_cover_dependency_and_baked_runtime_paths(tmp_path):
  (tmp_path / "backend" / "runtime").mkdir(parents=True)
  (tmp_path / "backend" / "app").mkdir(parents=True)
  (tmp_path / "Dockerfile").write_text("FROM scratch\n")
  (tmp_path / "backend" / "requirements.lock").write_text("a==1\n")
  (tmp_path / "backend" / "runtime" / "broker.py").write_text("x = 1\n")
  (tmp_path / "backend" / "app" / "main.py").write_text("y = 2\n")

  paths = activation.image_input_paths(tmp_path)

  # Only existing files; the served backend app is not an image input.
  assert paths == [
    "Dockerfile", "backend/requirements.lock", "backend/runtime/broker.py",
  ]
  hashes = activation.image_input_hashes(tmp_path)
  assert set(hashes) == set(paths)
  assert all(len(value) == 64 for value in hashes.values())
  # Every input is a path the classifier already treats as image-owned.
  for path in paths:
    level = activation.classify_activation([path])["level"]
    assert level in {
      activation.ActivationLevel.IMAGE_REBUILD.value,
      activation.ActivationLevel.DEPENDENCY_SYNC.value,
    }


def test_image_input_hashes_cli_matches_the_library_contract(tmp_path):
  (tmp_path / "backend" / "runtime").mkdir(parents=True)
  (tmp_path / "Dockerfile").write_text("FROM scratch\n")
  (tmp_path / "backend" / "runtime" / "broker.py").write_text("x = 1\n")

  module = Path(activation.__file__)
  completed = subprocess.run(
    [sys.executable, str(module), "--hashes", str(tmp_path)],
    check=True,
    capture_output=True,
    text=True,
  )

  assert json.loads(completed.stdout) == activation.image_input_hashes(tmp_path)
