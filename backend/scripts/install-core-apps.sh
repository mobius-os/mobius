#!/bin/bash
# install-core-apps.sh — installs Möbius's two CORE apps (Memory, the
# memory-graph viewer; and Reflection, the nightly brief) from baked source,
# plus the nightly reflection cron. Idempotent + deploy-aware: registers a
# missing app, and re-syncs an existing app's UI from baked source only when
# the baked jsx changed since the last sync (see sync_core_app).
#
# Runs AFTER the server is up (the entrypoint backgrounds it post-launch and
# it polls /api/health first) because registration goes through the API — the
# same path register_app.py / the agent use. The service token at
# /data/service-token.txt is the owner JWT, so it authorizes registration.
#
# Core-app source is baked at /app/core-apps/<slug>/. The reflection app also
# ships prompt.md + fetch.sh, which are copied to /data/apps/reflection/ so the
# cron can run them.
#
# core-apps/ is NOT hand-edited: it is a committed snapshot of the catalog repos
# (mobius-os/app-<slug>), pinned by commit in core-apps/SOURCES and regenerated
# by scripts/sync-core-apps.sh. The catalog repo is the single source of truth;
# CI (scripts/check-core-apps-sync.sh) fails the build if the two ever drift.
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

# has_app <slug> -> echoes the numeric id of the registered app (matched
# on slug or slugified name) from the global apps_json, else empty. The
# program is passed via -c (argv), NOT a
# stdin heredoc: `python3 - <<'PY'` would consume stdin as the program
# text, leaving json.load(sys.stdin) at EOF — so the piped apps_json
# would never be read and every lookup would (silently) return empty.
has_app() {
  echo "$apps_json" | python3 -c '
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
' "$1" 2>/dev/null
}

# app_manifest_url <slug> -> echoes the app's manifest_url (empty if none or
# not registered). A non-empty manifest_url means the app was installed from
# the catalog/store and owns its own update lifecycle — baked-sync must not
# clobber its working tree. Same argv-not-stdin program form as has_app.
app_manifest_url() {
  echo "$apps_json" | python3 -c '
import json, sys
slug = sys.argv[1]
try:
    apps = json.load(sys.stdin)
    apps = apps if isinstance(apps, list) else apps.get("apps", [])
    for a in apps:
        if a.get("slug") == slug or a.get("name","").lower().replace(" ","-") == slug:
            print(a.get("manifest_url") or ""); break
except Exception:
    pass
' "$1" 2>/dev/null
}

# sync_core_app <slug> <Name> <description> ; echoes the app id.
#
# register_app.py is create-OR-update (it PATCHes jsx_source when an app of
# the same name exists), so calling it always re-syncs the baked UI. But we
# gate that on the baked source actually having CHANGED since the last sync:
# core apps are platform-owned, yet the agent may still improve a core app's
# UI (e.g. the Reflection brief renderer), and Möbius treats agent edits as
# first-class. The hash sentinel means a platform DEPLOY (new baked jsx)
# propagates on the next boot, while an ordinary restart leaves any
# post-deploy agent edits untouched. First boot after this mechanism ships
# has no sentinel → treated as changed → installs/updates once.
sync_core_app() {
  local slug="$1" name="$2" desc="$3"
  local src="$CORE_SRC/$slug/index.jsx"
  local dst_dir="$DATA_DIR/apps/$slug"
  local hashfile="$dst_dir/.baked-jsx.sha256"
  mkdir -p "$dst_dir"
  local baked_hash live_hash existing_id
  baked_hash="$(sha256sum "$src" 2>/dev/null | cut -d' ' -f1)"
  live_hash="$(cat "$hashfile" 2>/dev/null || echo none)"
  existing_id="$(has_app "$slug")"
  # Store-managed apps (installed from the catalog, carrying a manifest_url)
  # own their update lifecycle via the store — the owner re-installs from the
  # manifest. Baked-sync must NOT cp baked source over a store-installed
  # working tree (that regressed prod Memory: served a stale baked vX over the
  # store-installed vY). Skip the JSX sync for them entirely, keeping baked-
  # sync a FIRST-INSTALL-ONLY fallback for instances that never reached the
  # catalog. Platform machinery copied OUTSIDE this function (e.g. the reflection
  # cron scripts) is separate and still runs.
  if [[ -n "$existing_id" && -n "$(app_manifest_url "$slug")" ]]; then
    log "$slug is store-managed (manifest_url set) — leaving its UI to the store (id=$existing_id)"
    echo "$existing_id"
    return
  fi
  if [[ -n "$existing_id" && "$baked_hash" == "$live_hash" ]]; then
    log "$slug unchanged since last sync (id=$existing_id)"
    echo "$existing_id"
    return
  fi
  if [[ ! -r "$src" ]]; then
    log "ERROR baked source unreadable for $slug ($src) — skipping sync"
    echo "$existing_id"
    return
  fi
  cp "$src" "$dst_dir/index.jsx"
  local id
  id="$(python3 /app/scripts/register_app.py "$name" "$desc" "$dst_dir/index.jsx" 2>>"$LOG" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")')"
  # Record the sentinel ONLY when registration returned an id. A failed
  # register (empty id) that still wrote the sentinel would poison it: the
  # next boot sees hash-match, takes the skip path, and the app is never
  # installed (the cron + icon blocks below are gated on a non-empty id).
  [[ -n "$baked_hash" && -n "$id" ]] && echo "$baked_hash" > "$hashfile"
  log "synced $slug from baked source (id=$id)"
  echo "${id:-$existing_id}"
}

# --- Memory -------------------------------------------------------------
# offline_capable stays FALSE (the app default; not PATCHed here). Memory reads
# the live shared graph at /data/shared/memory/graph.json + per-note markdown;
# offline support would need those cached/synced, not just the JSX. The store
# manifest (app-memory/mobius.json) declares false to match — keep all three
# (manifest, schema default, this script) in agreement if that ever changes.
mg_id="$(sync_core_app memory "Memory" "What Möbius knows about you — an Obsidian-style graph of its memory it grows over time.")"
# Set the app icon (kg-t1: glossy infinity-as-graph, the owner's pick). Raw PNG
# bytes; the route downscales + stores. Idempotent — fine to re-PUT each boot.
if [[ -n "$mg_id" && -f "$CORE_SRC/memory/icon.png" ]]; then
  curl -s -X PUT -H "Authorization: Bearer $TOKEN" --data-binary @"$CORE_SRC/memory/icon.png" \
    "$API_BASE_URL/api/apps/$mg_id/icon" -o /dev/null -w 'memory icon: HTTP %{http_code}\n' >>"$LOG" 2>&1 || true
fi

# Migration (kg-t1 rename): Memory supersedes the old "Memory Graph" viewer. On
# instances that had the predecessor, install-core-apps registers Memory as a NEW
# app and would otherwise leave both in the drawer. Soft-archive the old one by
# renaming (it owns no unique data — it read the shared graph — so we keep the
# row for audit/recovery rather than hard-deleting). Idempotent + a no-op on
# fresh instances and on prod (where the orphan was already removed).
if [[ -n "$mg_id" ]]; then
  old_mg_id="$(has_app memory-graph)"
  if [[ -n "$old_mg_id" && "$old_mg_id" != "$mg_id" ]]; then
    if curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d '{"name":"Memory Graph (archived)"}' "$API_BASE_URL/api/apps/$old_mg_id" >>"$LOG" 2>&1; then
      log "archived predecessor Memory Graph app (id=$old_mg_id; superseded by Memory id=$mg_id)"
    else
      log "WARN failed to archive old Memory Graph app (id=$old_mg_id) — drawer may show both"
    fi
  fi
fi

# --- reflection ---------------------------------------------------------
dr_id="$(sync_core_app reflection "Reflection" "Your nightly morning brief — Möbius works while you sleep and reports back.")"

# Ship the reflection cron machinery + install the schedule (idempotent).
# fetch.sh + the fork helpers are platform machinery (a thin runner wrapper
# and the introspection utilities) — always re-copied from baked source, no
# version gate: the agent edits the reflection SKILL, not these.
if [[ -n "$dr_id" ]]; then
  mkdir -p "$DATA_DIR/apps/reflection"
  cp "$CORE_SRC/reflection/fetch.sh" "$DATA_DIR/apps/reflection/fetch.sh"
  # Introspection helpers the Reflection agent calls to fork + interview chats
  # and app subagent runs (the heart of the nightly loop).
  cp /app/scripts/fork-chat.sh "$DATA_DIR/apps/reflection/fork-chat.sh" 2>/dev/null || true
  cp /app/scripts/fork-session.sh "$DATA_DIR/apps/reflection/fork-session.sh" 2>/dev/null || true
  chmod +x "$DATA_DIR/apps/reflection/fetch.sh" \
    "$DATA_DIR/apps/reflection/fork-chat.sh" "$DATA_DIR/apps/reflection/fork-session.sh" 2>/dev/null || true
  # offline_capable: the report viewer just reads cached HTML.
  curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"offline_capable": true}' "$API_BASE_URL/api/apps/$dr_id" >>"$LOG" 2>&1 || true
  # Install the nightly cron pointing at fetch.sh with the app id as $1.
  bash /app/scripts/init-cron-scaffold.sh reflection "0 6 * * *" fetch.sh "$dr_id" >>"$LOG" 2>&1 \
    && log "installed reflection cron (0 6 * * *, app_id=$dr_id)" \
    || log "WARN reflection cron install failed (see log)"
fi

log "done (memory=$mg_id reflection=$dr_id)"
