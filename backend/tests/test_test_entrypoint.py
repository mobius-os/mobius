"""The default test entrypoint stays fast, hermetic, and checkout-owned."""

import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "test.sh"
HOST_RUNNER = Path(__file__).parents[2] / "scripts" / "wt-pytest.sh"
CONFTEST = Path(__file__).parent / "conftest.py"
CONTRIBUTING = Path(__file__).parents[2] / "CONTRIBUTING.md"
MOBIUS_DEVELOPMENT = (
  Path(__file__).parents[1] / "scripts" / "seed-skills" / "mobius-development.md"
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
  development = MOBIUS_DEVELOPMENT.read_text()
  assert "opens or updates a draft pull request" in contributing
  assert "opening or updating a **Draft PR**" in development
  assert "Run GitHub checks" not in contributing
  assert "Run GitHub checks" not in development


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


def _git(cwd, *args):
  subprocess.run(
    ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
    cwd=cwd, check=True, capture_output=True,
  )


# A lockfile shaped like the real one around the backend's Node tools: rolldown
# has a hoisted dependency and an optional platform binding, and esrecurse uses
# a nested estraverse that shadows a different hoisted copy only vite uses.
_LOCKED = {
  "node_modules/rolldown": {
    "version": "1.0.0",
    "dependencies": {"@oxc-project/types": "^0.1.0"},
    "optionalDependencies": {"@rolldown/binding-linux-x64-gnu": "1.0.0"},
  },
  "node_modules/@oxc-project/types": {"version": "0.1.0"},
  "node_modules/@rolldown/binding-linux-x64-gnu": {"version": "1.0.0", "optional": True},
  "node_modules/acorn": {"version": "8.0.0"},
  "node_modules/eslint-scope": {"version": "9.0.0", "dependencies": {"esrecurse": "^4.3.0"}},
  "node_modules/esrecurse": {"version": "4.3.0", "dependencies": {"estraverse": "^5.2.0"}},
  "node_modules/esrecurse/node_modules/estraverse": {"version": "5.3.0"},
  "node_modules/estraverse": {"version": "4.3.0"},
  "node_modules/vite": {"version": "7.0.0", "dependencies": {"estraverse": "^4.0.0"}},
}


def _lockfile(changes=None, omit=()):
  """The fixture lockfile, with ``changes`` (path → fields) applied on top and
  the ``omit`` entries left out."""
  packages = {}
  for path, entry in _LOCKED.items():
    if path in omit:
      continue
    entry = {**entry, **(changes or {}).get(path, {})}
    entry.setdefault("integrity", f"sha512-{path}@{entry['version']}")
    packages[path] = entry
  return json.dumps({"lockfileVersion": 3, "packages": {"": {}, **packages}})


def _checkout_with_fake_backend_node_tools(root, lock):
  """A primary checkout whose frontend tree holds just the verified surface."""
  modules = root / "frontend" / "node_modules"
  (modules / ".bin").mkdir(parents=True)
  (modules / ".bin" / "rolldown").write_text("#!/bin/sh\n")
  (modules / ".bin" / "rolldown").chmod(0o755)
  for package in ("acorn", "eslint-scope"):
    (modules / package).mkdir()
    (modules / package / "package.json").write_text('{"name": "%s", "main": "index.js"}' % package)
    (modules / package / "index.js").write_text("module.exports = {}\n")
  (root / "frontend" / "package-lock.json").write_text(lock)
  (root / "backend").mkdir()
  (root / "backend" / "test_probe.py").write_text("def test_probe():\n  assert True\n")
  (root / ".gitignore").write_text("node_modules\n")
  _git(root, "init", "-q", "-b", "main")
  _git(root, "add", "-A")
  _git(root, "commit", "-q", "-m", "base")


def _run_host_runner_in_worktree(
  tmp_path, worktree_lock=None, break_acorn=False, main_lock=None, extra_env=None,
):
  main = tmp_path / "main"
  main.mkdir()
  _checkout_with_fake_backend_node_tools(main, main_lock or _lockfile())
  if break_acorn:
    (main / "frontend" / "node_modules" / "acorn" / "package.json").unlink()
    (main / "frontend" / "node_modules" / "acorn" / "index.js").unlink()
  worktree = tmp_path / "worktree"
  _git(main, "worktree", "add", "-q", "-b", "bump", str(worktree))
  if worktree_lock is not None:
    (worktree / "frontend" / "package-lock.json").write_text(worktree_lock)
  return subprocess.run(
    [str(HOST_RUNNER), "test_probe.py", "-q"],
    cwd=worktree, capture_output=True, text=True, timeout=60,
    env={**os.environ, **(extra_env or {})},
  )


@pytest.mark.parametrize("changes", [
  pytest.param({"node_modules/vite": {"version": "7.1.0"}}, id="unrelated-package"),
  # Only vite uses the hoisted estraverse; esrecurse resolves its nested copy.
  pytest.param({"node_modules/estraverse": {"version": "4.4.0"}}, id="shadowed-hoisted-copy"),
])
def test_host_runner_reuses_backend_node_tools_when_only_other_frontend_packages_differ(
  tmp_path, changes,
):
  result = _run_host_runner_in_worktree(tmp_path, worktree_lock=_lockfile(changes))
  if result.returncode == 78:
    pytest.skip("no pytest runtime available for the host runner here")
  assert result.returncode == 0, result.stdout + result.stderr
  assert "package-lock.json differs" in result.stderr
  assert "locks the backend's Node tools (rolldown, acorn, eslint-scope) identically" in result.stderr


@pytest.mark.parametrize("changes", [
  pytest.param({"node_modules/rolldown": {"version": "1.1.0"}}, id="direct-tool"),
  pytest.param({"node_modules/@oxc-project/types": {"version": "0.2.0"}}, id="hoisted-dependency"),
  pytest.param(
    {"node_modules/@rolldown/binding-linux-x64-gnu": {"integrity": "sha512-rebuilt"}},
    id="optional-platform-binding-integrity",
  ),
  pytest.param(
    {"node_modules/esrecurse/node_modules/estraverse": {"version": "5.4.0"}},
    id="nested-dependency",
  ),
])
def test_host_runner_refuses_a_lockfile_that_changes_the_backend_node_tools(tmp_path, changes):
  result = _run_host_runner_in_worktree(tmp_path, worktree_lock=_lockfile(changes))
  assert result.returncode == 1, result.stdout + result.stderr
  assert "no verified backend Node dependencies match this worktree" in result.stderr
  assert "MOBIUS_WT_PYTEST_NODE_DRIFT=1" in result.stderr


def test_host_runner_reuses_the_primary_node_tree_with_a_warning_when_drift_is_opted_in(tmp_path):
  result = _run_host_runner_in_worktree(
    tmp_path,
    worktree_lock=_lockfile({"node_modules/rolldown": {"version": "1.1.0"}}),
    extra_env={"MOBIUS_WT_PYTEST_NODE_DRIFT": "1"},
  )
  if result.returncode == 78:
    pytest.skip("no pytest runtime available for the host runner here")
  assert result.returncode == 0, result.stdout + result.stderr
  assert "not dependency-authoritative" in result.stderr


@pytest.mark.parametrize("missing", [
  pytest.param(("node_modules/@oxc-project/types",), id="hoisted-dependency"),
  pytest.param(("node_modules/@rolldown/binding-linux-x64-gnu",), id="optional-platform-binding"),
  # Without its nested copy esrecurse would resolve the hoisted one, so drop both.
  pytest.param(
    ("node_modules/esrecurse/node_modules/estraverse", "node_modules/estraverse"),
    id="nested-dependency",
  ),
])
def test_host_runner_refuses_lockfiles_that_both_leave_a_backend_dependency_unresolved(
  tmp_path, missing,
):
  # Both lockfiles omit the same required package and differ elsewhere: the
  # missing package is pinned by neither, so equal gaps must not count as a match.
  result = _run_host_runner_in_worktree(
    tmp_path,
    main_lock=_lockfile(omit=missing),
    worktree_lock=_lockfile({"node_modules/vite": {"version": "7.1.0"}}, omit=missing),
  )
  assert result.returncode == 1, result.stdout + result.stderr
  assert "no verified backend Node dependencies match this worktree" in result.stderr


def test_host_runner_still_refuses_when_no_tree_can_run_the_backend_node_tools(tmp_path):
  result = _run_host_runner_in_worktree(tmp_path, break_acorn=True)
  assert result.returncode == 1, result.stdout + result.stderr
  assert "no verified backend Node dependencies match this worktree" in result.stderr
