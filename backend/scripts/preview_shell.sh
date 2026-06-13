#!/usr/bin/env bash
# preview_shell.sh — screenshot the authenticated Möbius shell.
#
# Thin wrapper around agent-screenshot.sh (the general authenticated-
# screenshot helper). Kept for its historical signature so recovery.md
# and existing callers keep working:
#
#   preview_shell.sh [chat_id] [output_path]
#   defaults: chat_id=$CHAT_ID, output_path=/tmp/shell-preview.png
#
# Maps to the shell route: /chat/<id> when a chat id is given, else /.
# All the auth/viewport/banner handling lives in agent-screenshot.sh.

set -euo pipefail

CHAT_ID="${1:-${CHAT_ID:-}}"
OUT="${2:-/tmp/shell-preview.png}"

if [ -n "${CHAT_ID}" ]; then
  ROUTE="/chat/${CHAT_ID}"
else
  ROUTE="/"
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${DIR}/agent-screenshot.sh" "${ROUTE}" "${OUT}"
