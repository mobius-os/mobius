#!/bin/bash
# fetch.sh — the nightly "dreaming" wrapper. Thin by design: it owns
# only the OPERATIONAL concerns of an unattended cron run — no overlap,
# a wall-clock timeout, liveness heartbeats, an outcome event — and then
# hands the night to the agent.
#
# Unlike v1, this wrapper is NOT a security boundary. The Dreaming agent
# runs with FULL tools and a REAL token (no staging tree, no
# Bash-less/token-less envelope, no graph validation gate). It forks
# chats, consolidates the Mind graph, edits skills, fixes apps, writes
# the brief to reports/<date>.html via the storage API, opens the
# morning chat, and commits — all itself, instructed by its skill
# (/data/shared/skills/dreaming.md), per Möbius's "code empowers the
# agent; it does not police it." Reversibility comes from git, not from
# walls. So this file gathers a little read-only context for the agent,
# exports the few env vars its shell needs, runs the runner under a lock
# + timeout, and records how the night finished.
#
# Invoked by cron as: /data/apps/dreaming/fetch.sh <app_id>
# (the app id arrives as $1, per the cron-scaffold convention).
#
# DREAMING_DRY=1 skips the real agent run (records a dry outcome) so the
# plumbing — lock, inputs, env, heartbeat, cron_outcome — can be smoke-
# tested without spending a nightly run.
set -uo pipefail

APP_ID="${1:-}"
API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
DATA_DIR="${DATA_DIR:-/data}"
LOG="$DATA_DIR/cron-logs/dreaming.log"
LOCK="$DATA_DIR/cron-logs/dreaming.lock"
HEARTBEAT="$DATA_DIR/cron-logs/dreaming.heartbeat"
DATE="$(date +%F)"
INPUTS="$DATA_DIR/apps/dreaming/inputs"
RUNNER="${DREAMING_RUNNER:-/app/scripts/dreaming_runner.py}"
# Wall-clock cap for the whole night. Generous (the agent does real,
# multi-phase work) but bounded so a wedged run can't hold the lock past
# the next night's schedule. Overridable for tests.
RUN_TIMEOUT="${DREAMING_TIMEOUT:-7200}"

# CLI credentials the spawned claude/codex binary reads. Exported (not
# just set) so the runner and any subprocess it forks inherit them.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"
export CODEX_HOME="${CODEX_HOME:-$DATA_DIR/cli-auth/codex}"
export API_BASE_URL DATA_DIR

mkdir -p "$DATA_DIR/cron-logs" "$INPUTS"
log() { echo "[$(date -Iseconds)] dreaming: $*" >>"$LOG"; }

# emit_outcome <exit_code> — one cron_outcome activity event recording
# how the night finished, so the next night's agent (and the Dreaming
# app) can see the run history. Routed through the API so one process
# owns the activity-log file handle. Defined early because the token
# guard below emits a failure outcome before the main run.
emit_outcome() {
  local exit_code="$1"
  [[ -r "$TOKEN_FILE" ]] || return 0
  local token ts payload
  token="$(cat "$TOKEN_FILE")"
  ts="$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")"
  payload="$(printf '{"ev":"cron_outcome","ts":"%s","app_id":%s,"job":"dreaming","exit_code":%s}' \
    "$ts" "${APP_ID:-0}" "$exit_code")"
  curl -fsS -X POST \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "$API_BASE_URL/api/admin/activity/emit" >/dev/null 2>>"$LOG" || \
    log "WARN cron_outcome emit failed (rc=$exit_code)"
}

# --- no-overlap lock (flock) ------------------------------------------
# fd 9 holds the lock for the life of this process; flock -n fails fast
# if a prior night is still running (a long run that overran its window).
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another dreaming run holds the lock; exiting"
  exit 0
fi

if [[ -z "$APP_ID" ]]; then
  log "ERROR no app id passed as \$1; exiting"
  exit 2
fi

# --- token: export for the agent's shell (NOT a boundary) -------------
# The agent does its own privileged work (API reads, storage writes,
# notifications, git) using this token. We export it; we do NOT mediate
# the agent's use of it. A missing token means the agent can't reach the
# API, so fail loud rather than run a crippled night.
TOKEN_FILE="$DATA_DIR/service-token.txt"
if [[ ! -r "$TOKEN_FILE" ]]; then
  log "ERROR service token unreadable ($TOKEN_FILE) — is the instance signed out? exiting"
  emit_outcome 3
  exit 3
fi
SERVICE_TOKEN="$(cat "$TOKEN_FILE")"
# Both names: AGENT_TOKEN is what the skill's curl examples use; the
# wrapper-era scripts read SERVICE_TOKEN. Export both so either works.
export SERVICE_TOKEN AGENT_TOKEN="$SERVICE_TOKEN"
auth=(-H "Authorization: Bearer $SERVICE_TOKEN")

log "start (app_id=$APP_ID date=$DATE dry=${DREAMING_DRY:-0} timeout=${RUN_TIMEOUT}s)"

# --- gather read-only inputs for the agent ----------------------------
# The agent reads these from inputs/ as its starting context. It can (and
# does) gather more itself with its token — these are just the obvious
# 24h slices so it doesn't spend its first turns on boilerplate API
# calls. All best-effort: a failed gather leaves a stale/empty file and
# the agent copes.

# activity.jsonl — last 24h of platform events (app opens, storage
# writes, cron_outcomes). The runner's goal message points the agent here.
SINCE="$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
curl -s "${auth[@]}" "$API_BASE_URL/api/admin/activity?since=$SINCE" \
  >"$INPUTS/activity.jsonl" 2>>"$LOG" || true

# chats.md — recent chats list (titles + ids + provider), so the agent
# knows which sessions to fork-and-interview without re-deriving the list.
python3 - "$API_BASE_URL" "$SERVICE_TOKEN" >"$INPUTS/chats.md" 2>>"$LOG" <<'PY' || true
import json, sys, urllib.request
base, token = sys.argv[1], sys.argv[2]
def get(path):
    req = urllib.request.Request(base+path, headers={"Authorization": "Bearer "+token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)
print("# Recent chats (fork + interview the ones with activity)\n")
try:
    chats = get("/api/chats")
    chats = chats if isinstance(chats, list) else chats.get("chats", [])
    chats = sorted(chats, key=lambda c: c.get("updated_at",""), reverse=True)[:12]
    for c in chats:
        cid = c.get("id"); title = c.get("title") or "(untitled)"
        prov = c.get("provider") or "claude"
        updated = c.get("updated_at","")
        print(f"- `{cid}`  [{prov}]  {title}  (updated {updated})")
    if not chats:
        print("(no chats)")
except Exception as e:
    print(f"(could not list chats: {e})")
PY

# prev-report.html — yesterday's brief, so the agent doesn't repeat
# itself. Enumerate via the listing endpoint, fetch the newest report.
PREV="$(curl -s "${auth[@]}" "$API_BASE_URL/api/storage/apps-list/$APP_ID/reports/" 2>>"$LOG" \
  | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin); es=[e["name"] for e in d.get("entries",[]) if e.get("name","").endswith(".html")]
  print(sorted(es)[-1] if es else "")
except Exception: print("")' 2>>"$LOG")"
if [[ -n "$PREV" ]]; then
  curl -s "${auth[@]}" "$API_BASE_URL/api/storage/apps/$APP_ID/reports/$PREV" \
    >"$INPUTS/prev-report.html" 2>>"$LOG" || true
fi

# Record the app id where the runner's goal message and the agent can
# find it (the agent writes reports to apps/<app_id>/reports/).
printf '%s\n' "$APP_ID" >"$INPUTS/app_id"
log "gathered inputs (activity, chats, prev-report) into $INPUTS/"

# --- heartbeat: prove liveness while the long run is in flight --------
# A background loop touches the heartbeat file every 60s. A monitor (or a
# morning glance) can `stat` it to tell "still dreaming" from "wedged".
# Killed in the cleanup trap below.
#
# fd 9 (the flock handle) is CLOSED in the child (`9>&-`) so the lock is
# held ONLY by the main process. Without this, the backgrounded child
# inherits fd 9 and keeps the lock alive past the parent's exit until the
# child is reaped — so the NEXT night's run would spuriously see "another
# run holds the lock" and skip. The cleanup trap kills the child and
# waits for it so the lock is fully released by the time we exit.
heartbeat_loop() {
  while true; do
    date -Iseconds >"$HEARTBEAT" 2>/dev/null || true
    sleep 60
  done
}
heartbeat_loop 9>&- &
HEARTBEAT_PID=$!
cleanup() {
  if [[ -n "${HEARTBEAT_PID:-}" ]]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- run the agent: full tools, real token, no sandbox ----------------
# The runner loads the dreaming skill as the system prompt, sends the
# goal as the first user message, and drives the multi-turn loop. `timeout`
# bounds wall-clock; --signal=TERM gives the run a chance to flush before
# SIGKILL (--kill-after). The runner streams its own trace into $LOG.
RC=0
if [[ "${DREAMING_DRY:-0}" == "1" ]]; then
  log "DRY run: skipping agent; recording dry outcome"
  RC=0
elif [[ ! -r "$RUNNER" ]]; then
  log "ERROR runner not found/readable at $RUNNER; exiting"
  RC=127
else
  timeout --signal=TERM --kill-after=60 "$RUN_TIMEOUT" \
    python3 "$RUNNER" >>"$LOG" 2>&1
  RC=$?
  if [[ "$RC" == "124" ]]; then
    log "WARN agent run hit the ${RUN_TIMEOUT}s timeout (terminated)"
  elif [[ "$RC" != "0" ]]; then
    log "WARN agent run exited non-zero (rc=$RC)"
  fi
fi

# --- final safety-net commit ------------------------------------------
# The agent commits as it goes (pm-commit per chunk). This is a backstop:
# if the run was killed mid-chunk, sweep any agent-touched files into one
# commit so nothing is left dirty + unreversible. pm-commit's denylist +
# 50-file guard keep this honest; --allow-broad because a full night can
# legitimately touch many files (skills, memory notes, app sources).
if command -v pm-commit >/dev/null 2>&1; then
  ( cd "$DATA_DIR" && pm-commit --allow-broad "dreaming: nightly safety-net commit $DATE" \
      >>"$LOG" 2>&1 ) || true
fi

# --- emit cron_outcome ------------------------------------------------
emit_outcome "$RC"

log "done (rc=$RC)"
exit "$RC"
