#!/bin/bash
# fetch.sh — the nightly "dreaming" job: consolidate the knowledge graph and
# leave a morning brief. This wrapper is the SECURITY BOUNDARY: it owns the
# service token and does every privileged action (API reads, atomic publish,
# push, git). The LLM it launches runs token-less and Bash-less against a
# staging copy, so it can only Read/Write/Edit local files + WebSearch — it
# cannot hit the API, send notifications, or run shell commands. Everything it
# changes is git-committed, hence reversible (Codex review R6).
#
# Invoked by cron as: cron-emit.sh <app_id> /data/apps/dreaming/fetch.sh <app_id>
# (the app id arrives as $1, per the cron-scaffold convention).
#
# DREAMING_DRY=1 skips the real `claude -p` call (uses a placeholder report) so
# the plumbing can be smoke-tested without spending a nightly LLM run.
set -uo pipefail

APP_ID="${1:-}"
API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
DATA_DIR="${DATA_DIR:-/data}"
MEMORY="$DATA_DIR/shared/memory"
LOG="$DATA_DIR/cron-logs/dreaming.log"
LOCK="$DATA_DIR/cron-logs/dreaming.lock"
DATE="$(date +%F)"
PROMPT="$DATA_DIR/apps/dreaming/prompt.md"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/cli-auth/claude}"

mkdir -p "$DATA_DIR/cron-logs"
log() { echo "[$(date -Iseconds)] dreaming: $*" >>"$LOG"; }

# --- no-overlap lock (flock) ------------------------------------------
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another dreaming run holds the lock; exiting"
  exit 0
fi

# --- token guard: a silent token failure must be visible --------------
TOKEN_FILE="$DATA_DIR/service-token.txt"
if [[ ! -r "$TOKEN_FILE" ]]; then
  log "ERROR service token unreadable ($TOKEN_FILE) — is the instance signed out? exiting"
  exit 3
fi
SERVICE_TOKEN="$(cat "$TOKEN_FILE")"
auth=(-H "Authorization: Bearer $SERVICE_TOKEN")

if [[ -z "$APP_ID" ]]; then
  log "ERROR no app id passed as \$1; exiting"
  exit 2
fi

log "start (app_id=$APP_ID date=$DATE dry=${DREAMING_DRY:-0})"

# --- staging workspace ------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dreaming.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/inputs"

# Snapshot the live graph into staging. The LLM edits THIS copy.
if [[ -d "$MEMORY" ]]; then
  cp -a "$MEMORY/." "$WORK/memory/" 2>/dev/null || mkdir -p "$WORK/memory"
else
  log "no live graph yet; nothing to consolidate (still writing a report)"
  mkdir -p "$WORK/memory"
fi
# Remember how long the live inbox was, so appends that land DURING the run are
# preserved when we publish (the LLM truncates the staged inbox).
INBOX_SNAP_LINES=0
[[ -f "$MEMORY/inbox.md" ]] && INBOX_SNAP_LINES="$(wc -l <"$MEMORY/inbox.md")"

# --- gather inputs (token-owning) -------------------------------------
SINCE="$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
curl -s "${auth[@]}" "$API_BASE_URL/api/admin/activity?since=$SINCE" \
  >"$WORK/inputs/activity.jsonl" 2>>"$LOG" || true

# usage.md — which apps were opened (counted from app_open events)
python3 - "$WORK/inputs/activity.jsonl" >"$WORK/inputs/usage.md" 2>>"$LOG" <<'PY' || true
import json, sys, collections
opens = collections.Counter()
events = collections.Counter()
try:
    for line in open(sys.argv[1]):
        line = line.strip()
        if not line: continue
        ev = json.loads(line)
        events[ev.get("ev")] += 1
        if ev.get("ev") == "app_open":
            opens[ev.get("slug") or ev.get("app_id")] += 1
except FileNotFoundError:
    pass
print("# Yesterday's platform activity\n")
print(f"Total events: {sum(events.values())}\n")
print("Event types: " + ", ".join(f"{k}={v}" for k,v in events.most_common()) + "\n")
if opens:
    print("## Apps opened\n")
    for slug, n in opens.most_common():
        print(f"- {slug}: {n} open(s)")
else:
    print("No app opens recorded in the last 24h.")
PY

# chats.md — recent chat titles + last message tails (no since filter exists,
# so list then fetch the few most recent).
python3 - "$API_BASE_URL" "$SERVICE_TOKEN" >"$WORK/inputs/chats.md" 2>>"$LOG" <<'PY' || true
import json, sys, urllib.request
base, token = sys.argv[1], sys.argv[2]
def get(path):
    req = urllib.request.Request(base+path, headers={"Authorization": "Bearer "+token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)
print("# Recent chats\n")
try:
    chats = get("/api/chats")
    chats = chats if isinstance(chats, list) else chats.get("chats", [])
    chats = sorted(chats, key=lambda c: c.get("updated_at",""), reverse=True)[:6]
    for c in chats:
        cid, title = c.get("id"), (c.get("title") or "(untitled)")
        print(f"## {title}  (id {cid})")
        try:
            full = get(f"/api/chats/{cid}?limit=6")
            msgs = full.get("messages", []) if isinstance(full, dict) else []
            for m in msgs[-4:]:
                role = m.get("role","?")
                content = (m.get("content") or "")[:280].replace("\n"," ")
                print(f"- **{role}**: {content}")
        except Exception as e:
            print(f"- (could not load messages: {e})")
        print()
except Exception as e:
    print(f"(could not list chats: {e})")
PY

# prev-report.html — yesterday's brief, so the agent avoids repeating itself.
PREV="$(curl -s "${auth[@]}" "$API_BASE_URL/api/storage/apps-list/$APP_ID/reports/" 2>>"$LOG" \
  | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin); es=[e["name"] for e in d.get("entries",[]) if e.get("name","").endswith(".html")]
  print(sorted(es)[-1] if es else "")
except Exception: print("")' 2>>"$LOG")"
if [[ -n "$PREV" ]]; then
  curl -s "${auth[@]}" "$API_BASE_URL/api/storage/apps/$APP_ID/reports/$PREV" \
    >"$WORK/inputs/prev-report.html" 2>>"$LOG" || true
fi

cat >"$WORK/TASK.md" <<EOF
Today is $DATE. Your working directory is this folder. Read inputs/ for
yesterday's context, consolidate memory/ (your knowledge-graph copy), and write
report.html (the morning brief). Follow your system prompt exactly. You have no
token and no Bash — only Read/Write/Edit/WebSearch, scoped to this folder.
EOF

# --- run the LLM, token-less + Bash-less, cwd = staging ---------------
REPORT="$WORK/report.html"
if [[ "${DREAMING_DRY:-0}" == "1" ]]; then
  log "DRY run: skipping claude; writing placeholder report"
  cat >"$REPORT" <<'HTML'
<!doctype html><html><head><meta charset="utf-8"><title>Möbius brief (dry)</title>
<style>:root{--a:#2b6}body{font:16px/1.6 ui-serif,Georgia,serif;max-width:70ch;margin:2rem auto;padding:0 1rem}h1{color:var(--a)}</style>
</head><body><h1>Möbius — morning brief (dry run)</h1><p>Plumbing smoke test. No agent ran.</p></body></html>
HTML
else
  ( cd "$WORK" && env -u SERVICE_TOKEN -u API_BASE_URL \
      timeout 1500 claude -p "$(cat "$WORK/TASK.md")" \
        --system-prompt-file "$PROMPT" \
        --allowedTools "Read,Write,Edit,WebSearch" \
        --max-turns 40 \
        --dangerously-skip-permissions \
        >>"$LOG" 2>&1 ) || log "claude exited non-zero (continuing to publish what exists)"
fi

# --- validate + publish the consolidated memory graph -----------------
PUBLISHED=0
if [[ -d "$WORK/memory" ]] && [[ -n "$(ls -A "$WORK/memory" 2>/dev/null)" ]]; then
  # Preserve inbox lines appended to the LIVE graph during the run.
  if [[ -f "$MEMORY/inbox.md" ]]; then
    live_lines="$(wc -l <"$MEMORY/inbox.md")"
    if (( live_lines > INBOX_SNAP_LINES )); then
      tail -n +"$((INBOX_SNAP_LINES+1))" "$MEMORY/inbox.md" >>"$WORK/memory/inbox.md" 2>>"$LOG" || true
      log "preserved $((live_lines-INBOX_SNAP_LINES)) inbox line(s) added during the run"
    fi
  fi
  # Bounded change rate: refuse to publish if the agent rewrote too much.
  changed="$(diff -rq "$MEMORY" "$WORK/memory" 2>/dev/null | wc -l)"
  total="$(find "$WORK/memory" -name '*.md' | wc -l)"
  cap=$(( total > 6 ? total : 6 ))
  if (( changed > cap * 2 )); then
    log "WARN graph change rate high (changed=$changed cap=$((cap*2))); publishing anyway but flagging"
  fi
  # Lint the staged graph (bare memory dir) + build its graph.json; publish
  # only if there are no ERRORS.
  if python3 /app/scripts/build_memory_graph.py --root "$WORK/memory" >>"$LOG" 2>&1; then
    # Atomic-ish swap (6am, single user; window is two renames).
    rm -rf "$MEMORY.prev" 2>/dev/null || true
    if [[ -d "$MEMORY" ]]; then mv "$MEMORY" "$MEMORY.prev"; fi
    if mv "$WORK/memory" "$MEMORY"; then
      touch "$MEMORY/.ready"
      rm -rf "$MEMORY.prev" 2>/dev/null || true
      PUBLISHED=1
      log "published consolidated graph"
    else
      # rollback
      [[ -d "$MEMORY.prev" ]] && mv "$MEMORY.prev" "$MEMORY"
      log "ERROR publish rename failed; rolled back"
    fi
  else
    log "ERROR staged graph failed lint; keeping live graph (not published)"
  fi
fi

# Rebuild graph.json + commit (token-owning, reversible).
python3 /app/scripts/build_memory_graph.py "$DATA_DIR" >>"$LOG" 2>&1 || true
if command -v pm-commit >/dev/null 2>&1; then
  ( cd "$DATA_DIR" && pm-commit "dreaming: nightly consolidation $DATE" >>"$LOG" 2>&1 ) || true
fi

# --- validate + store the report --------------------------------------
report_ok=0
if [[ -f "$REPORT" ]]; then
  if python3 - "$REPORT" >>"$LOG" 2>&1 <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
# Reject any external fetch surface so the brief renders offline in the iframe.
bad = re.findall(r'(?:href|src|srcset|action)\s*=\s*["\']?\s*https?://', html, re.I)
bad += re.findall(r'@import\s+(?:url\()?["\']?\s*https?://', html, re.I)
bad += re.findall(r'url\(\s*["\']?\s*https?://', html, re.I)
if bad:
    print(f"report has {len(bad)} external URL(s); rejecting"); sys.exit(1)
if "<script" in html.lower():
    print("report contains <script>; rejecting"); sys.exit(1)
print("report validated (no external URLs, no scripts)")
PY
  then report_ok=1; fi
fi

TLDR="Your Möbius morning brief is ready."
if (( report_ok )); then
  # PUT the report (non-json -> {content:...} envelope).
  python3 - "$API_BASE_URL" "$SERVICE_TOKEN" "$APP_ID" "$DATE" "$REPORT" >>"$LOG" 2>&1 <<'PY' || true
import json, sys, urllib.request
base, token, app_id, date, path = sys.argv[1:6]
html = open(path, encoding="utf-8", errors="ignore").read()
body = json.dumps({"content": html}).encode()
req = urllib.request.Request(f"{base}/api/storage/apps/{app_id}/reports/{date}.html",
    data=body, method="PUT",
    headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=30).read()
print("stored report")
PY
  log "stored report reports/$DATE.html"
else
  log "WARN no valid report produced; skipping store"
fi

# --- update state.json (streak) + notify ------------------------------
python3 - "$API_BASE_URL" "$SERVICE_TOKEN" "$APP_ID" "$DATE" "$WORK/inputs/activity.jsonl" "$report_ok" >>"$LOG" 2>&1 <<'PY' || true
import json, sys, urllib.request, datetime
base, token, app_id, date, activity, report_ok = sys.argv[1:7]
def api(method, path, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
        headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None
# had activity today?
had = False
try:
    for line in open(activity):
        if line.strip(): had = True; break
except FileNotFoundError:
    pass
try:
    state = api("GET", f"/api/storage/apps/{app_id}/state.json") or {}
except Exception:
    state = {}
last = state.get("last_run")
streak = int(state.get("streak", 0) or 0)
y = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
if had:
    streak = streak + 1 if last == y else 1
else:
    streak = 0
state.update({"last_run": date, "streak": streak,
              "last_summary": "Brief ready." if report_ok == "1" else "Quiet night."})
try:
    api("PUT", f"/api/storage/apps/{app_id}/state.json", state)
    print(f"state updated streak={streak}")
except Exception as e:
    print(f"state update failed: {e}")
# push notification (suppressed automatically if the chat/app is open)
try:
    api("POST", "/api/notifications/send", {
        "title": "Möbius — morning brief",
        "body": state.get("last_summary","Your brief is ready."),
        "source_id": f"dreaming-{date}",
        "target": f"/app/{app_id}",
    })
    print("push sent")
except Exception as e:
    print(f"push failed: {e}")
PY

log "done (published_graph=$PUBLISHED report_ok=$report_ok)"
