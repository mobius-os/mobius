#!/usr/bin/env bash
# preview_app.sh — screenshot a mini-app rendered inside the
# authenticated Möbius shell.
#
# Thin wrapper around agent-screenshot.sh (the general authenticated-
# screenshot helper). Kept for its historical signature so platform-maintenance.md
# and existing callers keep working:
#
#   preview_app.sh [--standalone] <app_id> [output_path]
#   defaults: output_path=/data/chats/$CHAT_ID/media/app-<id>.png
#
# Maps to the in-shell app route /app/<id>. The bare app-frame URL
# can't be screenshotted directly — the frame waits for the parent
# shell's `moebius:frame-init` postMessage before initializing — so we
# go through /app/<id> in the authenticated shell. `--standalone` keeps the
# same numeric input and resolves the unique slug internally before opening
# the PWA page.
# All auth/viewport/banner handling lives in agent-screenshot.sh. App previews
# use its ephemeral content-only mode so owner onboarding/install overlays do
# not cover the app under test or mutate account completion state.

set -euo pipefail

STANDALONE=0
if [ "${1:-}" = "--standalone" ]; then
  STANDALONE=1
  shift
fi

APP_ID="${1:-}"
if [ -z "$APP_ID" ]; then
  echo "preview_app.sh: app_id required" >&2
  echo "Usage: preview_app.sh [--standalone] <app_id> [output_path]" >&2
  exit 1
fi
case "$APP_ID" in
  *[!0-9]*)
    echo "preview_app.sh: app_id must be numeric" >&2
    exit 1 ;;
esac

if [ "$STANDALONE" -eq 1 ]; then
  OUT="${2:-/data/chats/${CHAT_ID:-unknown}/media/app-${APP_ID}-standalone.png}"
  SLUG="$(
    APP_ID="$APP_ID" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

token = os.environ.get("AGENT_TOKEN")
base = os.environ.get("API_BASE_URL", "").rstrip("/")
app_id = os.environ["APP_ID"]
if not token or not base:
  print(
    "preview_app.sh: AGENT_TOKEN and API_BASE_URL must be set",
    file=sys.stderr,
  )
  raise SystemExit(1)
request = urllib.request.Request(
  f"{base}/api/apps/{app_id}",
  headers={"Authorization": f"Bearer {token}"},
)
try:
  with urllib.request.urlopen(request, timeout=30) as response:
    app = json.loads(response.read())
except (urllib.error.URLError, json.JSONDecodeError) as exc:
  print(f"preview_app.sh: could not resolve app {app_id}: {exc}", file=sys.stderr)
  raise SystemExit(1) from exc
slug = app.get("slug") if isinstance(app, dict) else None
if not isinstance(slug, str) or not slug:
  print(f"preview_app.sh: app {app_id} has no standalone slug", file=sys.stderr)
  raise SystemExit(1)
print(slug)
PY
  )"
  ROUTE="/apps/${SLUG}/"
else
  OUT="${2:-/data/chats/${CHAT_ID:-unknown}/media/app-${APP_ID}.png}"
  ROUTE="/app/${APP_ID}"
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$STANDALONE" -eq 1 ]; then
  exec "${DIR}/agent-screenshot.sh" "${ROUTE}" "${OUT}"
fi
exec "${DIR}/agent-screenshot.sh" --content-only "${ROUTE}" "${OUT}"
