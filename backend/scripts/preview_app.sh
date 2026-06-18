#!/usr/bin/env bash
# preview_app.sh — screenshot a mini-app rendered inside the
# authenticated Möbius shell.
#
# Thin wrapper around agent-screenshot.sh (the general authenticated-
# screenshot helper). Kept for its historical signature so recovery.md
# and existing callers keep working:
#
#   preview_app.sh <app_id> [output_path]
#   defaults: output_path=/data/chats/$CHAT_ID/media/app-<id>.png
#
# Maps to the in-shell app route /app/<id>. The bare app-frame URL
# can't be screenshotted directly — the frame waits for the parent
# shell's `moebius:frame-init` postMessage before initializing — so we
# go through /app/<id> in the authenticated shell. For the STANDALONE
# PWA page of an app, call agent-screenshot.sh /apps/<slug>/ directly.
# All auth/viewport/banner handling lives in agent-screenshot.sh.

set -euo pipefail

APP_ID="${1:-}"
if [ -z "$APP_ID" ]; then
  echo "preview_app.sh: app_id required" >&2
  echo "Usage: preview_app.sh <app_id> [output_path]" >&2
  exit 1
fi

OUT="${2:-/data/chats/${CHAT_ID:-unknown}/media/app-${APP_ID}.png}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${DIR}/agent-screenshot.sh" "/app/${APP_ID}" "${OUT}"
