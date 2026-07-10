#!/bin/bash
# fork-chat.sh <chat_id> "<interview prompt>"
#
# Forks a past chat into a THROWAWAY copy and interviews the agent that did the
# work, so the nightly Reflection agent can ask it what happened, what to prepare
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
# nothing — so Reflection's interviews quietly failed for most older chats.
# Instead we check the transcript exists; when it doesn't, we reseed a fresh
# same-provider session from the chat's stored transcript so the interview
# still runs. Continuity then comes from the DB, not a byte-exact fork.
#
# Exit codes: 0 ok · 2 bad args · 4 unknown provider · 5 DB read failed ·
# 6 no transcript to interview (refused rather than fabricate one).
#
# Fail-loud, don't fabricate: the DB reads below used to swallow errors
# (`2>/dev/null` + no set -e), so a missing/locked/corrupt DB silently
# defaulted the provider to Claude with EMPTY messages — yet the reseed prompt
# still tells Claude "you previously worked on this chat," so it would
# hallucinate an interview and Reflection would extract durable "facts" from
# it. We now check every DB read and refuse the fork (non-zero, no CLI call)
# when the read fails or the chat has no transcript to interview.
set -uo pipefail

CHAT_ID="${1:-}"; PROMPT="${2:-}"
if [[ -z "$CHAT_ID" || -z "$PROMPT" ]]; then
  echo "usage: fork-chat.sh <chat_id> \"<interview prompt>\"" >&2; exit 2
fi
DATA_DIR="${DATA_DIR:-/data}"
DB="$DATA_DIR/db/ultimate.db"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"
export CODEX_HOME="${CODEX_HOME:-$DATA_DIR/cli-auth/codex}"

# The chat runner records Claude sessions under the project dir for cwd=/data,
# which the CLI encodes by stripping the leading slash and turning every '/'
# into '-' (/data -> -data). A session id is resumable only if that transcript
# file exists. (Same rule as `_resumable` in claude_sdk_runner.py — keep them
# in sync.)
proj_dir="-$(echo "$DATA_DIR" | sed 's#^/##; s#/#-#g')"
transcript_exists() { [[ -n "$1" && -f "$CLAUDE_CONFIG_DIR/projects/$proj_dir/$1.jsonl" ]]; }

# The chat's recent transcript tail (last N messages), the seed for the
# reseed fallbacks. (python3; the container has no sqlite3 CLI.)
#
# Boundary-safe by construction: the old `tail -c 8000` sliced the messages
# JSON mid-object / mid-UTF-8 while labeling the result "recent messages as
# JSON," handing the model a truncated, unparseable blob. Instead we parse the
# array in python, keep the last N WHOLE messages, and shrink by dropping the
# OLDEST whole message until it fits the budget. A DB read error propagates as
# a non-zero exit (no `2>/dev/null` swallow) so the caller can refuse rather
# than fabricate.
#
# Output contract: empty (no usable transcript), or a first line of exactly
# `json` or `raw` followed by the payload. A `json` payload is a valid JSON
# value; a `raw` payload is a char-boundary tail of a legacy/corrupt row that
# json.loads rejected. The sentinel is positional — always the first line,
# always written here — so transcript content can never be mistaken for it,
# and reseed_prompt labels the block by which case actually happened instead
# of asserting "JSON" over text that isn't.
RECENT_BUDGET="${FORK_CHAT_RECENT_BUDGET:-8000}"
RECENT_KEEP="${FORK_CHAT_RECENT_KEEP:-15}"
recent_messages() {
  CHAT_ID="$CHAT_ID" DB="$DB" BUDGET="$RECENT_BUDGET" KEEP="$RECENT_KEEP" \
    python3 - <<'PY'
import json, os, sqlite3, sys
con = sqlite3.connect(os.environ["DB"])
r = con.execute(
  "select messages from chats where id=?", (os.environ["CHAT_ID"],)
).fetchone()
raw = r[0] if r and r[0] else ""
if not raw:
  sys.exit(0)  # print nothing; the caller treats empty as "no transcript"
try:
  msgs = json.loads(raw)
except ValueError:
  # Not a JSON array (legacy/odd row) — emit a char-boundary raw tail under
  # the `raw` sentinel so the prompt never claims it is structured JSON. A
  # whitespace-only tail carries no conversational signal: print nothing so
  # the caller refuses (exit 6) instead of interviewing over blank text.
  tail = raw[-int(os.environ["BUDGET"]):]
  if tail.strip():
    sys.stdout.write("raw\n" + tail)
  sys.exit(0)
if isinstance(msgs, list):
  msgs = msgs[-int(os.environ["KEEP"]):]
  out = json.dumps(msgs, ensure_ascii=False)
  budget = int(os.environ["BUDGET"])
  # Trim to budget by dropping whole (oldest) messages — never mid-object.
  while len(out) > budget and len(msgs) > 1:
    msgs = msgs[1:]
    out = json.dumps(msgs, ensure_ascii=False)
  sys.stdout.write("json\n" + out)
else:
  sys.stdout.write("json\n" + json.dumps(msgs, ensure_ascii=False))
PY
}

# Prints the chat's sentinel-tagged transcript for a reseed, or RETURNS
# non-zero (5 = DB read failed, 6 = no transcript) so the caller refuses the
# fork instead of fabricating an interview. It `return`s (not `exit`s) because
# it's meant to be called via `MSGS="$(require_recent_messages)"; rc=$?` — the
# caller must then check rc and exit; an `exit` here would only leave the
# substitution subshell.
require_recent_messages() {
  local msgs rc fmt
  msgs="$(recent_messages)"; rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "fork-chat: DB read failed for $CHAT_ID (rc=$rc); refusing to fabricate an interview" >&2
    return 5
  fi
  if [[ -z "$msgs" ]]; then
    echo "fork-chat: $CHAT_ID has no stored transcript; refusing to fabricate an interview" >&2
    return 6
  fi
  # A missing/unknown sentinel means the reader half of the contract broke —
  # refuse rather than guess a label for the payload. ($fmt == $msgs catches
  # a sentinel with no payload line after it.)
  fmt="${msgs%%$'\n'*}"
  if [[ "$fmt" == "$msgs" || ( "$fmt" != json && "$fmt" != raw ) ]]; then
    echo "fork-chat: transcript for $CHAT_ID arrived without a format sentinel; refusing to fabricate an interview" >&2
    return 5
  fi
  printf '%s' "$msgs"
}

# $1 = sentinel-tagged transcript from require_recent_messages. The framing is
# COMPUTED from the sentinel, never asserted: a `json` tail keeps the
# structured "recent messages as JSON" framing; a `raw` tail is presented as
# an unparseable-transcript excerpt so the model is never told malformed text
# is well-formed JSON.
reseed_prompt() {
  local fmt="${1%%$'\n'*}" body="${1#*$'\n'}"
  if [[ "$fmt" == json ]]; then
    printf 'You previously worked on this Möbius chat (recent messages as JSON):\n%s\n\n%s' "$body" "$PROMPT"
  else
    printf 'You previously worked on this Möbius chat. Its stored transcript is not valid JSON; below is a raw text excerpt of it (possibly truncated mid-text — this is plain text, not JSON):\n%s\n\n%s' "$body" "$PROMPT"
  fi
}

run_codex_reseed() {
  local prompt="$1" out log_tmp rc
  out="$(mktemp "${TMPDIR:-/tmp}/fork-chat-codex-out.XXXXXX")" || return 5
  log_tmp="$(mktemp "${TMPDIR:-/tmp}/fork-chat-codex-log.XXXXXX")" || {
    rm -f "$out"
    return 5
  }
  (
    cd "$DATA_DIR" && codex exec \
      --skip-git-repo-check \
      --ephemeral \
      --sandbox read-only \
      --color never \
      --output-last-message "$out" \
      - <<<"$prompt"
  ) >"$log_tmp" 2>&1
  rc=$?
  if [[ $rc -ne 0 ]]; then
    cat "$log_tmp" >&2
    echo "fork-chat: codex interview failed for $CHAT_ID (rc=$rc)" >&2
    rm -f "$out" "$log_tmp"
    return "$rc"
  fi
  cat "$out"
  rm -f "$out" "$log_tmp"
}

# Provider + CLI session id for the chat (reads the DB directly — no auth).
# A DB error exits non-zero (no `2>/dev/null` swallow) and a missing chat row
# exits 3, so we can tell "read failed / no such chat" from "chat exists but
# has no session id" — the latter is a valid reseed case, the former is not.
row="$(CHAT_ID="$CHAT_ID" DB="$DB" python3 - <<'PY'
import os, sqlite3, sys
con = sqlite3.connect(os.environ["DB"])
r = con.execute(
  "select coalesce(provider,'claude'), coalesce(session_id,'') from chats where id=?",
  (os.environ["CHAT_ID"],),
).fetchone()
if r is None:
  sys.exit(3)  # no such chat — nothing to interview
print(r[0] + "|" + (r[1] or ""))
PY
)"; row_rc=$?
if [[ $row_rc -ne 0 || -z "$row" ]]; then
  echo "fork-chat: could not read chat $CHAT_ID metadata (rc=$row_rc); refusing to fabricate an interview" >&2
  exit 5
fi
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
      # Capture at the top level so a "no transcript" refusal actually exits.
      echo "fork-chat: $CHAT_ID has no resumable transcript; reseeding from DB" >&2
      MSGS="$(require_recent_messages)"; msgs_rc=$?
      [[ $msgs_rc -eq 0 ]] || exit "$msgs_rc"
      ( cd "$DATA_DIR" && claude -p "$(reseed_prompt "$MSGS")" \
          --output-format text 2>/dev/null )
    fi
    ;;
  codex)
    # Codex thread-resume isn't exposed as a clean CLI fork; reseed from the
    # chat's recent messages (same provider, not a byte-exact fork). Pin
    # CODEX_HOME to the same auth dir real chat turns use and allow /data,
    # which is intentionally not a git repo; otherwise Reflection's Codex
    # interviews fail the CLI trust check and print nothing useful.
    MSGS="$(require_recent_messages)"; msgs_rc=$?
    [[ $msgs_rc -eq 0 ]] || exit "$msgs_rc"
    run_codex_reseed "$(reseed_prompt "$MSGS")"
    ;;
  *)
    echo "fork-chat: unknown provider '$PROVIDER'" >&2; exit 4 ;;
esac
