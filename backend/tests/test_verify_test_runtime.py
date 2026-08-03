import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys

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
  assert "FROM node:24-trixie-slim AS node-runtime" in dockerfile
  assert "agent-browser@0.31.1" in dockerfile
  assert "node:24-trixie-slim sh -c" in preship
  assert "node:22" not in dockerfile
  assert "node:22" not in preship


def test_image_deduplicates_agent_cli_payloads_without_breaking_sdk_contracts():
  dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
  requirements = (ROOT / "backend" / "requirements.txt").read_text(
    encoding="utf-8"
  )
  requirements_lock = (ROOT / "backend" / "requirements.lock").read_text(
    encoding="utf-8"
  )

  requirements_layer = dockerfile[
    dockerfile.index("COPY backend/requirements.txt backend/requirements.lock ./"):
    dockerfile.index("# openai-codex Python SDK:")
  ]
  assert (
    "pip install --no-cache-dir --require-hashes -r requirements.lock"
    in requirements_layer
  )
  assert "claude-agent-sdk==0.2.128" in requirements
  assert "claude-agent-sdk==0.2.128" in requirements_lock
  assert (
    'Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"'
    in requirements_layer
    and "unlink(missing_ok=True)" in requirements_layer
    and "assert not p.exists()" in requirements_layer
  )

  codex_layer = dockerfile[
    dockerfile.index("# openai-codex Python SDK:"):
    dockerfile.index("# Capture each installed agent CLI")
  ]
  assert "pip install --no-cache-dir --no-deps" in codex_layer
  assert "pip install --no-cache-dir 'openai-codex-cli-bin==0.144.4'" in codex_layer
  assert 'rm -rf "${_codex_cli_bin}/bin"' in codex_layer
  assert 'ln -s /usr/local/bin/codex "${_codex_cli_bin}/bin/codex"' in codex_layer
  assert "bundled_codex_path().samefile" in codex_layer
  assert "pip check" in codex_layer
  assert "declared cli-bin package is retained for SDK compatibility" in requirements


def test_production_image_keeps_persistent_sso_checkouts_bootable():
  dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
  fingerprint = (
    ROOT / "scripts" / "test-image-fingerprint.sh"
  ).read_text(encoding="utf-8")
  requirements = (ROOT / "backend" / "requirements.txt").read_text(
    encoding="utf-8"
  )
  requirements_lock = (ROOT / "backend" / "requirements.lock").read_text(
    encoding="utf-8"
  )
  shim_root = ROOT / "backend" / "legacy_runtime"
  probe_path = shim_root / "verify_jose.py"
  probe = probe_path.read_text(encoding="utf-8")
  subprocess.run(
    [sys.executable, "-B", str(probe_path)],
    check=True,
    capture_output=True,
    text=True,
    env={**os.environ, "PYTHONPATH": str(shim_root)},
  )

  for unsafe_dependency in ("python-jose", "ecdsa"):
    assert unsafe_dependency not in requirements.lower()
    assert unsafe_dependency not in requirements_lock.lower()
  assert (
    "COPY backend/legacy_runtime/jose/ "
    "/usr/local/lib/python3.12/site-packages/jose/"
  ) in dockerfile
  assert dockerfile.index("> /app/test-image-fingerprint") < dockerfile.index(
    "COPY backend/legacy_runtime/jose/ "
    "/usr/local/lib/python3.12/site-packages/jose/"
  )
  assert "from jose import JWTError, jwt" in probe
  assert "jwt.encode" in probe
  assert "jwt.decode" in probe
  assert "except JWTError:" in probe
  assert "backend/legacy_runtime/jose/__init__.py" in fingerprint
  assert "backend/legacy_runtime/verify_jose.py" in fingerprint
  assert "COPY backend/legacy_runtime/verify_jose.py" in dockerfile
  assert (
    "COPY backend/legacy_runtime/ "
    "/tmp/test-image-inputs/backend/legacy_runtime/"
  ) in dockerfile
  assert "python /tmp/verify-legacy-jose.py" in dockerfile


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


def test_managed_release_boot_uses_the_baked_updater():
  entrypoint = (
    ROOT / "backend" / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")

  assert 'if [ -n "${MOBIUS_PLATFORM_RELEASE_REF:-}" ]; then' in entrypoint
  assert "_platform_reconciler_backend=/app/platform-baked/backend" in entrypoint
  assert '_platform_reconciler_prefix="env PYTHONDONTWRITEBYTECODE=1"' in entrypoint
  assert "cd '$_platform_reconciler_backend'" in entrypoint


def test_managed_release_boot_falls_back_when_exact_target_is_not_integrated():
  entrypoint = (
    ROOT / "backend" / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")
  reconcile = entrypoint.index("platform_update.reconcile_clone_sync()")
  guard = entrypoint.index("platform_update.boot_guard_sync()", reconcile)
  proof = entrypoint.index("platform_update.managed_release_ready_sync()", guard)
  fallback = entrypoint.index("_platform_use_baked", proof)
  markers = entrypoint.index(
    "# Write the markers only after the managed exact-release gate",
    fallback,
  )

  assert reconcile < guard < proof < fallback < markers
  assert "/data/platform is preserved for recovery" in entrypoint


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
  assert 'git clone --quiet --no-local "$ROOT" "$snapshot_dir"' in runner
  assert '--project-directory "$snapshot_dir"' in runner
  assert 'cd "$snapshot_dir"' in runner
  assert 'MOBIUS_LOCAL_E2E_WORKERS:-1' in runner
  assert 'MOBIUS_LOCAL_E2E_WORKERS must be a positive integer' in runner
  assert '"$snapshot_dir/node_modules/.bin/playwright" test "$@" --workers="$e2e_workers"' in runner
  assert "Local E2E artifacts retained at:" in runner
  assert 'compose logs --no-color app caddy fake-tandoor' in runner
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


def test_manual_and_pull_request_runs_cover_suites_and_cutover_image():
  test_workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
    encoding="utf-8"
  )
  image_workflow = (
    ROOT / ".github" / "workflows" / "external-recovery-image.yml"
  ).read_text(encoding="utf-8")
  assert not (ROOT / ".github" / "workflows" / "main-image.yml").exists()
  assert not (
    ROOT / ".github" / "workflows" / "compatibility-bootstrap.yml"
  ).exists()
  assert not (ROOT / "scripts" / "compatibility_bootstrap.py").exists()
  test_triggers = test_workflow.split("\npermissions:\n", 1)[0]
  image_triggers = image_workflow.split("\npermissions:\n", 1)[0]
  backend = test_workflow.split("\n  backend:\n", 1)[1].split(
    "\n  frontend-unit:\n", 1,
  )[0]
  e2e = test_workflow.split("\n  e2e:\n", 1)[1]

  assert "pull_request:\n" in test_triggers
  assert "workflow_dispatch:\n" in test_triggers
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

  assert "push:\n" in image_triggers
  assert "    branches: [stack/external-recovery-v1]\n" in image_triggers
  assert "schedule:\n" not in image_triggers
  assert "workflow_dispatch:\n" not in image_triggers
  assert "pull_request:\n" not in image_triggers
  assert "packages: write" in image_workflow
  assert "push: true" in image_workflow
  immutable_tag = (
    "tags: ${{ env.MOBIUS_IMAGE_REPOSITORY }}:sha-${{ github.sha }}-"
    "run-${{ github.run_id }}-attempt-${{ github.run_attempt }}"
  )
  prepublish = image_workflow.index("/internal/core-releases/prepublish")
  identity_head_guard = image_workflow.index(
    'test "$release_head" = "$GITHUB_SHA"'
  )
  gate_head_guard_contract = (
    "          current_sha=$(git ls-remote --exit-code origin "
    '"$MOBIUS_PLATFORM_RELEASE_REF" | awk \'NR == 1 { print $1 }\')\n'
    '          test "$current_sha" = "$GITHUB_SHA"\n'
    "          while true; do\n"
    "            http_status=$(curl"
  )
  gate_head_guard = image_workflow.index(gate_head_guard_contract)
  registry_login = image_workflow.index("docker/login-action@")
  build = image_workflow.index("docker/build-push-action@")
  bind = image_workflow.index("/internal/core-releases/bind")
  redeploy = image_workflow.index("/internal/core-releases/postpublish")
  assert immutable_tag in image_workflow
  assert identity_head_guard < gate_head_guard < prepublish
  assert image_workflow.count('test "$release_head" = "$GITHUB_SHA"') == 1
  assert image_workflow.count('test "$current_sha" = "$GITHUB_SHA"') == 1
  assert 'echo "release_head=$release_head"' not in image_workflow
  assert prepublish < registry_login < build < bind < redeploy
  between_bind_and_redeploy = image_workflow[bind:redeploy]
  assert "docker buildx imagetools create" not in between_bind_and_redeploy
  assert "finalization starts" in between_bind_and_redeploy
  assert "completed_replay" in image_workflow
  assert "steps.gate.outputs.mode != 'completed_replay'" in image_workflow
  assert "MOBIUS_RECOVERY_IMAGE_REPOSITORY" in image_workflow
  assert "steps.gate.outputs.current_recovery_digest" in image_workflow
  assert "steps.gate.outputs.current_recovery_sha" in image_workflow
  assert "steps.gate.outputs.frozen_recovery_digest" in image_workflow
  assert "group: mobius-core-image-publication" in image_workflow
  assert "cancel-in-progress: false" in image_workflow
  assert "MOBIUS_EXTERNAL_RECOVERY_RELEASE_ENABLED == 'true'" in image_workflow
  assert "environment: external-recovery-release" in image_workflow
  assert "RECOVERY_CUTOVER_PREREQUISITE_SHA:" in image_workflow
  assert "RECOVERY_CUTOVER_PREREQUISITE_DIGEST:" in image_workflow
  assert 'test "$GITHUB_REPOSITORY" = mobius-os/mobius' in image_workflow
  assert 'test "$GITHUB_EVENT_NAME" = push' in image_workflow
  assert 'test "$GITHUB_REF" = "$MOBIUS_PLATFORM_RELEASE_REF"' in image_workflow
  assert image_workflow.count(
    "git fetch --quiet --no-tags origin refs/heads/main"
  ) == 4
  assert image_workflow.count(
    'git merge-base --is-ancestor "$RECOVERY_CUTOVER_PREREQUISITE_SHA" "$main_sha"'
  ) == 4
  assert image_workflow.count(
    'git cat-file -e "$main_sha:backend/recovery/recoveryd.py"'
  ) == 4
  assert "git rev-list --first-parent --reverse" in image_workflow
  assert 'git rev-parse "${removal_root_sha}^1"' in image_workflow
  assert image_workflow.count(
    "Refusing a removal lineage already contained by main"
  ) == 4
  assert image_workflow.count(
    'git merge-base --is-ancestor "$REMOVAL_ROOT_SHA" "$main_sha"'
  ) == 3
  assert (
    'git merge-base --is-ancestor "$removal_root_sha" "$main_sha"'
    in image_workflow
  )
  assert "Refusing to bind or publish a stale unbound release" in image_workflow
  assert 'if [ "$RELEASE_MODE" = resume_cutover ]' in image_workflow
  assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in image_workflow
  assert "persist-credentials: false" in image_workflow
  assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in image_workflow
  assert "docker/login-action@dbcb813823bdd20940b903addbd779551569679f" in image_workflow
  assert "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a" in image_workflow
  assert "ghcr.io/mobius-os/mobius:daily" not in image_workflow
  assert '--tag "$MOBIUS_IMAGE_REPOSITORY:main"' not in image_workflow
  assert '--tag "$MOBIUS_IMAGE_REPOSITORY:daily"' not in image_workflow
  assert '--tag "$MOBIUS_RELEASE_CHANNEL"' not in image_workflow
  assert "docker buildx imagetools create" not in image_workflow
  assert "normal_release" not in image_workflow
  assert "prewrite" not in image_workflow
  assert "cache-to: type=gha,mode=max,ignore-error=true" in image_workflow
  assert "load: true" not in image_workflow

  workflows = test_workflow + image_workflow
  assert workflows.count("DOCKER_BUILD_RECORD_UPLOAD: 'false'") == 2
  assert "actions/checkout@v5" not in workflows
  assert "actions/setup-node@v5" not in workflows
  assert "actions/setup-python@v5" not in workflows

def test_hosted_concurrency_is_scoped_to_the_pull_request():
  workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
    encoding="utf-8"
  )
  assert (
    "group: tests-pr-${{ github.event.pull_request.number || github.ref }}"
    in workflow
  )
  # A required-check merge queue needs the suite to run on merge_group too,
  # where pull_request.number is null (hence the github.ref fallback above).
  assert "merge_group:" in workflow
  assert "github.event.pull_request.head.ref" not in workflow
  assert "cancel-in-progress: true" in workflow
