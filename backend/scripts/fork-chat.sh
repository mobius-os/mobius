#!/bin/bash
# fork-chat.sh [--json] <chat_id> "<coaching prompt>"
#
# Resolves the chat's provider session and delegates to the exact-session fork
# driver. It never reconstructs a session from stored messages.
set -euo pipefail

exec python3 "$(dirname "$0")/fork_chat.py" "$@"
