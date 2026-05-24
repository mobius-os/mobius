#!/bin/sh
set -e

NOTIFY_URL="${API_BASE_URL:-http://localhost:8000}/api/notify"
AUTH="Authorization: Bearer ${AGENT_TOKEN}"
CT="Content-Type: application/json"

# best-effort notification — curl failure should not abort rebuild
notify() {
  curl -s -X POST "$NOTIFY_URL" -H "$AUTH" -H "$CT" -d "$1" >/dev/null 2>&1 || true
}

notify '{"type":"shell_rebuilding"}'

cd /data/shell
# Clean dist and vite transform cache to ensure a fully fresh build.
# Without clearing .vite, vite may reuse cached transforms from the
# previous source and produce a stale bundle.
rm -rf dist node_modules/.vite 2>/dev/null || true
if npx vite build 2>&1; then
  # Vite builds the app bundle but does NOT copy the self-hosted
  # vendor libs (three.js etc.) — those live only at /app/static/vendor
  # from the Dockerfile's npm-install step. Without this copy, mini-
  # apps importing /vendor/three/three.module.js get the SPA
  # index.html (via main.py's spa_fallback) and silently fail to
  # load. Surfaced in chat 380581a8 where tunnel-runner-3d hung
  # on its loader; agent had to patch its own import path.
  if [ -d /app/static/vendor ]; then
    cp -r /app/static/vendor dist/vendor 2>/dev/null || true
  fi
  echo "Shell rebuilt successfully."
  notify '{"type":"shell_rebuilt"}'
else
  err="vite build failed"
  echo "$err" >&2
  notify "{\"type\":\"shell_rebuild_failed\",\"error\":\"$err\"}"
  exit 1
fi
