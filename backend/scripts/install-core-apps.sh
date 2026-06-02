#!/bin/bash
# install-core-apps.sh — first-boot install of Möbius's two CORE apps
# (memory-graph viewer + dreaming) from baked source, plus the nightly
# dreaming cron. Idempotent: skips an app that's already registered.
#
# Runs AFTER the server is up (the entrypoint backgrounds it post-launch and
# it polls /api/health first) because registration goes through the API — the
# same path register_app.py / the agent use. The service token at
# /data/service-token.txt is the owner JWT, so it authorizes registration.
#
# Core-app source is baked at /app/core-apps/<slug>/. The dreaming app also
# ships prompt.md + fetch.sh, which are copied to /data/apps/dreaming/ so the
# cron can run them.
set -uo pipefail

API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
DATA_DIR="${DATA_DIR:-/data}"
CORE_SRC="/app/core-apps"
LOG="$DATA_DIR/cron-logs/install-core-apps.log"
mkdir -p "$DATA_DIR/cron-logs"
log() { echo "[$(date -Iseconds)] install-core-apps: $*" >>"$LOG"; }

# Source the baked source from the in-repo path too (dev / test bind-mounts).
[[ -d "$CORE_SRC" ]] || CORE_SRC="$(cd "$(dirname "$0")/../../core-apps" 2>/dev/null && pwd || echo /nonexistent)"

# Wait for health (up to ~60s).
for i in $(seq 1 60); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "$API_BASE_URL/api/health" 2>/dev/null)" == "200" ]] && break
  sleep 1
done

TOKEN_FILE="$DATA_DIR/service-token.txt"
if [[ ! -r "$TOKEN_FILE" ]]; then log "ERROR no service token; skipping"; exit 0; fi
TOKEN="$(cat "$TOKEN_FILE")"
export AGENT_TOKEN="$TOKEN" API_BASE_URL

apps_json="$(curl -s -H "Authorization: Bearer $TOKEN" "$API_BASE_URL/api/apps/" 2>>"$LOG")"

# has_app <slug> -> echoes the numeric id if registered, else empty.
has_app() {
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
slug = sys.argv[1]
try:
    apps = json.load(sys.stdin)
    apps = apps if isinstance(apps, list) else apps.get("apps", [])
    for a in apps:
        if a.get("slug") == slug or a.get("name","").lower().replace(" ","-") == slug:
            print(a.get("id")); break
except Exception:
    pass
PY
}

register() {  # <slug> <Name> <description> ; echoes new id
  local slug="$1" name="$2" desc="$3"
  mkdir -p "$DATA_DIR/apps/$slug"
  cp "$CORE_SRC/$slug/index.jsx" "$DATA_DIR/apps/$slug/index.jsx"
  python3 /app/scripts/register_app.py "$name" "$desc" "$DATA_DIR/apps/$slug/index.jsx" 2>>"$LOG" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")'
}

# --- memory-graph -----------------------------------------------------
mg_id="$(echo "$apps_json" | has_app memory-graph)"
if [[ -z "$mg_id" ]]; then
  mg_id="$(register memory-graph "Memory Graph" "Visualize what Möbius knows about you — an Obsidian-style graph of its memory.")"
  log "registered memory-graph (id=$mg_id)"
else
  log "memory-graph already installed (id=$mg_id)"
fi
# Set the app icon (kg-t1: glossy infinity-as-graph, the owner's pick). Raw PNG
# bytes; the route downscales + stores. Idempotent — fine to re-PUT each boot.
if [[ -n "$mg_id" && -f "$CORE_SRC/memory-graph/icon.png" ]]; then
  curl -s -X PUT -H "Authorization: Bearer $TOKEN" --data-binary @"$CORE_SRC/memory-graph/icon.png" \
    "$API_BASE_URL/api/apps/$mg_id/icon" -o /dev/null -w 'memory-graph icon: HTTP %{http_code}\n' >>"$LOG" 2>&1 || true
fi

# --- dreaming ---------------------------------------------------------
dr_id="$(echo "$apps_json" | has_app dreaming)"
if [[ -z "$dr_id" ]]; then
  dr_id="$(register dreaming "Dreaming" "Your nightly morning brief — Möbius works while you sleep and reports back.")"
  log "registered dreaming (id=$dr_id)"
else
  log "dreaming already installed (id=$dr_id)"
fi

# Ship the dreaming cron machinery + install the schedule (idempotent).
if [[ -n "$dr_id" ]]; then
  mkdir -p "$DATA_DIR/apps/dreaming"
  cp "$CORE_SRC/dreaming/fetch.sh" "$DATA_DIR/apps/dreaming/fetch.sh"
  cp "$CORE_SRC/dreaming/prompt.md" "$DATA_DIR/apps/dreaming/prompt.md"
  chmod +x "$DATA_DIR/apps/dreaming/fetch.sh"
  # offline_capable: the report viewer just reads cached HTML.
  curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"offline_capable": true}' "$API_BASE_URL/api/apps/$dr_id" >>"$LOG" 2>&1 || true
  # Install the nightly cron pointing at fetch.sh with the app id as $1.
  bash /app/scripts/init-cron-scaffold.sh dreaming "0 6 * * *" fetch.sh "$dr_id" >>"$LOG" 2>&1 \
    && log "installed dreaming cron (0 6 * * *, app_id=$dr_id)" \
    || log "WARN dreaming cron install failed (see log)"
fi

log "done (memory-graph=$mg_id dreaming=$dr_id)"
