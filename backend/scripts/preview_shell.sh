#!/usr/bin/env bash
# preview_shell.sh — screenshot the authenticated Möbius shell
#
# The shell only mounts after login; without a token the React app
# renders LoginForm and any ornaments on `html`/`body` are visible
# but the chat surfaces aren't. Agents that screenshot the bare app
# URL get a misleading preview of how their theme actually lands.
#
# This helper does the auth dance — sets the agent's scoped token
# in localStorage, navigates to a chat URL, dismisses the install
# banner, and writes a screenshot of the real chat view.
#
# Usage:
#   preview_shell.sh [chat_id] [output_path]
#   defaults: chat_id=$CHAT_ID, output_path=/tmp/shell-preview.png
#
# Returns the output path on stdout, or non-zero if the auth dance
# fails (no token, no API_BASE_URL, no agent-browser).

set -euo pipefail

CHAT_ID="${1:-${CHAT_ID:-}}"
OUT="${2:-/tmp/shell-preview.png}"

if [ -z "${AGENT_TOKEN:-}" ] || [ -z "${API_BASE_URL:-}" ]; then
  echo "preview_shell.sh: AGENT_TOKEN and API_BASE_URL must be set" >&2
  exit 1
fi

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "preview_shell.sh: agent-browser not on PATH" >&2
  exit 1
fi

# Origin must be loaded before localStorage.setItem (localStorage is
# per-origin and only writable once a same-origin document exists).
agent-browser open "${API_BASE_URL}/" >/dev/null
agent-browser eval "localStorage.setItem('token', '${AGENT_TOKEN}')" >/dev/null

# Now navigate to the actual shell view.
if [ -n "${CHAT_ID}" ]; then
  TARGET="${API_BASE_URL}/chat/${CHAT_ID}"
else
  TARGET="${API_BASE_URL}/"
fi
agent-browser open "${TARGET}" >/dev/null
agent-browser wait 1500 >/dev/null

# Dismiss the PWA install banner if it surfaces — it covers the
# bottom of the shell and would distract from the actual theme.
agent-browser find text "Not now" click >/dev/null 2>&1 || true
agent-browser wait 300 >/dev/null

agent-browser screenshot "${OUT}" >/dev/null
echo "${OUT}"
