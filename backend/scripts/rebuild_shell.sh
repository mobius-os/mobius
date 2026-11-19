#!/bin/sh
set -e

MAPI="$(dirname "$0")/mapi"

# best-effort notification — a failure should not abort rebuild
notify() {
  "$MAPI" -X POST /api/notify -d "$1" >/dev/null 2>&1 || true
}

notify_failed() {
  payload=$(
    printf '%s' "$1" | python3 -c '
import json, sys
print(json.dumps({
  "type": "shell_rebuild_failed",
  "error": sys.stdin.read().strip(),
}))
' 2>/dev/null \
      || printf '{"type":"shell_rebuild_failed","error":"shell rebuild failed"}'
  )
  notify "$payload"
}

FRONTEND_DIR="${FRONTEND_DIR:-/data/platform/frontend}"
BACKEND_DIR="${BACKEND_DIR:-/data/platform/backend}"
TMP_DIR="$FRONTEND_DIR/.vite-tmp"
BUILD_LOG="$TMP_DIR/vite-build.log"

mkdir -p "$TMP_DIR"
notify '{"type":"shell_rebuilding"}'

if cd "$BACKEND_DIR" && PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m app.frontend_watcher >"$BUILD_LOG" 2>&1; then
  cat "$BUILD_LOG"
  echo "Frontend rebuilt successfully."
  notify '{"type":"shell_rebuilt"}'
else
  cat "$BUILD_LOG" >&2 || true
  err="$(tail -c 4000 "$BUILD_LOG" 2>/dev/null || printf 'vite build failed')"
  [ -n "$err" ] || err="vite build failed"
  notify_failed "$err"
  exit 1
fi
