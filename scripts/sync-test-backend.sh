#!/usr/bin/env bash
# sync-test-backend.sh — Push host backend source into a mobius-TEST container's
# served platform layer, restart, and report. The backend counterpart to
# sync-test-shell.sh.
#
# Why this exists: the mobius-test `app` service serves BAKED image code — only
# the `pytest` service bind-mounts backend/app. And a bind-mount onto /app/app
# does NOT work anyway, because the entrypoint serves the whole-repo clone from
# /data/platform/backend (uvicorn runs `cd /data/platform/backend && import
# app.main`). So to test a backend edit without a full image rebuild, copy the
# source into the served tree /data/platform/backend/{app,scripts} and restart
# so uvicorn re-imports it. This is the exact manual loop (docker cp + restart)
# that costs a cycle every time it is rediscovered.
#
# REFUSES to touch prod (mobius / :8000) — same guardrail as sync-test-shell.sh.
# This must NEVER overwrite the live owner's served backend.
#
# Usage:
#   scripts/sync-test-backend.sh                 # sync backend → restart → verify
#   scripts/sync-test-backend.sh --no-restart    # sync only (uvicorn keeps the old modules)
#   CONTAINER=mobius-test-myslug PORT=8039 scripts/sync-test-backend.sh
#
# In an isolated per-slug session, pass the slug's container/port explicitly
# (scripts/mobius-session.sh exports MOBIUS_CONTAINER for you):
#   CONTAINER="$MOBIUS_CONTAINER" PORT="$TEST_PORT" scripts/sync-test-backend.sh

set -euo pipefail

CONTAINER="${CONTAINER:-${MOBIUS_CONTAINER:-mobius-test}}"
PORT="${PORT:-${TEST_PORT:-8001}}"
BASE="http://localhost:${PORT}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Hard prod-refusal: never sync into the live owner's backend.
if [ "$CONTAINER" = "mobius" ] || [ "$PORT" = "8000" ]; then
  echo "FATAL: sync-test-backend.sh refuses to target prod (mobius:8000)." >&2
  echo "       It overwrites the served backend; that is a test-only operation." >&2
  exit 2
fi

NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --no-restart) NO_RESTART=1 ;;
    -h|--help) sed -n '1,/^set -euo pipefail/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$1"; }

step "[0/4] verifying ${CONTAINER} is reachable"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "  container ${CONTAINER} is not running" >&2
  exit 1
fi

# Stream backend/app + backend/scripts into the served platform layer. tar runs
# as root (docker exec default) so it can overwrite the root-owned recovery
# island files too; we chown back to mobius and the entrypoint re-applies the
# protected-file perms on the restart below. __pycache__/.pyc are skipped so a
# stale host cache can't shadow the new source.
step "[1/4] syncing backend/app → ${CONTAINER}:/data/platform/backend/app/"
docker exec "$CONTAINER" mkdir -p /data/platform/backend/app /data/platform/backend/scripts
tar -C "${REPO_ROOT}/backend/app" --exclude='__pycache__' --exclude='*.pyc' -cf - . \
  | docker exec -i "$CONTAINER" tar -C /data/platform/backend/app -xpf -

step "[2/4] syncing backend/scripts → ${CONTAINER}:/data/platform/backend/scripts/"
tar -C "${REPO_ROOT}/backend/scripts" --exclude='__pycache__' --exclude='*.pyc' -cf - . \
  | docker exec -i "$CONTAINER" tar -C /data/platform/backend/scripts -xpf -

docker exec -u root "$CONTAINER" chown -R mobius:mobius /data/platform/backend/app /data/platform/backend/scripts

if [ "$NO_RESTART" = "1" ]; then
  step "[3/4] SKIPPED restart (--no-restart) — uvicorn still runs the previously imported modules"
  exit 0
fi

step "[3/4] restarting ${CONTAINER} so uvicorn re-imports the served backend"
docker restart "$CONTAINER" >/dev/null

step "[4/4] waiting up to 30s for /api/health"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/health" || true)
  if [ "$code" = "200" ]; then echo "  healthy after ${i}s"; exit 0; fi
  sleep 1
done
echo "  health check never returned 200 — check 'docker logs ${CONTAINER}'" >&2
exit 1
