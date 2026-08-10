"""The deploy must preflight and roll back the same image Compose deploys."""

from pathlib import Path
import subprocess
import textwrap


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"


def _read() -> str:
  return SCRIPT.read_text(encoding="utf-8")


def _identity_block() -> str:
  source = _read()
  start = source.index("# ── deploy image identity\n")
  end = source.index("# ── end deploy image identity", start)
  return source[start:end]


def _function_source(name: str, following_comment: str) -> str:
  source = _read()
  start = source.index(f"{name}() {{")
  end = source.index(following_comment, start)
  return source[start:end]


def test_mismatched_running_reference_migrates_to_compose_image():
  setup = textwrap.dedent("""\
    info() { :; }
    docker() {
      case "$*" in
        *"{{.Image}}"*) printf 'sha256:old-image\\n' ;;
        *"{{.Config.Image}}"*) printf 'mobius:selfhost-old-sha\\n' ;;
        *"config --images app"*) printf 'mobius:prod\\n' ;;
        *) return 99 ;;
      esac
    }
    CONTAINER=mobius
    COMPOSE_ARGS=(-p mobius)
    MOBIUS_IMAGE=mobius:prod
    SKIP_BUILD=0
  """)
  assertions = textwrap.dedent("""\
    compose_value=$(sh -c 'printf %s "$MOBIUS_IMAGE"')
    printf 'previous=%s\\nrunning=%s\\ntarget=%s\\ncompose=%s\\n' \
      "$PREV_IMAGE" "$RUNNING_IMAGE_REF" "$IMAGE_TAG" "$compose_value"
  """)
  harness = setup + _identity_block() + assertions

  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout == (
    "previous=sha256:old-image\n"
    "running=mobius:selfhost-old-sha\n"
    "target=mobius:prod\n"
    "compose=mobius:prod\n"
  )


def test_compose_image_comes_from_rendered_prod_config():
  setup = textwrap.dedent("""\
    unset MOBIUS_IMAGE
    info() { :; }
    docker() {
      case "$*" in
        *"{{.Image}}"*) printf 'sha256:old-image\\n' ;;
        *"{{.Config.Image}}"*) printf 'mobius:selfhost-old-sha\\n' ;;
        *"config --images app"*) printf 'mobius\\n' ;;
      esac
    }
    CONTAINER=mobius
    COMPOSE_ARGS=(-p mobius)
    SKIP_BUILD=0
  """)
  harness = setup + _identity_block() + "printf '%s\\n' \"$IMAGE_TAG\"\n"
  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout == "mobius\n"


def test_test_target_uses_its_rendered_image_not_the_prod_default(tmp_path):
  docker_log = tmp_path / "docker.log"
  setup = textwrap.dedent(f"""\
    unset MOBIUS_IMAGE
    info() {{ :; }}
    docker() {{
      printf '%s\\n' "$*" >> {str(docker_log)!r}
      case "$*" in
        *"{{{{.Image}}}}"*) printf 'sha256:test-image\\n' ;;
        *"{{{{.Config.Image}}}}"*) printf 'mobius-test:ci\\n' ;;
        *"config --images app"*) printf 'mobius-test:ci\\n' ;;
      esac
    }}
    CONTAINER=mobius-test
    COMPOSE_ARGS=(-p mobius-test -f docker-compose.test.yml)
    SKIP_BUILD=0
  """)
  harness = setup + _identity_block() + "printf '%s\\n' \"$IMAGE_TAG\"\n"

  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout == "mobius-test:ci\n"
  assert "compose -p mobius-test -f docker-compose.test.yml config --images app" in (
    docker_log.read_text(encoding="utf-8").splitlines()
  )


def test_skip_build_refuses_a_stale_compose_tag():
  setup = textwrap.dedent("""\
    info() { :; }
    fail() { printf '%s\\n' "$1" >&2; }
    docker() {
      case "$*" in
        *"{{.Image}}"*) printf 'sha256:serving\\n' ;;
        *"{{.Config.Image}}"*) printf 'mobius:selfhost-old-sha\\n' ;;
        *"config --images app"*) printf 'mobius\\n' ;;
        *"image inspect"*) printf 'sha256:stale\\n' ;;
      esac
    }
    CONTAINER=mobius
    COMPOSE_ARGS=(-p mobius)
    SKIP_BUILD=1
  """)

  result = subprocess.run(
    ["bash", "-c", setup + _identity_block()],
    capture_output=True,
    text=True,
  )

  assert result.returncode == 1
  assert "--skip-build target mobius is not the image serving" in result.stderr


def test_prod_image_override_loads_from_worktree_env(tmp_path):
  (tmp_path / ".env").write_text("MOBIUS_IMAGE=mobius:from-env\n", encoding="utf-8")
  env_reader = _function_source("env_value_from_file", "ensure_prod_env()")
  resolver = _function_source(
    "resolve_prod_image_override", "# ── end prod environment resolution",
  )
  harness = env_reader + resolver + textwrap.dedent(f"""\
    unset MOBIUS_IMAGE
    TARGET=prod
    REPO_ROOT={str(tmp_path)!r}
    canonical_env_path() {{ return 1; }}
    info() {{ :; }}
    resolve_prod_image_override
    printf '%s\\n' "$MOBIUS_IMAGE"
  """)

  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout == "mobius:from-env\n"


def test_scratch_preflight_runs_the_compose_image_not_the_old_reference():
  source = _read()
  start = source.index("# ── preflight: boot the new image")
  end = source.index("# ── step 2: recreate container", start)
  preflight = source[start:end]

  assert '"$IMAGE_TAG" >/dev/null' in preflight
  assert "RUNNING_IMAGE_REF" not in preflight


def test_rollback_retags_previous_id_then_recreates_compose_image(tmp_path):
  docker_log = tmp_path / "docker.log"
  rollback = _function_source(
    "attempt_rollback", "# Wait for a live-container probe",
  )
  harness = rollback + textwrap.dedent(f"""\
    warn() {{ :; }}
    intent() {{ :; }}
    fail() {{ printf 'FAIL %s\\n' "$1" >&2; }}
    ok() {{ :; }}
    external_prod_caddy_running() {{ return 1; }}
    docker() {{
      printf '%s\\n' "$*" >> {str(docker_log)!r}
      if [ "$1" = exec ]; then printf '200'; fi
      return 0
    }}
    PREV_IMAGE=sha256:old-image
    IMAGE_TAG=mobius:prod
    CONTAINER=mobius
    CUTOVER_WAIT_SECONDS=1
    INTERNAL_BASE=http://localhost:8000
    COMPOSE_ARGS=(-p mobius)
    attempt_rollback
  """)

  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  calls = docker_log.read_text(encoding="utf-8").splitlines()
  assert calls[0] == "tag sha256:old-image mobius:prod"
  assert calls[1] == "compose -p mobius up -d --force-recreate"
  assert calls[2].startswith("exec mobius sh -c curl ")
