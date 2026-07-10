#!/usr/bin/env bash
# verify-fresh.sh — fail if the served bundle isn't the one we just built.
#
# Wraps bundle-info.sh's JSON output and compares:
#   - what the container is currently serving (parsed from <script src=> in /)
#   - what's actually on disk at /data/platform/frontend/dist/assets/index-*.js
#
# A mismatch means the served platform frontend dist is not the bundle currently
# reaching clients.
#
# Usage:
#   CONTAINER=mobius PORT=8000 scripts/verify-fresh.sh
#   CONTAINER=mobius-test PORT=8001 scripts/verify-fresh.sh
#
# Returns 0 if served == dist, non-zero otherwise.

set -euo pipefail

CONTAINER="${CONTAINER:-mobius-test}"
PORT="${PORT:-8001}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The SPA lives under /shell/ (the manifest scope) — `/` 308-redirects
# there. Probe /shell/ directly so the bundle script tag is in the
# response body. Prod doesn't bind 8000 to the host; use docker exec.
SHELL_PATH="/shell/"
if [ "$CONTAINER" = "mobius" ]; then
  served=$(docker exec "$CONTAINER" curl -fsSL "http://localhost:${PORT}${SHELL_PATH}" \
    | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -1 || true)
else
  served=$(curl -fsSL "http://localhost:${PORT}${SHELL_PATH}" \
    | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -1 || true)
fi

if [ -z "$served" ]; then
  echo "verify-fresh: could not parse served bundle from /shell/ (container=$CONTAINER port=$PORT)" >&2
  exit 2
fi

# The dist file the container would serve from /data/platform/frontend/dist/assets/.
# `ls | head -1` is fine — vite produces exactly one index-<hash>.js per build.
dist=$(docker exec "$CONTAINER" bash -c \
  "ls /data/platform/frontend/dist/assets/ 2>/dev/null | grep '^index-' | grep '\\.js$' | head -1" || true)

if [ -z "$dist" ]; then
  echo "verify-fresh: no dist bundle at /data/platform/frontend/dist/assets/ — container is serving the baked /app/static/ fallback" >&2
  echo "  served: $served (from baked image)"
  echo "  The watcher builds /data/platform/frontend/src into dist after source saves."
  echo "  For platform updates, use the platform apply flow; it rebuilds via rebuild_frontend_now."
  exit 3
fi

if [ "$served" = "$dist" ]; then
  echo "verify-fresh: OK — $CONTAINER serving $served"
  exit 0
fi

cat >&2 <<EOF
verify-fresh: MISMATCH — $CONTAINER is serving stale code

  served: $served
  on disk: $dist

The container is serving a different bundle than the platform frontend dist.
Wait for any in-flight watcher build to finish, then check again. If this
followed a platform update rather than a file save, use the platform apply flow;
it rebuilds /data/platform/frontend via rebuild_frontend_now.
EOF
exit 1
