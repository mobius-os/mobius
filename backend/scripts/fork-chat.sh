#!/bin/bash
# fork-chat.sh [--json] [--after-call '<call moment json>'] <chat_id> "<coaching prompt>"
#
# Resolves the chat's provider session and delegates to the exact-session fork
# driver. With --after-call, the fork ends at that app tool call (Claude:
# right after its result; Codex: at the end of the turn that made it).
# It never reconstructs a session from stored messages.
set -euo pipefail

exec python3 "$(dirname "$0")/fork_chat.py" "$@"
