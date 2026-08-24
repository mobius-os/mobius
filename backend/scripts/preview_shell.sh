#!/usr/bin/env bash
# preview_shell.sh — screenshot the authenticated Möbius shell.
#
# Thin wrapper around agent-screenshot.sh (the general authenticated-
# screenshot helper). Kept for its historical signature so platform-maintenance.md
# and existing callers keep working:
#
#   preview_shell.sh [chat_id] [output_path]
#   defaults: chat_id=$CHAT_ID, output_path=the current chat's unique media path
#
# Maps to the shell route: /chat/<id> when a chat id is given, else /.
# All the auth/viewport/banner handling lives in agent-screenshot.sh.

set -euo pipefail

CHAT_ID="${1:-${CHAT_ID:-}}"
if [ -n "$CHAT_ID" ]; then
  # agent-screenshot.sh owns the default media path, so an explicit positional
  # chat id must cross the exec boundary just like an inherited CHAT_ID does.
  export CHAT_ID
fi
OUT="${2:-}"
if [ -z "$OUT" ] && [ -z "$CHAT_ID" ]; then
  # A standalone developer invocation has no chat-owned destination.
  OUT="/tmp/shell-preview.png"
fi

if [ -n "${CHAT_ID}" ]; then
  ROUTE="/chat/${CHAT_ID}"
else
  ROUTE="/"
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "$OUT" ]; then
  exec "${DIR}/agent-screenshot.sh" "${ROUTE}" "${OUT}"
fi
# Let the owning helper mint the final unique media path directly. This keeps
# a viewable capture out of /tmp without creating a second copy afterward.
exec "${DIR}/agent-screenshot.sh" "${ROUTE}"
