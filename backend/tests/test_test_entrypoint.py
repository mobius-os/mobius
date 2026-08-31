"""The default test entrypoint stays fast, hermetic, and checkout-owned."""

from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "test.sh"
HOST_RUNNER = Path(__file__).parents[2] / "scripts" / "wt-pytest.sh"
CONTRIBUTING = Path(__file__).parents[2] / "CONTRIBUTING.md"
PLATFORM_MAINTENANCE = (
  Path(__file__).parents[1] / "scripts" / "seed-skills" / "platform-maintenance.md"
)


def test_fast_mode_uses_host_runtime_before_any_docker_preflight():
  source = SCRIPT.read_text()
  fast_branch = source.index('if [ "${mode}" = "fast" ]; then', source.index("run_backend()"))
  full_preflight = source.index("check_backend_prereqs", fast_branch)
  host_runner = source.index("scripts/wt-pytest.sh", fast_branch)
  assert host_runner < full_preflight
  assert '"tests/test_readiness.py"' in source
  assert (
    '"tests/test_db_migrations.py::'
    'test_previous_release_database_upgrades_to_current_orm"'
  ) in source
  assert (
    '"tests/test_db_migrations.py::'
    'test_published_schema_migration_history_is_unique_ordered_and_immutable"'
  ) in source
  assert '"tests/test_pm_commit.py"' in source


def test_full_backend_keeps_the_isolated_container_contract():
  source = SCRIPT.read_text()
  assert "normal Möbius app container" in source
  assert "intentional trust boundary" in source
  assert "open or update a draft PR" in source
  assert "Run GitHub checks" not in source
  assert "docker compose -p \"${TEST_PROJECT}\"" in source
  assert "docker-compose.test.yml run --rm --no-deps" in source


def test_hosted_checks_documentation_uses_the_draft_pr_path():
  contributing = CONTRIBUTING.read_text()
  maintenance = PLATFORM_MAINTENANCE.read_text()
  assert "opens or updates a draft pull request" in contributing
  assert "opening or updating a **Draft PR**" in maintenance
  assert "Run GitHub checks" not in contributing
  assert "Run GitHub checks" not in maintenance


def test_host_runner_checks_backend_node_surface_not_full_frontend_tree():
  source = HOST_RUNNER.read_text()
  assert "backend_test_node_deps \"$ROOT/frontend\"" in source
  assert "npm ls --depth=0" not in source


def test_image_runtime_reports_when_its_python_lock_differs():
  source = HOST_RUNNER.read_text()
  assert 'cmp -s "$ROOT/backend/requirements.lock" /app/requirements.lock' in source
  assert "checkout requirements.lock differs from the image runtime" in source
  assert "not dependency-authoritative" in source


def test_host_runner_clears_live_deployment_origin_derivation():
  source = HOST_RUNNER.read_text()
  assert "DOMAIN=localhost" in source
  assert "FRONTEND_ORIGIN=http://localhost:5173" in source
  assert "RAILWAY_PUBLIC_DOMAIN=" in source
