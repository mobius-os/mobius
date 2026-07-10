#!/bin/bash
# install-core-apps.sh — installs Möbius's CORE apps from the platform source
# tree, plus the nightly reflection cron. In the normal post-defrost runtime,
# the editable source of truth is /data/platform/core-apps/<slug>; /app is only
# a recovery floor for first boot / broken-platform fallback.
#
# Runs AFTER the server is up (the entrypoint backgrounds it post-launch and
# it polls /api/health first) because registration goes through the API — the
# same path register_app.py / the agent use. The service token at
# /data/service-token.txt is the owner JWT, so it authorizes registration.
#
# Core app UI source is not copied into /data/apps. That directory remains for
# ordinary user/store app repos, per-app numeric storage, and tiny runtime
# sidecars such as reflection's cron replay file.
#
# core-apps/ is NOT hand-edited: it is a committed snapshot of the catalog repos
# (mobius-os/app-<slug>), pinned by commit in core-apps/SOURCES and regenerated
# by scripts/sync-core-apps.sh. The catalog repo is the single source of truth;
# CI (scripts/check-core-apps-sync.sh) fails the build if the two ever drift.
set -uo pipefail

API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
DATA_DIR="${DATA_DIR:-/data}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE_SRC="/app/core-apps"
# Post-defrost the platform clone is the SERVED source of truth and the baked
# /app tree is the recovery floor — prefer the platform copy so a platform
# deploy of core-app machinery isn't silently reverted by the next boot's
# unconditional re-copy (the entrypoint applies the same preference to the
# backend it serves).
[[ -d "$DATA_DIR/platform/core-apps" ]] && CORE_SRC="$DATA_DIR/platform/core-apps"
CORE_SRC_MODE="baked"
[[ "$CORE_SRC" == "$DATA_DIR/platform/core-apps" ]] && CORE_SRC_MODE="platform"
LOG="$DATA_DIR/cron-logs/install-core-apps.log"
mkdir -p "$DATA_DIR/cron-logs"
log() { echo "[$(date -Iseconds)] install-core-apps: $*" >>"$LOG"; }

# Source the baked source from the in-repo path too (dev / test bind-mounts).
[[ -d "$CORE_SRC" ]] || CORE_SRC="$(cd "$(dirname "$0")/../../core-apps" 2>/dev/null && pwd || echo /nonexistent)"
[[ "$CORE_SRC" == "$DATA_DIR/platform/core-apps" ]] && CORE_SRC_MODE="platform"

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
# not registered). In baked fallback mode a non-empty manifest_url means the app
# was installed from the catalog/store, so the recovery floor must not clobber
# its working tree. In platform mode core apps migrate to platform ownership.
# Same argv-not-stdin program form as has_app.
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

app_source_dir() {
  echo "$apps_json" | python3 -c '
import json, sys
slug = sys.argv[1]
try:
    apps = json.load(sys.stdin)
    apps = apps if isinstance(apps, list) else apps.get("apps", [])
    for a in apps:
        if a.get("slug") == slug or a.get("name","").lower().replace(" ","-") == slug:
            print(a.get("source_dir") or ""); break
except Exception:
    pass
' "$1" 2>/dev/null
}

source_dir_claimed() {
  echo "$apps_json" | python3 -c '
import json, os, sys
target = os.path.abspath(sys.argv[1])
try:
    apps = json.load(sys.stdin)
    apps = apps if isinstance(apps, list) else apps.get("apps", [])
    for a in apps:
        source_dir = a.get("source_dir") or ""
        if source_dir and os.path.abspath(source_dir) == target:
            print("1"); break
except Exception:
    pass
' "$1" 2>/dev/null
}

core_source_hash() {
  local dir="$1"
  (
    cd "$dir" 2>/dev/null || exit 0
    find . -type f \
      ! -path './.git/*' \
      ! -path './node_modules/*' \
      ! -path './tests/*' \
      -print0 \
      | sort -z \
      | xargs -0 sha256sum
  ) 2>/dev/null | sha256sum | cut -d' ' -f1
}

desired_sync_hash() {
  local source_hash="$1" desc="$2"
  printf '%s\0%s' "$source_hash" "$desc" | sha256sum | cut -d' ' -f1
}

copy_core_source_tree() {
  local src_dir="$1" dst_dir="$2"
  mkdir -p "$dst_dir"
  (
    cd "$src_dir" || exit 1
    tar --exclude='./.git' --exclude='./node_modules' --exclude='./tests' -cf - .
  ) | (
    cd "$dst_dir" || exit 1
    tar -xf -
  )
}

cleanup_legacy_core_source_tree() {
  local slug="$1" src_dir="$2" legacy_dir="$3"
  [[ "$CORE_SRC_MODE" == "platform" ]] || return 0
  [[ -d "$src_dir" && -d "$legacy_dir" ]] || return 0
  case "$legacy_dir" in
    "$DATA_DIR/apps/"*) : ;;
    *) return 0 ;;
  esac
  local base
  base="$(basename "$legacy_dir")"
  [[ "$base" =~ ^[0-9]+$ ]] && return 0
  local archive_root archive_dir
  archive_root="$DATA_DIR/cron-logs/core-app-sync/legacy-source-archive"
  archive_dir="$archive_root/$slug-$(date +%s)-$$"
  if [[ "$slug" != "reflection" ]]; then
    mkdir -p "$archive_root"
    mv "$legacy_dir" "$archive_dir" 2>/dev/null \
      && log "quarantined legacy $slug source at $archive_dir" \
      || true
    return 0
  fi
  (
    cd "$src_dir" || exit 0
    find . -type f \
      ! -path './.git/*' \
      ! -path './node_modules/*' \
      ! -path './tests/*' \
      -print
  ) | while IFS= read -r rel; do
    local legacy_file="$legacy_dir/${rel#./}"
    [[ -f "$legacy_file" ]] || continue
    mkdir -p "$archive_dir/$(dirname "${rel#./}")"
    mv "$legacy_file" "$archive_dir/${rel#./}" 2>/dev/null || true
  done
  if [[ -f "$legacy_dir/.baked-source.sha256" ]]; then
    mkdir -p "$archive_dir"
    mv "$legacy_dir/.baked-source.sha256" "$archive_dir/.baked-source.sha256" 2>/dev/null || true
  fi
  find "$legacy_dir" -depth -type d -empty -delete 2>/dev/null || true
}

cleanup_unclaimed_legacy_core_source_tree() {
  local slug="$1" src_dir="$2" legacy_dir="$3"
  [[ "$CORE_SRC_MODE" == "platform" ]] || return 0
  [[ -d "$legacy_dir" ]] || return 0
  [[ -n "$(source_dir_claimed "$legacy_dir")" ]] && return 0
  cleanup_legacy_core_source_tree "$slug" "$src_dir" "$legacy_dir"
}

write_core_cron_replay() {
  local slug="$1" schedule="$2" job_path="$3" app_id="$4"
  local runtime_dir="$DATA_DIR/apps/$slug"
  local init_path="$runtime_dir/init-cron.sh"
  mkdir -p "$runtime_dir"
  cat > "$init_path" <<INIT
#!/bin/sh
# Restores the cron entry for the "$slug" core app on container restart.
ENTRY="$schedule $job_path $app_id"
ERRFILE=\$(mktemp)
EXISTING=\$(crontab -u mobius -l 2>"\$ERRFILE"); RC=\$?
if [ "\$RC" -eq 0 ]; then
  (printf '%s\n' "\$EXISTING" | grep -vF "$job_path"; echo "\$ENTRY") | crontab -u mobius -
elif grep -qi 'no crontab for' "\$ERRFILE"; then
  echo "\$ENTRY" | crontab -u mobius -
else
  echo "init-cron($slug): crontab read error (rc=\$RC); leaving crontab unchanged" >&2
  cat "\$ERRFILE" >&2
fi
rm -f "\$ERRFILE"
INIT
  chmod +x "$init_path" 2>/dev/null || true
  bash "$init_path"
}

# sync_core_app <slug> <Name> <description> [src_slug] [dst_slug] ; echoes the app id.
#
# register_app.py is create-OR-update. In platform mode it registers the core
# app directly from /data/platform/core-apps/<slug>; in baked fallback mode it
# first copies to /data/apps/<slug> because /app is not an approved editable
# source root. A hash sentinel gates re-registration so ordinary restarts do not
# churn bundles, while platform deploys and metadata changes land on next boot.
sync_core_app() {
  local slug="$1" name="$2" desc="$3"
  local src_slug="${4:-$slug}" dst_slug="${5:-$slug}"
  # Durable owner-suppression: if the owner uninstalled this core app, a marker
  # file persists under /data (it survives reboots AND the 7-day tombstone TTL
  # purge). Honor it and do NOT re-create the app — the deletion stays deleted
  # until the owner brings it back (recover within the TTL, or reinstall from
  # the store; both clear the marker). Skipping here also avoids the stale
  # cp-over-preserved-source a blind re-seed does during the TTL window.
  # Path is kept in lockstep with core_app_suppress._SUPPRESS_SUBDIR. Only slugs
  # in core_app_suppress.SUPPRESSIBLE_CORE_SLUGS ever get a marker (memory,
  # reflection, beat-machine). For reflection, returning early here ALSO skips the reflection
  # cron block below (gated on a non-empty id), so its nightly run — brief +
  # memory-graph consolidation — stops. That's the intended "uninstall the
  # feature" semantic (owner call 2026-07-06); the built graph is untouched.
  if [[ -f "$DATA_DIR/shared/suppressed-core-apps/$slug" ]]; then
    log "$slug is owner-suppressed (uninstalled) — skipping core-app seed"
    return
  fi
  local src_dir="$CORE_SRC/$src_slug"
  local src="$src_dir/index.jsx"
  local dst_dir="$DATA_DIR/apps/$dst_slug"
  local register_dir="$src_dir"
  local hashfile="$DATA_DIR/cron-logs/core-app-sync/$slug.sha256"
  local legacy_dirs="$DATA_DIR/apps/$dst_slug"
  [[ "$dst_slug" != "$slug" ]] && legacy_dirs="$legacy_dirs:$DATA_DIR/apps/$slug"
  if [[ "$CORE_SRC_MODE" != "platform" ]]; then
    register_dir="$dst_dir"
    hashfile="$dst_dir/.baked-source.sha256"
  fi
  if [[ ! -f "$src" ]]; then
    log "WARN $slug missing core source ($src); skipping core-app seed"
    return
  fi
  mkdir -p "$(dirname "$hashfile")"
  local baked_hash desired_hash live_hash existing_id existing_source existing_manifest
  baked_hash="$(core_source_hash "$src_dir")"
  desired_hash="$(desired_sync_hash "$baked_hash" "$desc")"
  live_hash="$(cat "$hashfile" 2>/dev/null || echo none)"
  existing_id="$(has_app "$slug")"
  existing_source="$(app_source_dir "$slug")"
  existing_manifest="$(app_manifest_url "$slug")"
  if [[ "$CORE_SRC_MODE" == "platform" && -n "$existing_id" ]]; then
    cleanup_unclaimed_legacy_core_source_tree "$slug" "$src_dir" "$DATA_DIR/apps/$dst_slug"
    [[ "$dst_slug" != "$slug" ]] \
      && cleanup_unclaimed_legacy_core_source_tree "$slug" "$src_dir" "$DATA_DIR/apps/$slug"
  fi
  # Store-managed apps used to own their lifecycle through the App Store. That
  # remains true only when we're running from the baked recovery floor: do not
  # overwrite a store-updated app with stale image contents. In platform mode,
  # the core slugs are platform-owned and should migrate in place even if an
  # older row still carries manifest_url.
  if [[
    "$CORE_SRC_MODE" != "platform" &&
    -n "$existing_id" &&
    -n "$existing_manifest"
  ]]; then
    log "$slug is store-managed (manifest_url set) — baked fallback leaving its UI to the store (id=$existing_id)"
    echo "$existing_id"
    return
  fi
  if [[
    -n "$existing_id" &&
    "$desired_hash" == "$live_hash" &&
    "$existing_source" == "$register_dir" &&
    ( "$CORE_SRC_MODE" != "platform" || -z "$existing_manifest" )
  ]]; then
    log "$slug unchanged since last sync (id=$existing_id)"
    echo "$existing_id"
    return
  fi
  if [[ ! -r "$src" ]]; then
    log "ERROR core source unreadable for $slug ($src) — skipping sync"
    echo "$existing_id"
    return
  fi
  if [[ "$CORE_SRC_MODE" != "platform" ]]; then
    mkdir -p "$dst_dir"
    copy_core_source_tree "$src_dir" "$dst_dir"
  fi
  local id
  id="$(MOBIUS_REGISTER_LEGACY_SOURCE_DIRS="$legacy_dirs" \
    python3 "$SCRIPT_DIR/register_app.py" "$name" "$desc" "$register_dir/index.jsx" 2>>"$LOG" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")')"
  # Record the sentinel ONLY when registration returned an id. A failed
  # register (empty id) that still wrote the sentinel would poison it: the
  # next boot sees hash-match, takes the skip path, and the app is never
  # installed (the cron + icon blocks below are gated on a non-empty id).
  if [[ -n "$desired_hash" && -n "$id" ]]; then
    echo "$desired_hash" > "$hashfile"
    [[ "$existing_source" == "$DATA_DIR/apps/$dst_slug" ]] \
      && cleanup_legacy_core_source_tree "$slug" "$src_dir" "$DATA_DIR/apps/$dst_slug"
    [[ "$dst_slug" != "$slug" && "$existing_source" == "$DATA_DIR/apps/$slug" ]] \
      && cleanup_legacy_core_source_tree "$slug" "$src_dir" "$DATA_DIR/apps/$slug"
  fi
  log "synced $slug from $CORE_SRC_MODE core source (id=$id source_dir=$register_dir)"
  echo "${id:-$existing_id}"
}

# --- Memory -------------------------------------------------------------
# offline_capable stays FALSE (the app default; not PATCHed here). Memory reads
# the live shared graph at /data/shared/memory/graph.json + per-note markdown;
# offline support would need those cached/synced, not just the JSX. The store
# manifest (app-memory/mobius.json) declares false to match — keep all three
# (manifest, schema default, this script) in agreement if that ever changes.
memory_app_id="$(sync_core_app memory "Memory" "What Möbius knows about you — an Obsidian-style graph of its memory it grows over time.")"
# Set the app icon (kg-t1: glossy infinity-as-graph, the owner's pick). Raw PNG
# bytes; the route downscales + stores. Idempotent — fine to re-PUT each boot.
if [[ -n "$memory_app_id" && -f "$CORE_SRC/memory/icon.png" ]]; then
  curl -s -X PUT -H "Authorization: Bearer $TOKEN" --data-binary @"$CORE_SRC/memory/icon.png" \
    "$API_BASE_URL/api/apps/$memory_app_id/icon" -o /dev/null -w 'memory icon: HTTP %{http_code}\n' >>"$LOG" 2>&1 || true
fi

# Migration (kg-t1 rename): Memory supersedes the old "Memory Graph" viewer. On
# instances that had the predecessor, install-core-apps registers Memory as a NEW
# app and would otherwise leave both in the drawer. Soft-archive the old one by
# renaming (it owns no unique data — it read the shared graph — so we keep the
# row for audit/recovery rather than hard-deleting). Idempotent + a no-op on
# fresh instances and on prod (where the orphan was already removed).
if [[ -n "$memory_app_id" ]]; then
  old_mg_id="$(has_app memory-graph)"
  if [[ -n "$old_mg_id" && "$old_mg_id" != "$memory_app_id" ]]; then
    if curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d '{"name":"Memory Graph (archived)"}' "$API_BASE_URL/api/apps/$old_mg_id" >>"$LOG" 2>&1; then
      log "archived predecessor Memory Graph app (id=$old_mg_id; superseded by Memory id=$memory_app_id)"
    else
      log "WARN failed to archive old Memory Graph app (id=$old_mg_id) — drawer may show both"
    fi
  fi
fi

# --- reflection ---------------------------------------------------------
reflection_app_id="$(sync_core_app reflection "Reflection" "Your nightly morning brief — Möbius works while you sleep and reports back.")"

# Ship the reflection cron machinery + install the schedule (idempotent). The
# job runs from the platform core source tree; /data/apps/reflection only holds
# runtime inputs/settings plus replay/helper wrappers.
if [[ -n "$reflection_app_id" ]]; then
  mkdir -p "$DATA_DIR/apps/reflection"
  HELPER_SRC="/app/scripts"
  [[ -f "$DATA_DIR/platform/backend/scripts/fork-chat.sh" ]] \
    && HELPER_SRC="$DATA_DIR/platform/backend/scripts"
  cat > "$DATA_DIR/apps/reflection/fork-chat.sh" <<WRAP
#!/bin/sh
exec bash "$HELPER_SRC/fork-chat.sh" "\$@"
WRAP
  cat > "$DATA_DIR/apps/reflection/fork-session.sh" <<WRAP
#!/bin/sh
exec bash "$HELPER_SRC/fork-session.sh" "\$@"
WRAP
  chmod +x "$CORE_SRC/reflection/fetch.sh" \
    "$DATA_DIR/apps/reflection/fork-chat.sh" \
    "$DATA_DIR/apps/reflection/fork-session.sh" 2>/dev/null || true
  # offline_capable: the report viewer just reads cached HTML.
  curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"offline_capable": true}' "$API_BASE_URL/api/apps/$reflection_app_id" >>"$LOG" 2>&1 || true
  # Install the nightly cron pointing at fetch.sh with the app id as $1.
  write_core_cron_replay reflection "0 6 * * *" "$CORE_SRC/reflection/fetch.sh" "$reflection_app_id" >>"$LOG" 2>&1 \
    && log "installed reflection cron (0 6 * * *, app_id=$reflection_app_id, job=$CORE_SRC/reflection/fetch.sh)" \
    || log "WARN reflection cron install failed (see log)"
fi

# --- Beat Machine -------------------------------------------------------
# Canonical app slug is `beat-machine`; older prod rows used
# `/data/apps/beatmachine`. Pass that legacy path to register_app.py so the row
# migrates in place to the platform source without creating a duplicate.
beat_machine_app_id="$(sync_core_app beat-machine "Beat Machine" "A native step sequencer with synthesized drums, custom recordings, and simple effects." beat-machine beatmachine)"
if [[ -n "$beat_machine_app_id" ]]; then
  if [[ -f "$CORE_SRC/beat-machine/icon.png" ]]; then
    curl -s -X PUT -H "Authorization: Bearer $TOKEN" --data-binary @"$CORE_SRC/beat-machine/icon.png" \
      "$API_BASE_URL/api/apps/$beat_machine_app_id/icon" -o /dev/null -w 'beat-machine icon: HTTP %{http_code}\n' >>"$LOG" 2>&1 || true
  fi
  curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"offline_capable": true}' "$API_BASE_URL/api/apps/$beat_machine_app_id" >>"$LOG" 2>&1 || true
fi

log "done (memory=$memory_app_id reflection=$reflection_app_id beat-machine=$beat_machine_app_id)"
