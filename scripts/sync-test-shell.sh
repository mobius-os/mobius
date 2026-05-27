#!/usr/bin/env bash
# sync-test-shell.sh — Push host frontend source into mobius-test, rebuild
# the shell, restart the container, and report the served bundle hash.
#
# Closes the inner dev loop: edit frontend/src on the host, run this, see
# the new bundle being served at http://localhost:8001/. Without the
# restart, /data/shell/dist/ is fresh but `_static_dir` (resolved at
# uvicorn module load) still points at /app/static/ and the user sees
# the old bundle — see "Shell rebuild + static-dir resolution" in
# CLAUDE.md for the gory details.
#
# Refuses to touch :8000 / the prod `mobius` container.
#
# Usage:
#   scripts/sync-test-shell.sh                  # full loop: sync + rebuild + restart + verify
#   scripts/sync-test-shell.sh --no-restart     # sync + rebuild, skip the restart (fast iter; you accept the static-dir gotcha)
#   scripts/sync-test-shell.sh --check          # just print the served bundle hash, no sync
#
# Override target via env:
#   CONTAINER=mobius-test PORT=8001 scripts/sync-test-shell.sh

set -euo pipefail

CONTAINER="${CONTAINER:-mobius-test}"
PORT="${PORT:-8001}"
BASE="http://localhost:${PORT}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Refuse to talk to prod — same guardrail as live-test.sh.
if [ "$CONTAINER" = "mobius" ] || [ "$PORT" = "8000" ]; then
  echo "FATAL: sync-test-shell.sh refuses to target prod (mobius:8000)." >&2
  echo "       Use CONTAINER=mobius-test PORT=8001 (the defaults)." >&2
  exit 2
fi

NO_RESTART=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-restart) NO_RESTART=1 ;;
    --check)      CHECK_ONLY=1 ;;
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

# Pretty step banner + failure attribution.
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

# Pull the served bundle hash out of index.html. Empty string if nothing is being served.
served_bundle() {
  curl -fsS "${BASE}/" 2>/dev/null \
    | grep -oE 'index-[A-Za-z0-9_-]+\.js' \
    | head -n1 || true
}

# ── --check shortcut: print and exit, no sync, no restart.
if [ "$CHECK_ONLY" = "1" ]; then
  step "checking served bundle on ${CONTAINER} (:${PORT})"
  hash=$(served_bundle)
  if [ -z "$hash" ]; then
    echo "  no bundle reference found in /  (container may be down or unbuilt)"
    exit 1
  fi
  echo "  served: $hash"
  exit 0
fi

# Sanity-check the container is up before we start copying.
step "[0/5] verifying ${CONTAINER} is reachable"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "  container ${CONTAINER} is not running" >&2
  exit 1
fi
before_hash=$(served_bundle)
echo "  before: ${before_hash:-<none>}"

step "[1/5] syncing frontend/src/ → ${CONTAINER}:/data/shell/src/"
# Stream through tar so we preserve perms and don't clobber unrelated
# /data/shell/src files (cleaner than `docker cp` of a directory which
# nests). Pipe-stripped: extract directly into /data/shell/src so the
# tarball's top-level "src" doesn't end up as /data/shell/src/src.
tar -C "${REPO_ROOT}/frontend/src" -cf - . \
  | docker exec -i "$CONTAINER" tar -C /data/shell/src -xpf -

step "[2/5] syncing frontend/public/ → ${CONTAINER}:/data/shell/public/"
tar -C "${REPO_ROOT}/frontend/public" -cf - . \
  | docker exec -i "$CONTAINER" tar -C /data/shell/public -xpf -

# Make sure the mobius user (which runs vite) owns what we just dropped in.
docker exec -u root "$CONTAINER" chown -R mobius:mobius /data/shell/src /data/shell/public

step "[3/5] running /app/scripts/rebuild_shell.sh inside ${CONTAINER}"
docker exec "$CONTAINER" bash /app/scripts/rebuild_shell.sh

if [ "$NO_RESTART" = "1" ]; then
  step "[4/5] SKIPPED restart (--no-restart). /data/shell/dist is fresh, but the running uvicorn still serves the old _static_dir."
else
  step "[4/5] restarting ${CONTAINER} so main.py re-resolves _static_dir"
  docker restart "$CONTAINER" >/dev/null

  step "[4/5] waiting up to 30s for /api/health to return 200"
  for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/health" || true)
    if [ "$code" = "200" ]; then
      echo "  healthy after ${i}s"
      break
    fi
    sleep 1
    if [ "$i" = "30" ]; then
      echo "  health check never returned 200 (last: ${code})" >&2
      exit 1
    fi
  done
fi

step "[5/5] verifying served bundle"
after_hash=$(served_bundle)
echo "  after:  ${after_hash:-<none>}"
if [ -z "$after_hash" ]; then
  echo "  WARN: could not parse bundle hash from / — check ${BASE}/ manually" >&2
  exit 1
fi
if [ -n "$before_hash" ] && [ "$before_hash" = "$after_hash" ]; then
  if [ "$NO_RESTART" = "1" ]; then
    echo "  note: bundle hash unchanged — expected with --no-restart (uvicorn still on baked /app/static/)"
  else
    echo "  note: bundle hash unchanged — either no source change, or vite produced an identical bundle"
  fi
else
  echo "  bundle rotated: ${before_hash:-<none>} → ${after_hash}"
fi
