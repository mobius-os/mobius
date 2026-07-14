from pathlib import Path

from scripts.verify_test_runtime import validate_runtime


SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def _version(**overrides):
  value = {
    "sha": SHA,
    "serving_source": "platform",
    "served_sha": SHA,
    "platform_sha": SHA,
    "frontend_source": "platform",
  }
  value.update(overrides)
  return value


def test_exact_ci_checkout_is_healthy():
  assert validate_runtime(_version(), SHA, SHA) == []


def test_local_unstamped_checkout_uses_actual_head():
  assert validate_runtime(_version(sha="unknown"), SHA, "unknown") == []


def test_rejects_baked_or_mismatched_runtime():
  errors = validate_runtime(
    _version(
      sha="b" * 40,
      serving_source="baked",
      served_sha="b" * 40,
      platform_sha=None,
      frontend_source="baked",
    ),
    SHA,
    SHA,
  )
  assert any("serving_source" in error for error in errors)
  assert any("frontend_source" in error for error in errors)
  assert any("served_sha" in error for error in errors)
  assert any("platform_sha" in error for error in errors)
  assert any("sha=" in error for error in errors)


def test_rejects_checkout_that_differs_from_ci_sha():
  other = "b" * 40
  errors = validate_runtime(_version(), SHA, other)
  assert any("platform HEAD" in error for error in errors)
  assert any("sha=" in error for error in errors)


def test_test_compose_pins_runtime_to_mounted_checkout():
  compose = (ROOT / "docker-compose.test.yml").read_text(encoding="utf-8")
  assert "MOBIUS_TEST_RUNTIME=1" in compose
  assert "MOBIUS_TEST_PLATFORM_SOURCE=/workspace" in compose
  assert "BUILD_SHA=${GITHUB_SHA:-unknown}" in compose
  assert "./:/workspace:ro" in compose
  assert 'python3", "/app/scripts/verify_test_runtime.py"' in compose


def test_test_runtime_seed_precedes_selection_and_skips_reconcile():
  entrypoint = (
    ROOT / "backend" / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")
  seed_call = '_platform_seed_test_checkout || exit 1'
  selection = 'if [ ! -d "$_platform_app" ]; then'
  assert entrypoint.index(seed_call) < entrypoint.index(selection)
  assert (
    'if [ "$_use_platform" -eq 1 ] && '
    '[ "${MOBIUS_TEST_RUNTIME:-0}" != "1" ]; then'
  ) in entrypoint
