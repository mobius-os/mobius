#!/usr/bin/env bash
# test.sh — single entrypoint for Möbius's two test layers.
#
# Backend tests run inside the mobius-test Docker image (pytest, real
# environment with esbuild + node + pip deps); frontend tests run on
# the host (Playwright driving the system Chrome channel locally).
# Picking the right invocation by hand is a coin-flip and the slow
# suite often wins by default — this wrapper makes the choice explicit
# and visible up front.
#
# Usage: see --help.

set -euo pipefail

# ---- Config -----------------------------------------------------------------
PROJECT_DIR="/home/hmzmrzx/projects/mobius"
TEST_IMAGE="mobius-test:ci"
# Slow Codex SDK tests — excluded by --fast. The cost is real (each
# SDK contract test spins up a Thread/TurnHandle dance) and they cover
# a narrow surface compared to the rest of the suite.
SLOW_TESTS=(
  "tests/test_codex_sdk_runner.py"
  "tests/test_codex_sdk_contract.py"
  "tests/test_codex_provider.py"
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
  --backend   Pytest in the mobius-test Docker image.        (~70s, 362 tests)
  --frontend  Playwright on the host (system Chrome locally). (~30s)
  --all       Backend first, then frontend. DEFAULT.         (~100s)
  --fast      Backend only, skipping the slow Codex SDK tests.
              Covers ~80% of risk in ~20s — good for tight inner loops.
  --help      Show this message.

Backend runs first under --all because it is slower but catches more
(import/regression breaks early) and the frontend suite depends on a
healthy backend image anyway.

Examples:
  scripts/test.sh              # full sweep
  scripts/test.sh --fast       # quick sanity check during edits
  scripts/test.sh --frontend   # just re-run Playwright after a UI tweak
EOF
}

# Backend preflight: the test image must already be built, otherwise
# every pytest invocation would silently rebuild (or fail cryptically).
# We surface the right next command instead of letting `docker compose
# run` chew through a 3-minute build with no explanation.
check_backend_prereqs() {
  if ! docker image inspect "${TEST_IMAGE}" >/dev/null 2>&1; then
    die "Image ${TEST_IMAGE} not found. Build it first:
    cd ${PROJECT_DIR} && docker compose -p mobius-test -f docker-compose.test.yml build"
  fi
}

# Frontend preflight: Playwright needs (a) the package installed in
# node_modules, (b) a working Chrome binary. We test both up front
# because the failure modes otherwise are "Cannot find module" vs
# "Executable doesn't exist" thrown deep inside the test runner.
check_frontend_prereqs() {
  if [ ! -x "${PROJECT_DIR}/node_modules/.bin/playwright" ]; then
    die "Playwright not installed. Run:
    cd ${PROJECT_DIR} && npm install"
  fi
  # `channel: 'chrome'` (per playwright.config.mjs) needs a real Chrome
  # on PATH. If neither google-chrome nor playwright's bundled browser
  # is available, point the user at the install command.
  if ! command -v google-chrome >/dev/null 2>&1 \
       && ! command -v google-chrome-stable >/dev/null 2>&1 \
       && [ ! -d "${HOME}/.cache/ms-playwright" ]; then
    die "No Chrome found on PATH and no Playwright browsers cached. Run:
    cd ${PROJECT_DIR} && npx playwright install chrome"
  fi
}

# ---- Suite runners ----------------------------------------------------------
run_backend() {
  local mode="${1:-full}"   # "full" or "fast"
  check_backend_prereqs

  local -a pytest_args=("--tb=short" "-q")
  local summary
  if [ "${mode}" = "fast" ]; then
    for f in "${SLOW_TESTS[@]}"; do
      pytest_args+=("--ignore=${f}")
    done
    summary="pytest (fast — skipping ${#SLOW_TESTS[@]} slow files), expected ~20s"
  else
    summary="pytest (full backend suite, 362 tests), expected ~70s"
  fi

  log "backend: ${summary}"
  if (cd "${PROJECT_DIR}" && docker compose -p mobius-test \
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

  log "frontend: Playwright (8 spec files, system Chrome channel), expected ~30s"
  if (cd "${PROJECT_DIR}" && npx playwright test); then
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
  local mode="all"
  if [ $# -gt 1 ]; then
    usage >&2
    die "at most one flag accepted, got: $*"
  fi
  case "${1:-}" in
    --backend)            mode="backend" ;;
    --frontend)           mode="frontend" ;;
    --all|"")             mode="all" ;;
    --fast)               mode="fast" ;;
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
