"""The default test entrypoint stays fast, hermetic, and checkout-owned."""

from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "test.sh"
HOST_RUNNER = Path(__file__).parents[2] / "scripts" / "wt-pytest.sh"
CONFTEST = Path(__file__).parent / "conftest.py"
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
  assert '"tests/test_schema_migration_history.py"' in source
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


def test_host_runner_isolates_database_before_pytest_collects_modules():
  source = HOST_RUNNER.read_text()
  pytest_call = source.index('"$PYTHON" -m pytest')
  assert source.index('TEST_RUNTIME_ROOT="$(mktemp -d') < pytest_call
  assert 'DATABASE_URL="sqlite:///$TEST_RUNTIME_ROOT/test.db"' in source
  assert 'DATA_DIR="$TEST_RUNTIME_ROOT/data"' in source
  assert "MOBIUS_TEST_DATABASE_ISOLATED=1" in source
  assert "trap cleanup_test_runtime EXIT" in source
  assert "trap 'exit 130' INT" in source


def test_host_runner_drops_managed_identity_before_pytest_collects_modules():
  source = HOST_RUNNER.read_text()
  pytest_call = source.index('"$PYTHON" -m pytest')
  assert 'MOBIUS_SSO_ISSUER=' in source[:pytest_call]
  assert 'MOBIUS_SSO_INSTANCE_ID=' in source[:pytest_call]
  assert (
    'MOBIUS_IDENTITY_BROKER_SOCKET="$TEST_RUNTIME_ROOT/no-identity-broker.sock"'
    in source[:pytest_call]
  )


def test_live_database_guard_requires_database_isolation_not_generic_test_mode():
  source = CONFTEST.read_text()
  guard = source[source.index("_inherited_data_dir"):source.index("# Set env vars")]
  assert 'os.environ.get("MOBIUS_TEST_DATABASE_ISOLATED") != "1"' in guard
  assert 'os.environ.get("MOBIUS_TEST_RUNTIME")' not in guard


def test_pre_push_delegates_backend_pytest_to_the_canonical_runner():
  hook = (
    Path(__file__).parents[2] / "scripts" / "githooks" / "pre-push"
  ).read_text()
  assert "MOBIUS_PYTEST_SERIALIZE=1" in hook
  assert '"$MAIN/scripts/wt-pytest.sh" -q' in hook
  assert '"$VENV" -m pytest' not in hook
  assert "flock" not in hook
  assert '78)' in hook
  assert "no Python test runtime" in hook

  runner = HOST_RUNNER.read_text()
  assert '"${MOBIUS_PYTEST_SERIALIZE:-0}" = "1"' in runner
  assert '"$MAIN/backend/.venv/.suite.lock"' in runner
  assert "exit 78" in runner


def test_image_runtime_reports_when_its_python_lock_differs():
  source = HOST_RUNNER.read_text()
  assert 'cmp -s "$ROOT/backend/requirements.lock" /app/requirements.lock' in source
  assert 'elif [ -r /app/requirements.lock ]' in source
  assert "checkout requirements.lock differs from the image runtime" in source
  assert "not dependency-authoritative" in source
