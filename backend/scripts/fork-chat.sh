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
# Resumable-or-reseed: the stored CLI session id often can't be resumed — a
# PHANTOM id (the codex plugin's SessionStart hook mints ids that get a
# `session-env/<id>` dir but no transcript) or one the CLI's ~30-day cleanup
# deleted. `--resume` against a missing transcript dies "No conversation
# found", which (piped to /dev/null) used to make this script silently print
# nothing — so Dreaming's interviews quietly failed for most older chats.
# Instead we check the transcript exists; when it doesn't, we reseed a fresh
# same-provider session from the chat's stored transcript so the interview
# still runs. Continuity then comes from the DB, not a byte-exact fork.
#
# Exit codes: 0 ok · 2 bad args · 4 unknown provider.
set -uo pipefail

CHAT_ID="${1:-}"; PROMPT="${2:-}"
if [[ -z "$CHAT_ID" || -z "$PROMPT" ]]; then
  echo "usage: fork-chat.sh <chat_id> \"<interview prompt>\"" >&2; exit 2
fi
DATA_DIR="${DATA_DIR:-/data}"
DB="$DATA_DIR/db/ultimate.db"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"

# The chat runner records Claude sessions under the project dir for cwd=/data,
# which the CLI encodes by stripping the leading slash and turning every '/'
# into '-' (/data -> -data). A session id is resumable only if that transcript
# file exists. (Same rule as `_resumable` in claude_sdk_runner.py — keep them
# in sync.)
proj_dir="-$(echo "$DATA_DIR" | sed 's#^/##; s#/#-#g')"
transcript_exists() { [[ -n "$1" && -f "$CLAUDE_CONFIG_DIR/projects/$proj_dir/$1.jsonl" ]]; }

# The chat's recent transcript as JSON, newest-trimmed to ~8KB — the seed for
# the reseed fallbacks. (python3; the container has no sqlite3 CLI.)
recent_messages() {
  CHAT_ID="$CHAT_ID" DB="$DB" python3 - <<'PY' 2>/dev/null | tail -c 8000
import os, sqlite3
con = sqlite3.connect(os.environ["DB"])
r = con.execute("select messages from chats where id=?", (os.environ["CHAT_ID"],)).fetchone()
print(r[0] if r and r[0] else "")
PY
}

reseed_prompt() {  # $1 = recent messages JSON
  printf 'You previously worked on this Möbius chat (recent messages as JSON):\n%s\n\n%s' "$1" "$PROMPT"
}

# Provider + CLI session id for the chat (reads the DB directly — no auth).
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

case "$PROVIDER" in
  claude)
    if transcript_exists "$SID"; then
      # True fork: --fork-session branches the original transcript byte-for-byte
      # into a throwaway session; the original chat is untouched.
      ( cd "$DATA_DIR" && claude --resume "$SID" --fork-session -p "$PROMPT" \
          --output-format text 2>/dev/null )
    else
      # No resumable transcript (phantom / expired / unset id) — reseed a fresh
      # session from the DB transcript so the interview still produces an answer.
      echo "fork-chat: $CHAT_ID has no resumable transcript; reseeding from DB" >&2
      ( cd "$DATA_DIR" && claude -p "$(reseed_prompt "$(recent_messages)")" \
          --output-format text 2>/dev/null )
    fi
    ;;
  codex)
    # Codex thread-resume isn't exposed as a clean CLI fork; reseed from the
    # chat's recent messages (same provider, not a byte-exact fork).
    codex exec "$(reseed_prompt "$(recent_messages)")" 2>/dev/null
    ;;
  *)
    echo "fork-chat: unknown provider '$PROVIDER'" >&2; exit 4 ;;
esac
