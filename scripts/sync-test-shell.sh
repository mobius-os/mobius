#!/usr/bin/env bash
# sync-test-shell.sh — Push host frontend source into mobius-test, rebuild the
# served platform frontend, and report the served bundle hash.
#
# Usage:
#   scripts/sync-test-shell.sh          # sync + rebuild + verify
#   scripts/sync-test-shell.sh --check  # just print the served bundle hash
#
# Override target via env:
#   CONTAINER=mobius-test PORT=8001 scripts/sync-test-shell.sh

set -euo pipefail

CONTAINER="${CONTAINER:-mobius-test}"
PORT="${PORT:-8001}"
BASE="http://localhost:${PORT}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_IN_CONTAINER="/data/platform/frontend"

# Refuse to talk to prod — same guardrail as live-test.sh.
if [ "$CONTAINER" = "mobius" ] || [ "$PORT" = "8000" ]; then
  echo "FATAL: sync-test-shell.sh refuses to target prod (mobius:8000)." >&2
  echo "       Use CONTAINER=mobius-test PORT=8001 (the defaults)." >&2
  exit 2
fi

CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help)
      sed -n '1,/^set -euo pipefail/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

CURRENT_STEP=""
on_err() {
  local rc=$?
  if [ -n "$CURRENT_STEP" ]; then
    echo "FAILED at step: $CURRENT_STEP (exit $rc)" >&2
  else
    echo "FAILED (exit $rc)" >&2
  fi
  exit "$rc"
}
trap on_err ERR

step() {
  CURRENT_STEP="$1"
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$1"
}

# Pull the served bundle hash out of index.html. Empty string if nothing is served.
served_bundle() {
  curl -fsSL "${BASE}/shell/" 2>/dev/null \
    | grep -oE 'index-[A-Za-z0-9_-]+\.js' \
    | head -n1 || true
}

if [ "$CHECK_ONLY" = "1" ]; then
  step "checking served bundle on ${CONTAINER} (:${PORT})"
  hash=$(served_bundle)
  if [ -z "$hash" ]; then
    echo "  no bundle reference found in /shell/ (container may be down or unbuilt)"
    exit 1
  fi
  echo "  served: $hash"
  exit 0
fi

step "[0/4] verifying ${CONTAINER} is reachable"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "  container ${CONTAINER} is not running" >&2
  exit 1
fi
before_hash=$(served_bundle)
echo "  before: ${before_hash:-<none>}"

step "[1/4] syncing frontend/src/ -> ${CONTAINER}:${FRONTEND_IN_CONTAINER}/src/"
docker exec -u mobius "$CONTAINER" mkdir -p "${FRONTEND_IN_CONTAINER}/src"
tar -C "${REPO_ROOT}/frontend/src" -cf - . \
  | docker exec -i -u mobius "$CONTAINER" tar -C "${FRONTEND_IN_CONTAINER}/src" -xpf -

step "[2/4] syncing frontend/public/ -> ${CONTAINER}:${FRONTEND_IN_CONTAINER}/public/"
docker exec -u mobius "$CONTAINER" mkdir -p "${FRONTEND_IN_CONTAINER}/public"
tar -C "${REPO_ROOT}/frontend/public" -cf - . \
  | docker exec -i -u mobius "$CONTAINER" tar -C "${FRONTEND_IN_CONTAINER}/public" -xpf -

step "[3/4] rebuilding ${FRONTEND_IN_CONTAINER}"
docker exec -u mobius "$CONTAINER" bash /app/scripts/rebuild_shell.sh

step "[4/4] verifying served bundle"
after_hash=$(served_bundle)
echo "  after:  ${after_hash:-<none>}"
if [ -z "$after_hash" ]; then
  echo "  WARN: could not parse bundle hash from /shell/ — check ${BASE}/shell/ manually" >&2
  exit 1
fi
if [ -n "$before_hash" ] && [ "$before_hash" = "$after_hash" ]; then
  echo "  note: bundle hash unchanged — either no source change, or vite produced an identical bundle"
else
  echo "  bundle rotated: ${before_hash:-<none>} -> ${after_hash}"
fi
