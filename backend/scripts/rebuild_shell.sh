#!/bin/sh
set -e

NOTIFY_URL="${API_BASE_URL:-http://localhost:8000}/api/notify"
AUTH="Authorization: Bearer ${AGENT_TOKEN}"
CT="Content-Type: application/json"

# best-effort notification — curl failure should not abort rebuild
notify() {
  curl -s -X POST "$NOTIFY_URL" -H "$AUTH" -H "$CT" -d "$1" >/dev/null 2>&1 || true
}

notify_failed() {
  payload=$(
    printf '%s' "$1" | python3 -c 'import json, sys; print(json.dumps({"type":"shell_rebuild_failed","error":sys.stdin.read().strip()}))' 2>/dev/null \
      || printf '{"type":"shell_rebuild_failed","error":"shell rebuild failed"}'
  )
  notify "$payload"
}

notify '{"type":"shell_rebuilding"}'

FRONTEND_DIR=/data/platform/frontend
NEXT_DIST=.dist-next
OLD_DIST=.dist-old
CACHE_DIR=.vite-cache
TMP_DIR=.vite-tmp
BUILD_LOG="$TMP_DIR/vite-build.log"

cd "$FRONTEND_DIR"

# The served frontend clone owns the build. Reuse the baked node_modules from
# /app/shell-src so runtime edits do not install dependencies into /data.
[ -e node_modules ] || [ -L node_modules ] || ln -s /app/shell-src/node_modules node_modules || true

# Clean the output and Vite transform cache to ensure a fully fresh build.
rm -rf "$NEXT_DIST" "$OLD_DIST" "$CACHE_DIR" "$TMP_DIR" node_modules/.vite 2>/dev/null || true
mkdir -p "$CACHE_DIR" "$TMP_DIR"

if MOBIUS_VITE_CACHE="$FRONTEND_DIR/$CACHE_DIR" TMPDIR="$FRONTEND_DIR/$TMP_DIR" \
  npx vite build --configLoader runner --outDir "$NEXT_DIST" --emptyOutDir >"$BUILD_LOG" 2>&1; then
  cat "$BUILD_LOG"
  # Vite builds the app bundle but does NOT copy the self-hosted
  # vendor libs (three.js etc.) — those live only at /app/static/vendor
  # from the Dockerfile's npm-install step. Without this copy, mini-
  # apps importing /vendor/three/three.module.js get the SPA
  # index.html (via main.py's spa_fallback) and silently fail to
  # load. Surfaced in chat 380581a8 where tunnel-runner-3d hung
  # on its loader; agent had to patch its own import path.
  if [ -d /app/static/vendor ]; then
    rm -rf "$NEXT_DIST/vendor" 2>/dev/null || true
    cp -r /app/static/vendor "$NEXT_DIST/vendor" 2>/dev/null || true
  fi
  if [ ! -f "$NEXT_DIST/index.html" ] || [ ! -d "$NEXT_DIST/assets" ]; then
    rm -rf "$NEXT_DIST" 2>/dev/null || true
    err="vite build did not produce a complete dist"
    echo "$err" >&2
    notify_failed "$err"
    exit 1
  fi
  old_moved=0
  if [ -e dist ]; then
    mv dist "$OLD_DIST"
    old_moved=1
  fi
  if ! mv "$NEXT_DIST" dist; then
    if [ "$old_moved" -eq 1 ] && [ ! -e dist ] && [ -e "$OLD_DIST" ]; then
      mv "$OLD_DIST" dist 2>/dev/null || true
    fi
    err="could not promote rebuilt frontend dist"
    echo "$err" >&2
    notify_failed "$err"
    exit 1
  fi
  rm -rf "$OLD_DIST" 2>/dev/null || true
  echo "Frontend rebuilt successfully."
  notify '{"type":"shell_rebuilt"}'
else
  cat "$BUILD_LOG" >&2 || true
  err="$(tail -c 4000 "$BUILD_LOG" 2>/dev/null || printf 'vite build failed')"
  [ -n "$err" ] || err="vite build failed"
  notify_failed "$err"
  exit 1
fi
