#!/usr/bin/env bash
#
# Self-driven offline reproduction harness for the in-shell mini-app path.
#
# WHY THIS EXISTS: the cold-reopen-offline "Snake spinner forever" bug only
# manifests in a real browser with a real service worker going offline, and we
# were relying on the user to airplane-test by hand each iteration. This drives
# a headless browser through the exact sequence and reads back the iframe DOM
# + screenshots, so we can iterate without the user.
#
# It targets the mobius-test container (port 8001) per the repo rule that live
# UI tests never hit prod. It expects an owner account + at least one
# offline_capable app already present in that instance; if not, see SETUP below.
#
# Usage:
#   bash scripts/offline-repro.sh            # full run
#   APPID=22 bash scripts/offline-repro.sh   # probe a specific app id
#
# Output: writes step-by-step results to /tmp/offline-repro/out.txt and
# screenshots to /tmp/offline-repro/*.png.
set -u
BASE="${BASE:-http://localhost:8001}"
USER="${USER_NAME:-admin}"
PASS="${PASS:-admin}"
OUT=/tmp/offline-repro
mkdir -p "$OUT"
export PATH=/home/hmzmrzx/projects/node_modules/.bin:$PATH
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/out.txt"; }

: > "$OUT/out.txt"
log "=== offline-repro against $BASE ==="

# 0. health
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health" || true)
log "health=$code"
[ "$code" = "200" ] || { log "ABORT: $BASE not healthy"; exit 1; }

# 1. fresh browser, online, log in.
agent-browser close --all >/dev/null 2>&1 || true
agent-browser open "$BASE/" >/dev/null 2>&1
sleep 3
agent-browser screenshot "$OUT/01-open.png" >/dev/null 2>&1
# Inject owner token directly if a login API token is obtainable (faster +
# deterministic than driving the login form).
TOK=$(curl -s -X POST "$BASE/api/auth/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "username=$USER&password=$PASS" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
log "token_len=${#TOK}"
if [ -n "$TOK" ]; then
  agent-browser eval "localStorage.setItem('token', $(python3 -c "import json;print(json.dumps('$TOK'))')); location.reload(); 'ok'" >/dev/null 2>&1
  sleep 4
fi

# 2. discover an offline_capable app id (or use $APPID).
if [ -z "${APPID:-}" ]; then
  APPID=$(curl -s "$BASE/api/apps/" -H "Authorization: Bearer $TOK" \
    | python3 -c 'import sys,json
a=json.load(sys.stdin)
cap=[x for x in a if x.get("offline_capable")]
print((cap[0] if cap else (a[0] if a else {})).get("id",""))' 2>/dev/null || true)
fi
log "APPID=$APPID"
[ -n "$APPID" ] || { log "ABORT: no app to test"; exit 1; }

# 3. WARM online: open the app in-shell so SW caches frame+module+react+theme.
agent-browser eval "(async()=>{try{await navigator.serviceWorker.ready;await fetch('/api/apps/$APPID/frame',{cache:'reload'});await fetch('/api/theme',{cache:'reload'});return 'warmed';}catch(e){return 'warm-err '+e.message;}})()" >/dev/null 2>&1
# Navigate to the app via hash/route so AppCanvas mounts and caches via SW.
agent-browser eval "location.href='/app/$APPID'; 'go'" >/dev/null 2>&1
sleep 5
agent-browser screenshot "$OUT/02-app-online.png" >/dev/null 2>&1
agent-browser eval "(()=>{const f=document.querySelector('iframe.canvas');return JSON.stringify({hasIframe:!!f, src:f&&f.getAttribute('src'), spinner:!!document.querySelector('.canvas-loading')});})()" 2>&1 | tail -1 | tee -a "$OUT/out.txt"

# 4. GO OFFLINE and cold-reopen (reload while offline = cold boot from SW).
agent-browser set offline on >/dev/null 2>&1
log "set offline on"
agent-browser eval "location.href='/'; 'home'" >/dev/null 2>&1
sleep 4
agent-browser eval "location.href='/app/$APPID'; 'reopen-app'" >/dev/null 2>&1
sleep 8
agent-browser screenshot "$OUT/03-app-offline.png" >/dev/null 2>&1

# 5. observe iframe state.
log "--- iframe state offline ---"
agent-browser eval "(()=>{const f=document.querySelector('iframe.canvas');const spin=document.querySelector('.canvas-loading');let inner='';try{inner=f&&f.contentDocument?f.contentDocument.getElementById('root')?.innerHTML?.slice(0,80):'(no root)';}catch(e){inner='(cross?)'+e.message;}return JSON.stringify({hasIframe:!!f,spinnerVisible:!!spin,iframeRoot:inner});})()" 2>&1 | tail -1 | tee -a "$OUT/out.txt"

agent-browser set offline off >/dev/null 2>&1
agent-browser close --all >/dev/null 2>&1
