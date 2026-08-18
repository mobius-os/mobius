#!/usr/bin/env bash
# test.sh — single entrypoint for Möbius's two test layers.
#
# Fast backend tests run in the checkout's hermetic host test runtime. The full
# backend suite uses the mobius-test Docker image, and browser E2E is an
# explicit host-only opt-in through the disposable Playwright runner.
# Picking the right invocation by hand is a coin-flip and the slow
# suite often wins by default — this wrapper makes the choice explicit
# and visible up front.
#
# Usage: see --help.

set -euo pipefail

# ---- Config -----------------------------------------------------------------
# Resolve the checkout that owns this script.  A fixed path silently tested the
# primary clone when this wrapper was invoked from a linked worktree, which is
# exactly where agents do most review/fix-forward work.
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
_checkout_name="$(basename "$PROJECT_DIR" | tr -cs '[:alnum:]_.-' '-')"
_checkout_id="$(printf '%s' "$PROJECT_DIR" | cksum | cut -d' ' -f1)"
TEST_PROJECT="${MOBIUS_TEST_PROJECT:-mobius-test-${_checkout_name}-${_checkout_id}}"
TEST_IMAGE="${MOBIUS_IMAGE:-mobius-test:ci}"
# A deliberately small cross-section for the iteration loop. These exercise
# boot/readiness, auth/config, app compilation, source inspection, the test
# safety boundary, and the task-owned commit primitive. The complete backend
# suite remains the required --backend/--all closeout gate.
FAST_TESTS=(
  "tests/test_readiness.py"
  "tests/test_db_migrations.py::test_applied_legacy_schema_migration_is_immutable"
  "tests/test_auth_helpers.py"
  "tests/test_app_compile_contract.py"
  "tests/test_source_status.py"
  "tests/test_verify_test_runtime.py"
  "tests/test_pm_commit.py"
  "tests/test_test_entrypoint.py"
)

BACKEND_STATUS="SKIP"
FRONTEND_STATUS="SKIP"

# ---- Helpers ----------------------------------------------------------------
log()  { printf '[%(%H:%M:%S)T] %s\n' -1 "$*"; }
die()  { log "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: scripts/test.sh [--backend | --frontend | --all | --fast | --help]

Möbius has two test layers; this wrapper runs either or both.

Flags (mutually exclusive):
  --backend   Full pytest suite in the mobius-test image.    (several minutes)
  --frontend  Disposable host-only Playwright stack.         (several minutes)
  --all       Full backend, then disposable Playwright.      (explicit, expensive)
  --fast      Small hermetic host contract suite (normally seconds).
              No Docker image needed; DEFAULT. Use --backend before landing.
  --help      Show this message.

Backend runs first under --all because it is slower but catches more
(import/regression breaks early) and the frontend suite depends on a
healthy backend image anyway.

Examples:
  scripts/test.sh              # cheap backend sanity check
  scripts/test.sh --fast       # quick sanity check during edits
  scripts/test.sh --frontend   # explicit isolated browser run
EOF
}

# Backend preflight: the test image must already be built, otherwise
# every pytest invocation would silently rebuild (or fail cryptically).
# We surface the right next command instead of letting `docker compose
# run` chew through a 3-minute build with no explanation.
check_backend_prereqs() {
  if ! command -v docker >/dev/null 2>&1; then
    die "Docker is not available in this runtime. Use --fast for local iteration;
    run --backend on a Docker-capable host or rely on the complete CI gate."
  fi
  if ! docker compose version >/dev/null 2>&1; then
    die "Docker Compose is not available. Install the compose plugin before --backend."
  fi
  if ! docker image inspect "${TEST_IMAGE}" >/dev/null 2>&1; then
    die "Image ${TEST_IMAGE} not found. Build it first:
    cd ${PROJECT_DIR} && docker compose -p ${TEST_PROJECT} -f docker-compose.test.yml build"
  fi

  local expected actual
  expected="$("${PROJECT_DIR}/scripts/test-image-fingerprint.sh")"
  actual="$("${PROJECT_DIR}/scripts/docker-probe.sh" --timeout 15 -- \
    --entrypoint sh "${TEST_IMAGE}" -c \
    'test -r /app/test-image-fingerprint && cat /app/test-image-fingerprint' \
    2>/dev/null || true)"
  if [ "${actual}" != "${expected}" ]; then
    if [ "${MOBIUS_ALLOW_UNVERIFIED_TEST_IMAGE:-0}" = "1" ]; then
      log "backend: WARNING — using unverified image ${TEST_IMAGE} by explicit override"
      return
    fi
    die "Image ${TEST_IMAGE} is stale or predates dependency fingerprints.
    Expected: ${expected}
    Found:    ${actual:-missing}
    Rebuild explicitly (the test runner never rebuilds):
    cd ${PROJECT_DIR} && docker compose -p ${TEST_PROJECT} -f docker-compose.test.yml build
    If the baked dependencies are known to be unchanged, set
    MOBIUS_ALLOW_UNVERIFIED_TEST_IMAGE=1 for this run."
  fi
}

# Browser preflight. The disposable runner performs the Docker, browser, clean
# revision, and runtime-identity checks itself.
check_frontend_prereqs() {
  if [ ! -x "${PROJECT_DIR}/node_modules/.bin/playwright" ]; then
    die "Playwright not installed. Run:
    cd ${PROJECT_DIR} && npm ci"
  fi
}

# ---- Suite runners ----------------------------------------------------------
run_backend() {
  local mode="${1:-full}"   # "full" or "fast"
  local -a pytest_args=("--tb=short" "-q")
  local summary
  if [ "${mode}" = "fast" ]; then
    summary="hermetic host contract suite (${#FAST_TESTS[@]} files)"
  else
    summary="pytest (full backend suite, currently ~2,450 tests)"
  fi

  log "backend: ${summary}"
  if [ "${mode}" = "fast" ]; then
    if (cd "${PROJECT_DIR}" && scripts/wt-pytest.sh "${FAST_TESTS[@]}" "${pytest_args[@]}"); then
      BACKEND_STATUS="PASS"
      log "backend: PASS"
    else
      BACKEND_STATUS="FAIL"
      log "backend: FAIL"
    fi
    return
  fi

  check_backend_prereqs
  if (cd "${PROJECT_DIR}" && docker compose -p "${TEST_PROJECT}" \
      -f docker-compose.test.yml run --rm --no-deps \
      --entrypoint python pytest -m pytest "${pytest_args[@]}" tests/); then
    BACKEND_STATUS="PASS"
    log "backend: PASS"
  else
    BACKEND_STATUS="FAIL"
    log "backend: FAIL"
  fi
}

run_frontend() {
  check_frontend_prereqs

  log "frontend: disposable Playwright stack (host-only, one worker)"
  if (cd "${PROJECT_DIR}" && scripts/playwright-local.sh --allow-local-e2e); then
    FRONTEND_STATUS="PASS"
    log "frontend: PASS"
  else
    FRONTEND_STATUS="FAIL"
    log "frontend: FAIL"
  fi
}

# Print the final scorecard. Single source of truth for the exit code
# of an --all run — keeps the "one suite failed but we exited 0" bug
# out of reach.
summarize() {
  echo
  echo "==== SUMMARY ===="
  printf '  backend:  %s\n' "${BACKEND_STATUS}"
  printf '  frontend: %s\n' "${FRONTEND_STATUS}"
  echo "================="
  if [ "${BACKEND_STATUS}" = "FAIL" ] || [ "${FRONTEND_STATUS}" = "FAIL" ]; then
    exit 1
  fi
}

# ---- Main -------------------------------------------------------------------
main() {
  local mode="fast"
  if [ $# -gt 1 ]; then
    usage >&2
    die "at most one flag accepted, got: $*"
  fi
  case "${1:-}" in
    --backend)            mode="backend" ;;
    --frontend)           mode="frontend" ;;
    --all)                mode="all" ;;
    --fast|"")            mode="fast" ;;
    --help|-h)            usage; exit 0 ;;
    *)                    usage >&2; die "unknown flag: $1" ;;
  esac

  case "${mode}" in
    backend)   run_backend full ;;
    frontend)  run_frontend ;;
    fast)      run_backend fast ;;
    all)       run_backend full; run_frontend ;;
  esac
  summarize
}

main "$@"
