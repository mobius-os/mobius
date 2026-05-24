#!/usr/bin/env bash
# live-test-sdk.sh — End-to-end live test for the Claude Agent SDK path.
#
# Drives mobius-test through a curl-only smoke that the live UI would
# exercise: basic turn, session resume across turns, AskUserQuestion
# round-trip, Stop while a turn is running. Verifies the SDK dispatch
# path specifically (not the subprocess fallback).
#
# Complements scripts/live-test.sh (UI / mobile-emulated) — this one is
# CI-friendly and faster: no browser, no screenshots, just SSE + REST.
#
# All operations target http://localhost:8001 — mobius-test ONLY.
# REFUSES to talk to prod (:8000).
#
# Usage:
#   bash scripts/live-test-sdk.sh              # rebuild + fresh container + run
#   SKIP_REBUILD=1 bash scripts/live-test-sdk.sh  # reuse current mobius-test
#   FLOWS="1 2"   bash scripts/live-test-sdk.sh   # subset

set -u  # NOT -e — we want to keep going on individual flow failures

readonly PORT=8001
readonly BASE="http://localhost:${PORT}"
readonly LOG_DIR=/tmp/mobius-live-sdk
# Flows 1-4 cover the Claude SDK path (basic / resume / AskUserQuestion / Stop).
# Flows 5-7 cover the Codex SDK path (basic / resume / Stop). AskUserQuestion
# for Codex is wired (see codex_sdk_runner.py:_install_request_user_input_handler)
# but not currently exercised by this script — flow 3 covers the Claude path;
# if the Codex bridge needs a smoke, add a flow that POSTs answers to
# /api/chats/.../messages with body.answers set. Run Codex flows by exporting
# CODEX=1 (default: off, to avoid burning credits on every smoke run).
readonly FLOWS=${FLOWS:-"1 2 3 4"}
readonly CODEX_FLOWS=${CODEX_FLOWS:-"5 6 7"}
readonly RUN_CODEX=${CODEX:-0}

mkdir -p "$LOG_DIR"

# Refuse to talk to prod (:8000) for any reason.
if [ "$PORT" = "8000" ]; then
  echo "FATAL: live-test-sdk.sh hardcoded to mobius-test (:8001) only" >&2
  exit 1
fi

step() { printf '\n=== %s ===\n' "$*"; }
ok()   { printf '  ok  %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; exit_code=1; }

exit_code=0

# ── setup ────────────────────────────────────────────────────────────
step "setup mobius-test container"

if [ -z "${SKIP_REBUILD:-}" ]; then
  cd "$(dirname "$0")/.." || exit 1
  docker compose -p mobius-test -f docker-compose.test.yml build >/dev/null 2>&1 \
    && ok "image built" || fail "image build failed"
fi

docker rm -f mobius-test >/dev/null 2>&1
docker volume rm mobius-test_test_data >/dev/null 2>&1
cd "$(dirname "$0")/.." || exit 1
docker compose -p mobius-test -f docker-compose.test.yml up -d >/dev/null 2>&1 \
  && ok "container up" || fail "container failed to start"

for i in $(seq 1 30); do
  hc=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health" 2>/dev/null)
  [ "$hc" = "200" ] && break
  sleep 1
done
[ "$hc" = "200" ] && ok "health=200" || fail "health=$hc after 30s"

curl -s -X POST "$BASE/api/auth/setup" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}' >/dev/null
docker exec mobius-test mkdir -p /data/cli-auth/claude
docker cp ~/.claude/.credentials.json mobius-test:/data/cli-auth/claude/.credentials.json
docker cp ~/.claude.json mobius-test:/data/cli-auth/claude/.claude.json 2>/dev/null
docker exec -u root mobius-test chown -R mobius:mobius /data/cli-auth
ok "creds copied + chowned"

TOK=$(curl -s -X POST "$BASE/api/auth/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=admin&password=admin' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
[ -n "$TOK" ] && ok "token acquired" || { fail "no token"; exit 1; }

# ── helpers ──────────────────────────────────────────────────────────
new_chat() {
  curl -s -X POST "$BASE/api/chats" \
    -H "Authorization: Bearer $TOK" \
    -H 'Content-Type: application/json' \
    -d '{"title":"sdk live test","provider":"claude"}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'
}

send_msg() {
  local chat_id=$1 content=$2
  curl -s -X POST "$BASE/api/chats/$chat_id/messages" \
    -H "Authorization: Bearer $TOK" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c "import json,sys;print(json.dumps({'content':sys.argv[1]}))" "$content")" \
    -o /dev/null -w '%{http_code}'
}

wait_done() {
  local chat_id=$1 timeout=${2:-90}
  for _ in $(seq 1 "$timeout"); do
    running=$(curl -s -H "Authorization: Bearer $TOK" "$BASE/api/chats/$chat_id" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("running"))' 2>/dev/null)
    if [ "$running" = "False" ] || [ "$running" = "false" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

get_session_id() {
  local chat_id=$1
  curl -s -H "Authorization: Bearer $TOK" "$BASE/api/chats/$chat_id" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id"))' 2>/dev/null
}

# ── flow 1: basic SDK turn ───────────────────────────────────────────
if [[ " $FLOWS " == *" 1 "* ]]; then
  step "flow 1: basic SDK turn"
  cid=$(new_chat)
  code=$(send_msg "$cid" "Just say hi. One word.")
  [ "$code" = "202" ] && ok "POST 202" || fail "POST $code"
  wait_done "$cid" 60 && ok "turn completed" || fail "turn timeout"
  sid=$(get_session_id "$cid")
  [ "$sid" != "None" ] && [ -n "$sid" ] && ok "session_id=$sid persisted" \
    || fail "session_id missing"
  docker exec mobius-test grep -q "sdk=claude" /data/logs/chat.log \
    && ok "sdk=claude log marker present" \
    || fail "sdk=claude log marker MISSING — SDK dispatch fell through"
fi

# ── flow 2: session resume across turns ──────────────────────────────
if [[ " $FLOWS " == *" 2 "* ]]; then
  step "flow 2: session resume (multi-turn)"
  cid=$(new_chat)
  send_msg "$cid" "Remember: my favorite number is 42." >/dev/null
  wait_done "$cid" 60 || fail "turn 1 timeout"
  sid1=$(get_session_id "$cid")
  send_msg "$cid" "What did I just tell you my favorite number is? Reply with just the digits." >/dev/null
  wait_done "$cid" 60 || fail "turn 2 timeout"
  sid2=$(get_session_id "$cid")
  [ "$sid1" = "$sid2" ] && ok "session_id preserved across turns ($sid1)" \
    || fail "session_id changed ($sid1 → $sid2) — resume not working"
  last=$(curl -s -H "Authorization: Bearer $TOK" "$BASE/api/chats/$cid" \
    | python3 -c '
import sys, json
chat = json.load(sys.stdin)
msgs = chat.get("messages", [])
for m in reversed(msgs):
  if m.get("role") == "assistant":
    content = m.get("content", "")
    if isinstance(content, list):
      content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    print(content[:200])
    break
')
  echo "$last" | grep -q '42' && ok "agent recalled '42' (resume context works)" \
    || fail "agent did NOT recall 42; got: $last"
fi

# ── flow 3: AskUserQuestion round-trip ───────────────────────────────
if [[ " $FLOWS " == *" 3 "* ]]; then
  step "flow 3: AskUserQuestion round-trip"
  cid=$(new_chat)
  # Send the message FIRST so the broadcast exists, THEN subscribe.
  # GET /stream returns 204 when no broadcast is registered (the
  # subscribe-before-send race silently dropped the SSE connection
  # the previous way). The catch-up burst replays prior events on
  # connect, so subscribing post-POST doesn't miss the question.
  send_msg "$cid" \
    "Use AskUserQuestion to ask me what kind of greeting I want (options: Hi, Hello, Hey). Just one question." \
    >/dev/null
  # stdbuf -oL keeps curl line-buffered when redirected to a file.
  stdbuf -oL curl -N -s -H "Authorization: Bearer $TOK" "$BASE/api/chats/$cid/stream" \
    > "$LOG_DIR/flow3-sse.log" 2>&1 &
  sse_pid=$!
  # Wait for question event in SSE
  for _ in $(seq 1 90); do
    grep -q '"type": "question"\|"type":"question"' "$LOG_DIR/flow3-sse.log" 2>/dev/null \
      && break
    sleep 1
  done
  grep -q '"type": "question"\|"type":"question"' "$LOG_DIR/flow3-sse.log" \
    && ok "question event received" \
    || fail "no question event after 90s"
  # Parse first question text
  qtext=$(python3 -c '
import json
with open("'$LOG_DIR'/flow3-sse.log") as f:
  for line in f:
    if line.startswith("data:") and "question" in line:
      try:
        data = json.loads(line[5:].strip())
        if data.get("type") == "question":
          qs = data.get("questions") or []
          if qs:
            print(qs[0].get("question", ""))
            break
      except Exception:
        pass
')
  [ -n "$qtext" ] && ok "question text='$qtext'" \
    || fail "could not parse question text"
  # Submit answer
  curl -s -X POST "$BASE/api/chats/$cid/messages" \
    -H "Authorization: Bearer $TOK" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c "
import json, sys
print(json.dumps({'content':'','hidden':True,'answers':{sys.argv[1]:'Hi'}}))
" "$qtext")" \
    -o /dev/null -w 'answer=%{http_code}\n' >> "$LOG_DIR/flow3-sse.log"
  # Wait for done
  wait_done "$cid" 120 && ok "turn completed after answer" \
    || fail "turn never completed after answer — answer delivery broken"
  # Check final assistant response includes "Hi"
  last=$(curl -s -H "Authorization: Bearer $TOK" "$BASE/api/chats/$cid" \
    | python3 -c '
import sys, json
chat = json.load(sys.stdin)
for m in reversed(chat.get("messages", [])):
  if m.get("role") == "assistant":
    content = m.get("content", "")
    if isinstance(content, list):
      content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    print(content[:200])
    break
')
  echo "$last" | grep -qi 'hi\|hello\|hey' \
    && ok "agent acknowledged the answer (response: $last)" \
    || fail "agent didn't use the answer — response: $last"
  kill $sse_pid 2>/dev/null
fi

# ── flow 4: Stop while running ───────────────────────────────────────
if [[ " $FLOWS " == *" 4 "* ]]; then
  step "flow 4: Stop mid-turn"
  cid=$(new_chat)
  send_msg "$cid" "Count slowly from 1 to 20, one number per line." >/dev/null
  sleep 3  # let it get going
  curl -s -X POST "$BASE/api/chat/stop" \
    -H "Authorization: Bearer $TOK" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c "import json,sys;print(json.dumps({'chat_id':sys.argv[1]}))" "$cid")" \
    -o /dev/null -w 'stop=%{http_code}\n'
  wait_done "$cid" 15 && ok "broadcast completed after stop" \
    || fail "broadcast still running after stop — mark_completed not fired"
fi

# ── Codex SDK flows (5-7) ────────────────────────────────────────────
# Off by default to avoid burning credits on every smoke. Set CODEX=1
# to run. Setup copies host's ~/.codex creds into the container and
# flips owner.provider to codex; reverts to claude at end.
if [ "$RUN_CODEX" = "1" ]; then
  step "Codex setup"
  if [ ! -f "$HOME/.codex/auth.json" ]; then
    fail "no ~/.codex/auth.json on host — run codex auth on host first"
  else
    docker exec mobius-test mkdir -p /data/cli-auth/codex
    docker cp "$HOME/.codex/auth.json" mobius-test:/data/cli-auth/codex/auth.json
    docker cp "$HOME/.codex/config.toml" mobius-test:/data/cli-auth/codex/config.toml 2>/dev/null
    docker exec -u root mobius-test chown -R mobius:mobius /data/cli-auth/codex
    ok "codex creds copied"
    curl -s -X POST "$BASE/api/settings" \
      -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
      -d '{"provider":"codex"}' >/dev/null
    cur=$(curl -s -H "Authorization: Bearer $TOK" "$BASE/api/settings" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("provider"))')
    [ "$cur" = "codex" ] && ok "provider=codex" || fail "provider switch failed (got: $cur)"
  fi

  new_codex_chat() {
    curl -s -X POST "$BASE/api/chats" \
      -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
      -d '{"title":"codex sdk live test"}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'
  }

  # ── flow 5: basic Codex SDK turn ───────────────────────────────────
  if [[ " $CODEX_FLOWS " == *" 5 "* ]]; then
    step "flow 5: basic Codex SDK turn"
    cid=$(new_codex_chat)
    code=$(send_msg "$cid" "Just say hi. One word.")
    [ "$code" = "202" ] && ok "POST 202" || fail "POST $code"
    wait_done "$cid" 60 && ok "turn completed" || fail "turn timeout"
    docker exec mobius-test grep -q "chat_id=$cid.*sdk=codex" /data/logs/chat.log \
      && ok "sdk=codex log marker present" \
      || fail "sdk=codex marker MISSING — fell through to subprocess"
  fi

  # ── flow 6: Codex multi-turn resume ────────────────────────────────
  if [[ " $CODEX_FLOWS " == *" 6 "* ]]; then
    step "flow 6: Codex session resume"
    cid=$(new_codex_chat)
    send_msg "$cid" "Remember: my favorite color is teal." >/dev/null
    wait_done "$cid" 60 || fail "turn 1 timeout"
    sid1=$(get_session_id "$cid")
    send_msg "$cid" "What did I just tell you my favorite color is? One word." >/dev/null
    wait_done "$cid" 60 || fail "turn 2 timeout"
    sid2=$(get_session_id "$cid")
    [ "$sid1" = "$sid2" ] && ok "session_id preserved ($sid1)" \
      || fail "session_id changed ($sid1 → $sid2) — resume broken"
    last=$(curl -s -H "Authorization: Bearer $TOK" "$BASE/api/chats/$cid" \
      | python3 -c '
import sys, json
chat = json.load(sys.stdin)
for m in reversed(chat.get("messages", [])):
  if m.get("role") == "assistant":
    content = m.get("content", "")
    if isinstance(content, list):
      content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    print(content[:200])
    break
')
    echo "$last" | grep -qi teal && ok "agent recalled 'teal' (resume works)" \
      || fail "agent did NOT recall teal; got: $last"
  fi

  # ── flow 7: Codex Stop mid-turn ────────────────────────────────────
  if [[ " $CODEX_FLOWS " == *" 7 "* ]]; then
    step "flow 7: Codex Stop mid-turn"
    cid=$(new_codex_chat)
    send_msg "$cid" "Count slowly from 1 to 20, one number per line." >/dev/null
    sleep 3
    curl -s -X POST "$BASE/api/chat/stop" \
      -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
      -d "$(python3 -c "import json,sys;print(json.dumps({'chat_id':sys.argv[1]}))" "$cid")" \
      -o /dev/null -w 'stop=%{http_code}\n'
    wait_done "$cid" 15 && ok "broadcast completed after stop" \
      || fail "broadcast still running after stop — SDK interrupt didn't fire mark_completed"
  fi

  # ── cleanup: restore provider to claude ────────────────────────────
  curl -s -X POST "$BASE/api/settings" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    -d '{"provider":"claude"}' >/dev/null
  ok "provider restored to claude"
fi

# ── summary ──────────────────────────────────────────────────────────
echo
if [ $exit_code -eq 0 ]; then
  echo "=== ALL FLOWS PASSED ==="
else
  echo "=== SOME FLOWS FAILED — see $LOG_DIR for capture ==="
fi
exit $exit_code
