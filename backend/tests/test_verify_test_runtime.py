import fcntl
import os
from pathlib import Path
import shutil
import subprocess

from scripts.verify_test_runtime import PLATFORM_ROOT, platform_head, validate_runtime


SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def _version(**overrides):
  value = {
    "test_runtime": True,
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


def test_rejects_runtime_without_explicit_test_identity():
  errors = validate_runtime(_version(test_runtime=False), SHA, SHA)
  assert any("test_runtime" in error for error in errors)


def test_rejects_checkout_that_differs_from_ci_sha():
  other = "b" * 40
  errors = validate_runtime(_version(), SHA, other)
  assert any("platform HEAD" in error for error in errors)
  assert any("sha=" in error for error in errors)


def test_healthcheck_marks_only_the_mounted_checkout_safe(monkeypatch):
  captured = {}

  class Result:
    stdout = f"{SHA}\n"

  def fake_run(command, **kwargs):
    captured["command"] = command
    captured["kwargs"] = kwargs
    return Result()

  monkeypatch.setattr(subprocess, "run", fake_run)

  assert platform_head() == SHA
  assert captured["command"] == [
    "git",
    "-c",
    f"safe.directory={PLATFORM_ROOT}",
    "-C",
    str(PLATFORM_ROOT),
    "rev-parse",
    "HEAD",
  ]
  assert captured["kwargs"] == {
    "check": True,
    "capture_output": True,
    "text": True,
    "timeout": 3,
  }


def test_test_compose_pins_runtime_to_mounted_checkout():
  compose = (ROOT / "docker-compose.test.yml").read_text(encoding="utf-8")
  assert "MOBIUS_TEST_RUNTIME=1" in compose
  assert "MOBIUS_TEST_PLATFORM_SOURCE=/workspace" in compose
  assert "BUILD_SHA=${GITHUB_SHA:-unknown}" in compose
  assert "./:/workspace:ro" in compose
  assert 'python3", "/app/scripts/verify_test_runtime.py"' in compose


def test_test_wrapper_isolates_compose_and_rejects_stale_images():
  wrapper = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
  assert 'TEST_PROJECT="${MOBIUS_TEST_PROJECT:-mobius-test-' in wrapper
  assert 'TEST_IMAGE="${MOBIUS_IMAGE:-mobius-test:ci}"' in wrapper
  assert 'docker compose -p "${TEST_PROJECT}"' in wrapper
  assert "test-image-fingerprint.sh" in wrapper
  assert "the test runner never rebuilds" in wrapper
  dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
  assert "COPY Dockerfile ./test-image-inputs/Dockerfile" not in dockerfile
  shell_deps = "RUN cd ./shell-src && npm ci --ignore-scripts"
  retained_assets = (
    "RUN mkdir -p /tmp/pdfjs-install",
    "RUN mkdir -p /tmp/katex-install",
  )
  backend_source = "COPY backend/app ./app/"
  backend_scripts = "COPY backend/scripts ./scripts/"
  platform_seed = 'git clone --depth 1 "$MOBIUS_PLATFORM_ORIGIN" /app/platform-baked'
  frontend_source = "COPY frontend/ ./shell-src/"
  assert dockerfile.count(backend_source) == 1
  assert dockerfile.count(backend_scripts) == 1
  for asset_stage in retained_assets:
    assert dockerfile.index(shell_deps) < dockerfile.index(asset_stage)
    assert dockerfile.index(asset_stage) < dockerfile.index(frontend_source)
  assert dockerfile.index(frontend_source) < dockerfile.index(platform_seed)
  assert dockerfile.index(platform_seed) < dockerfile.index(backend_source)


def test_node_runtime_satisfies_the_pinned_agent_browser_engine():
  dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
  preship = (ROOT / "scripts" / "preship-gate.sh").read_text(encoding="utf-8")
  assert "FROM node:24-slim AS node-runtime" in dockerfile
  assert "agent-browser@0.31.1" in dockerfile
  assert "node:24-slim sh -c" in preship
  assert "node:22" not in dockerfile
  assert "node:22" not in preship


def test_pre_push_syntax_check_keeps_bytecode_out_of_checkout():
  hook = (ROOT / "scripts" / "githooks" / "pre-push").read_text(
    encoding="utf-8"
  )
  assert 'PYTHONPYCACHEPREFIX="$PP_TMP/pycache"' in hook


def test_pre_push_only_runs_frontend_suite_with_complete_dependencies():
  hook = (ROOT / "scripts" / "githooks" / "pre-push").read_text(
    encoding="utf-8"
  )
  assert "npm ls --depth=0" in hook
  assert "dependency tree unavailable or incomplete" in hook


def test_identity_verifier_allows_the_mobius_owned_platform_repo():
  verifier = (ROOT / "backend/scripts/verify_test_runtime.py").read_text(
    encoding="utf-8"
  )
  assert 'f"safe.directory={PLATFORM_ROOT}"' in verifier


def test_pre_push_rejects_main_and_defers_backend_to_pr_ci():
  hook = (ROOT / "scripts" / "githooks" / "pre-push").read_text(
    encoding="utf-8"
  )
  assert 'refs/heads/main)' in hook
  assert "direct updates to main are prohibited" in hook
  assert "scripts/submit-pr.sh" in hook
  assert '${MOBIUS_PREPUSH_FULL:-0}' in hook
  assert 'this push does not update main' in hook
  assert 'full suite currently ~10m' in hook


def test_git_doctor_compares_installed_hooks_to_landed_main():
  doctor = (ROOT / "scripts" / "git-doctor.sh").read_text(encoding="utf-8")
  assert 'origin/main:$source_path' in doctor
  assert 'git show "origin/main:$source_path"' in doctor


def test_submit_pr_rechecks_landed_hooks_after_refresh():
  submit = (ROOT / "scripts" / "submit-pr.sh").read_text(encoding="utf-8")
  initial_doctor = submit.index("scripts/git-doctor.sh --fix")
  fetch = submit.index("git fetch origin main")
  rebase = submit.index("git rebase origin/main")
  publish = submit.index('info "publishing ${branch}"')
  refreshed_segment = submit[rebase:publish]

  # The first doctor repairs pre-existing shared-repo corruption. The second
  # enforces any hook policy that the fetch just landed.
  assert initial_doctor < fetch < rebase < publish
  assert "scripts/git-doctor.sh --fix" in refreshed_segment


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


def test_browser_setup_fails_closed_before_auth_and_never_wipes_chats():
  setup = (ROOT / "tests" / "auth.setup.mjs").read_text(encoding="utf-8")
  marker_probe = 'request.get(`${BASE}/api/version`'
  auth_write = 'request.post(`${BASE}/api/auth/setup`'
  assert marker_probe in setup
  assert "version?.test_runtime !== true" in setup
  assert setup.index(marker_probe) < setup.index(auth_write)
  assert 'request.get(`${BASE}/api/chats`' not in setup
  assert 'request.delete(`${BASE}/api/chats/' not in setup


def test_chat_cleanup_uses_registered_ids_without_account_listing():
  tracker = (ROOT / "tests" / "_chatTracker.mjs").read_text(encoding="utf-8")
  assert "registerCreatedChats" in tracker
  assert "drainCreatedChats" in tracker
  assert "Promise.all(ids.map" in tracker
  assert 'request.get(`${BASE}/api/chats`' not in tracker


def test_local_browser_e2e_is_explicit_and_disposable():
  config = (ROOT / "playwright.config.mjs").read_text(encoding="utf-8")
  runner = (ROOT / "scripts" / "playwright-local.sh").read_text(encoding="utf-8")
  assert "MOBIUS_LOCAL_E2E" in config
  assert "MOBIUS_AUTH_FILE" in config
  assert "--allow-local-e2e" in runner
  assert "down -v --remove-orphans" in runner
  assert 'value.get("test_runtime") is not True' in runner
  assert 'MOBIUS_AUTH_FILE="$auth_file"' in runner
  assert 'recovery_test_port="$(docker port "$recovery_container" 8001/tcp' in runner
  assert 'MOBIUS_RECOVER_URL="http://localhost:${recovery_test_port}"' in runner
  assert 'git clone --quiet --no-local "$ROOT" "$snapshot_dir"' in runner
  assert '--project-directory "$snapshot_dir"' in runner
  assert 'cd "$snapshot_dir"' in runner
  assert 'MOBIUS_LOCAL_E2E_WORKERS:-1' in runner
  assert 'MOBIUS_LOCAL_E2E_WORKERS must be a positive integer' in runner
  assert '"$snapshot_dir/node_modules/.bin/playwright" test "$@" --workers="$e2e_workers"' in runner
  assert "Local E2E artifacts retained at:" in runner
  assert 'compose logs --no-color app caddy recoveryd fake-tandoor' in runner
  assert 'MOBIUS_LOCAL_E2E_KEEP_CACHE' in runner
  assert 'MOBIUS_LOCAL_E2E_KEEP_CACHE:-0' in runner
  assert 'mobius-local-e2e-cache-${checkout_id}:test' in runner
  assert 'MOBIUS_LOCAL_E2E_MIN_FREE_GB' in runner
  assert 'MOBIUS_LOCAL_E2E_MIN_FREE_GB:-20' in runner
  assert 'MOBIUS_LOCAL_E2E_ADMISSION_WAIT:-1800' in runner
  assert 'mobius-local-e2e-admission-${UID}.lock' in runner
  assert 'flock -w "$admission_wait" 8' in runner
  assert "docker system df" in runner
  assert 'docker image tag "$image_name" "$cache_image"' in runner
  assert 'docker image rm "$image_name"' in runner
  assert 'error: timed out waiting for the isolated test backend' in runner
  assert 'error: isolated test stack failed to start' in runner
  assert 'error: timed out waiting for isolated browser proxy' in runner


def _git_env(home: Path) -> dict[str, str]:
  """Minimal Git environment for nested-repository harnesses.

  These tests run near the end of the full suite and must not inherit any
  repository-discovery or temporary config variables exercised by earlier Git
  tests. A fresh HOME also prevents host/image user config from changing init
  or clone behavior.
  """
  home.mkdir(parents=True, exist_ok=True)
  empty_config = home / ".gitconfig-empty"
  empty_config.write_text("", encoding="utf-8")
  return {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": str(home),
    "XDG_CONFIG_HOME": str(home / ".config"),
    "TMPDIR": str(home),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": str(empty_config),
  }


def _git(repo: Path, *args: str):
  result = subprocess.run(
    ["git", "-C", str(repo), *args], check=False, capture_output=True,
    text=True, env=_git_env(repo.parent),
  )
  if result.returncode != 0:
    raise AssertionError(
      f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
    )
  return result


def _init_repo(repo: Path):
  repo.mkdir(parents=True)
  _git(repo, "init", "-q")
  _git(repo, "config", "user.name", "Test")
  _git(repo, "config", "user.email", "test@example.com")


def test_local_runner_refuses_uncommitted_edits_before_docker(tmp_path):
  repo = tmp_path / "repo"
  _init_repo(repo)
  (repo / "scripts").mkdir()
  shutil.copy2(ROOT / "scripts" / "playwright-local.sh", repo / "scripts")
  playwright = repo / "node_modules" / ".bin" / "playwright"
  playwright.parent.mkdir(parents=True)
  playwright.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
  playwright.chmod(0o755)
  tracked = repo / "tracked.txt"
  tracked.write_text("clean\n", encoding="utf-8")
  _git(repo, "add", ".")
  _git(repo, "commit", "-qm", "fixture")
  tracked.write_text("dirty\n", encoding="utf-8")

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  docker = fake_bin / "docker"
  docker.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
  docker.chmod(0o755)
  result = subprocess.run(
    [str(repo / "scripts" / "playwright-local.sh"), "--allow-local-e2e"],
    cwd=repo,
    capture_output=True,
    text=True,
    env={
      **_git_env(tmp_path),
      "PATH": f"{fake_bin}:{os.environ['PATH']}",
    },
  )
  assert result.returncode == 2
  assert "requires a committed revision" in result.stderr

  _git(repo, "restore", "tracked.txt")
  (repo / "new-source.py").write_text("untracked\n", encoding="utf-8")
  result = subprocess.run(
    [str(repo / "scripts" / "playwright-local.sh"), "--allow-local-e2e"],
    cwd=repo,
    capture_output=True,
    text=True,
    env={
      **_git_env(tmp_path),
      "PATH": f"{fake_bin}:{os.environ['PATH']}",
    },
  )
  assert result.returncode == 2
  assert "requires a committed revision" in result.stderr


def test_local_runner_serializes_worktrees_before_docker_probe(tmp_path):
  repo = tmp_path / "repo"
  _init_repo(repo)
  (repo / "scripts").mkdir()
  shutil.copy2(ROOT / "scripts" / "playwright-local.sh", repo / "scripts")
  playwright = repo / "node_modules" / ".bin" / "playwright"
  playwright.parent.mkdir(parents=True)
  playwright.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
  playwright.chmod(0o755)
  _git(repo, "add", ".")
  _git(repo, "commit", "-qm", "fixture")

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  docker_marker = tmp_path / "docker-called"
  docker = fake_bin / "docker"
  docker.write_text(
    f"#!/bin/sh\ntouch {docker_marker}\nexit 99\n",
    encoding="utf-8",
  )
  docker.chmod(0o755)
  runtime_dir = tmp_path / "runtime"
  runtime_dir.mkdir()
  lock_path = runtime_dir / f"mobius-local-e2e-admission-{os.getuid()}.lock"

  with lock_path.open("w", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result = subprocess.run(
      [str(repo / "scripts" / "playwright-local.sh"), "--allow-local-e2e"],
      cwd=repo,
      capture_output=True,
      text=True,
      env={
        **_git_env(tmp_path),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "MOBIUS_LOCAL_E2E_ADMISSION_WAIT": "0",
      },
    )

  assert result.returncode == 2
  assert "timed out after 0s waiting for the local E2E build slot" in result.stderr
  assert not docker_marker.exists()


def test_no_local_clone_from_linked_worktree_has_standalone_git_dir(tmp_path):
  repo = tmp_path / "repo"
  _init_repo(repo)
  (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
  _git(repo, "add", ".")
  _git(repo, "commit", "-qm", "fixture")
  linked = tmp_path / "linked"
  snapshot = tmp_path / "snapshot"
  _git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")

  subprocess.run(
    ["git", "clone", "--quiet", "--no-local", str(linked), str(snapshot)],
    check=True, env=_git_env(tmp_path),
  )
  assert (linked / ".git").is_file()
  assert (snapshot / ".git").is_dir()
  assert _git(snapshot, "rev-parse", "HEAD").stdout == _git(
    linked, "rev-parse", "HEAD"
  ).stdout


def test_documented_browser_commands_use_disposable_runner():
  contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
  test_script = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
  spec_text = "\n".join(
    path.read_text(encoding="utf-8") for path in (ROOT / "tests").glob("*.mjs")
  )
  assert "npx playwright test" not in contributing
  assert "npx playwright test" not in test_script
  assert "npx playwright test" not in spec_text
  assert "playwright-local.sh --allow-local-e2e" in contributing
  assert "playwright-local.sh --allow-local-e2e" in test_script
  assert '/home/' not in test_script


def test_pull_requests_run_required_suites_and_main_only_refreshes_cache():
  test_workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
    encoding="utf-8"
  )
  cache_workflow = (ROOT / ".github" / "workflows" / "image-cache.yml").read_text(
    encoding="utf-8"
  )
  test_triggers = test_workflow.split("\npermissions:\n", 1)[0]
  cache_triggers = cache_workflow.split("\npermissions:\n", 1)[0]
  backend = test_workflow.split("\n  backend:\n", 1)[1].split(
    "\n  frontend-unit:\n", 1,
  )[0]
  e2e = test_workflow.split("\n  e2e:\n", 1)[1]

  assert "pull_request:\n" in test_triggers
  assert "push:\n" not in test_triggers
  assert "'feat/**'" not in test_triggers
  assert "'fix/**'" not in test_triggers
  for job in (backend, e2e):
    assert "github.event_name == 'pull_request'" not in job
    assert "refs/heads/integration/" not in job
  assert "needs: privacy" in e2e
  assert "needs: backend" not in e2e
  assert "cache-from: type=gha" in e2e
  assert "cache-to:" not in e2e

  assert "push:\n" in cache_triggers
  assert "    branches: [main]\n" in cache_triggers
  assert "pull_request:\n" not in cache_triggers
  assert "outputs: type=cacheonly" in cache_workflow
  assert "cache-to: type=gha,mode=max,ignore-error=true" in cache_workflow
  assert "load: true" not in cache_workflow

  workflows = test_workflow + cache_workflow
  assert workflows.count("DOCKER_BUILD_RECORD_UPLOAD: 'false'") == 2
  assert "actions/checkout@v5" not in workflows
  assert "actions/setup-node@v5" not in workflows
  assert "actions/setup-python@v5" not in workflows

def test_hosted_concurrency_is_scoped_to_the_pull_request():
  workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
    encoding="utf-8"
  )
  assert "group: tests-pr-${{ github.event.pull_request.number }}" in workflow
  assert "github.event.pull_request.head.ref" not in workflow
  assert "cancel-in-progress: true" in workflow
