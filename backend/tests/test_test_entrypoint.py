"""The default test entrypoint stays fast, hermetic, and checkout-owned."""

from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "test.sh"
HOST_RUNNER = Path(__file__).parents[2] / "scripts" / "wt-pytest.sh"


def test_fast_mode_uses_host_runtime_before_any_docker_preflight():
  source = SCRIPT.read_text()
  fast_branch = source.index('if [ "${mode}" = "fast" ]; then', source.index("run_backend()"))
  full_preflight = source.index("check_backend_prereqs", fast_branch)
  host_runner = source.index("scripts/wt-pytest.sh", fast_branch)
  assert host_runner < full_preflight
  assert '"tests/test_readiness.py"' in source
  assert (
    '"tests/test_db_migrations.py::'
    'test_applied_legacy_schema_migration_is_immutable"'
  ) in source
  assert '"tests/test_pm_commit.py"' in source


def test_full_backend_keeps_the_isolated_container_contract():
  source = SCRIPT.read_text()
  assert "Docker is not available in this runtime" in source
  assert "docker compose -p \"${TEST_PROJECT}\"" in source
  assert "docker-compose.test.yml run --rm --no-deps" in source


def test_host_runner_checks_backend_node_surface_not_full_frontend_tree():
  source = HOST_RUNNER.read_text()
  assert "backend_test_node_deps \"$ROOT/frontend\"" in source
  assert "npm ls --depth=0" not in source
