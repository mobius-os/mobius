#!/bin/bash
# fork-session.sh <session_id> <cwd> "<interview prompt>"
#
# Forks a raw CLI session by id and interviews it — for app subagent runs (cron
# jobs like news/gym) whose sessions are NOT rows in the chats table. The
# Dreaming agent finds these under
# /data/cli-auth/claude/projects/<encoded-cwd>/<session_id>.jsonl, decodes the
# cwd (the dir name is the cwd with '/' -> '-', e.g. -data-apps-news-2 ==
# /data/apps/news-2), and forks with that cwd so --resume locates the session.
# For chats, use fork-chat.sh instead. The original session is untouched.
set -uo pipefail
SID="${1:-}"; CWD="${2:-/data}"; PROMPT="${3:-}"
if [[ -z "$SID" || -z "$PROMPT" ]]; then
  echo "usage: fork-session.sh <session_id> <cwd> \"<prompt>\"" >&2; exit 2
fi
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/data/cli-auth/claude}"
( cd "$CWD" 2>/dev/null && claude --resume "$SID" --fork-session -p "$PROMPT" \
    --output-format text 2>/dev/null )
