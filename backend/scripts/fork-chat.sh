#!/bin/bash
# fork-chat.sh <chat_id> "<interview prompt>"
#
# Forks a past chat into a THROWAWAY copy and interviews the agent that did the
# work, so the nightly Dreaming agent can ask it what happened, what to prepare
# for the user, what was hard, how well it used its skills, and how the
# knowledge graph could improve. Prints the agent's answer to stdout.
#
# Same-provider by construction: you can only resume a Claude session with
# Claude and a Codex thread with Codex, and the matching agent gives the best
# introspection. The ORIGINAL chat is never touched — `--fork-session` mints a
# new session id and branches the conversation.
#
# Exit codes: 0 ok · 2 bad args · 3 no resumable session · 4 unknown provider.
set -uo pipefail

CHAT_ID="${1:-}"; PROMPT="${2:-}"
if [[ -z "$CHAT_ID" || -z "$PROMPT" ]]; then
  echo "usage: fork-chat.sh <chat_id> \"<interview prompt>\"" >&2; exit 2
fi
DATA_DIR="${DATA_DIR:-/data}"
DB="$DATA_DIR/db/ultimate.db"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"

# Look up the chat's provider + CLI session id (python3; the container has no
# sqlite3 CLI). Reads the DB directly — fast + no auth needed.
row="$(CHAT_ID="$CHAT_ID" DB="$DB" python3 - <<'PY' 2>/dev/null
import os, sqlite3
con = sqlite3.connect(os.environ["DB"])
r = con.execute(
  "select coalesce(provider,'claude'), coalesce(session_id,'') from chats where id=?",
  (os.environ["CHAT_ID"],),
).fetchone()
print((r[0] if r else "claude") + "|" + (r[1] if r and r[1] else ""))
PY
)"
PROVIDER="${row%%|*}"; SID="${row#*|}"
PROVIDER="${PROVIDER:-claude}"

if [[ -z "$SID" ]]; then
  echo "fork-chat: chat $CHAT_ID has no resumable session id (provider=$PROVIDER); skipping" >&2
  exit 3
fi

case "$PROVIDER" in
  claude)
    # Resume from cwd=/data — chat sessions are recorded under the /data project
    # (cwd the chat runner uses). --fork-session keeps the original transcript
    # byte-for-byte; the fork is a fresh session we don't keep.
    ( cd "$DATA_DIR" && claude --resume "$SID" --fork-session -p "$PROMPT" \
        --output-format text 2>/dev/null )
    ;;
  codex)
    # Codex thread-resume isn't exposed as a clean CLI fork yet; fall back to a
    # fresh Codex session seeded with the chat's recent messages (same provider,
    # not a true fork). Good enough for a summary-style interview.
    MSGS="$(CHAT_ID="$CHAT_ID" DB="$DB" python3 - <<'PY' 2>/dev/null | tail -c 8000
import os, sqlite3
con = sqlite3.connect(os.environ["DB"])
r = con.execute("select messages from chats where id=?", (os.environ["CHAT_ID"],)).fetchone()
print(r[0] if r and r[0] else "")
PY
)"
    codex exec "You previously worked on this Möbius chat (recent messages as JSON):
$MSGS

$PROMPT" 2>/dev/null
    ;;
  *)
    echo "fork-chat: unknown provider '$PROVIDER'" >&2; exit 4 ;;
esac
