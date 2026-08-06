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
        *) return 99 ;;
      esac
    }
    CONTAINER=mobius
    MOBIUS_IMAGE=mobius:prod
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


def test_compose_image_default_is_stable():
  setup = textwrap.dedent("""\
    unset MOBIUS_IMAGE
    info() { :; }
    docker() {
      case "$*" in
        *"{{.Image}}"*) printf 'sha256:old-image\\n' ;;
        *"{{.Config.Image}}"*) printf 'mobius:selfhost-old-sha\\n' ;;
      esac
    }
    CONTAINER=mobius
  """)
  harness = setup + _identity_block() + "printf '%s\\n' \"$IMAGE_TAG\"\n"
  result = subprocess.run(
    ["bash", "-c", harness], capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout == "mobius\n"


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
