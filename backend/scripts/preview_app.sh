#!/usr/bin/env bash
# preview_app.sh — screenshot a mini-app rendered inside the
# authenticated Möbius shell.
#
# Direct app-frame URLs (/api/apps/{id}/frame?token=...) used to
# work as standalone preview targets, but the frame now waits for
# a parent-shell `moebius:frame-init` postMessage before
# initializing. Screenshotting the bare frame URL gives you a blank
# page saying "No init message from parent shell."
#
# This helper takes the working path: sign in via localStorage,
# navigate to /app/<id> in the authenticated shell, dismiss the
# install banner, wait for first render, screenshot.
#
# Usage:
#   preview_app.sh <app_id> [output_path]
#   defaults: output_path=/data/chats/$CHAT_ID/generated/app-<id>.png
#
# Prints the output path on stdout.

set -euo pipefail

APP_ID="${1:-}"
if [ -z "$APP_ID" ]; then
  echo "preview_app.sh: app_id required" >&2
  echo "Usage: preview_app.sh <app_id> [output_path]" >&2
  exit 1
fi

OUT="${2:-/data/chats/${CHAT_ID:-unknown}/generated/app-${APP_ID}.png}"
mkdir -p "$(dirname "$OUT")"

if [ -z "${AGENT_TOKEN:-}" ] || [ -z "${API_BASE_URL:-}" ]; then
  echo "preview_app.sh: AGENT_TOKEN and API_BASE_URL must be set" >&2
  exit 1
fi

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "preview_app.sh: agent-browser not on PATH" >&2
  exit 1
fi

# Match the partner's viewport (set by chat.py from the React
# shell's per-turn payload). Screenshots require those values.
if [ -z "${VIEWPORT_WIDTH:-}" ] || [ -z "${VIEWPORT_HEIGHT:-}" ]; then
  echo "preview_app.sh: VIEWPORT_WIDTH and VIEWPORT_HEIGHT must be set" >&2
  exit 1
fi
VW="$VIEWPORT_WIDTH"
VH="$VIEWPORT_HEIGHT"
agent-browser set viewport "$VW" "$VH" >/dev/null

# Same auth dance as preview_shell.sh: load origin, write token to
# localStorage, navigate to the in-shell app route.
agent-browser open "${API_BASE_URL}/" >/dev/null
agent-browser eval "localStorage.setItem('token', '${AGENT_TOKEN}')" >/dev/null
agent-browser open "${API_BASE_URL}/app/${APP_ID}" >/dev/null
agent-browser wait 1500 >/dev/null

# Dismiss the install banner so it doesn't cover the bottom of the
# app while we screenshot.
agent-browser find text "Not now" click >/dev/null 2>&1 || true
agent-browser wait 300 >/dev/null

agent-browser screenshot "$OUT" >/dev/null
echo "$OUT"
