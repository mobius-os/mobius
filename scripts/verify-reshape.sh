#!/usr/bin/env bash
# verify-reshape.sh — post-deploy gate for the three-layer agent context
# (core.md constitution + /data/shared/skills/ + /data/shared/memory/ graph)
# and any installed Memory + Reflection catalog apps. Confirms the reshape took effect
# INSIDE a running container — not just that the image deployed. Read-only.
#
# Run it after scripts/deploy-prod.sh (or against mobius-test after a test
# deploy). Exit 0 if all checks pass, 1 otherwise.
#
# Usage:
#   scripts/verify-reshape.sh                      # container 'mobius', port 8000
#   scripts/verify-reshape.sh --container mobius-test --port 8000
set -uo pipefail

C="mobius"; PORT="8000"
while [ $# -gt 0 ]; do
  case "$1" in
    --container) C="${2:?}"; shift 2 ;;
    --port)      PORT="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1 (use --container/--port)" >&2; exit 2 ;;
  esac
done
BASE="http://localhost:$PORT"

PASS=0; FAIL=0
c_ok=$'\033[1;32m'; c_err=$'\033[1;31m'; c_off=$'\033[0m'
ok()  { printf '%s  ok %s%s\n' "$c_ok"  "$c_off" "$*"; PASS=$((PASS+1)); }
bad() { printf '%s  XX %s%s\n' "$c_err" "$c_off" "$*" >&2; FAIL=$((FAIL+1)); }
dex() { docker exec "$C" sh -c "$1" 2>/dev/null; }

if ! docker inspect "$C" >/dev/null 2>&1; then echo "container '$C' not found" >&2; exit 2; fi
echo "== verify-reshape against '$C' ($BASE) =="

# 1. system prompt resolves to the constitution.
sp="$(dex 'cd /app && python3 -c "from app.providers import get_skill_path; print(get_skill_path())"')"
case "$sp" in */core.md) ok "system prompt = core.md" ;; *) bad "system prompt NOT core.md: ${sp:-<none>}" ;; esac

# 2. skills seeded (8 expected).
nsk="$(dex 'ls /data/shared/skills/*.md 2>/dev/null | wc -l' | tr -d ' ')"
[ "${nsk:-0}" -ge 8 ] && ok "$nsk skills at /data/shared/skills/" || bad "only ${nsk:-0} skills (expected >=8)"

# 3. memory graph ready + graph injection mode (not the legacy flat fallback).
[ "$(dex 'ls /data/shared/memory/.ready >/dev/null 2>&1 && echo y')" = "y" ] \
  && ok "memory graph .ready present" || bad "memory graph .ready missing"
mode="$(dex 'cd /app && python3 -c "from app import memory; print(memory.build_memory_block(\"/data\").mode)"')"
[ "$mode" = "graph" ] && ok "memory injection mode = graph" || bad "memory injection mode = ${mode:-<none>} (expected graph)"

# 4. Memory + Reflection are optional catalog apps, not required platform core apps.
TOK="$(docker exec "$C" cat /data/service-token.txt 2>/dev/null)"
dex "curl -s -H 'Authorization: Bearer $TOK' $BASE/api/apps/ > /tmp/vr_apps.json"
source_for() {
  dex "python3 -c \"import json,sys; slug=sys.argv[1]; apps=json.load(open('/tmp/vr_apps.json')); print(next((a.get('source_dir') or '' for a in apps if a.get('slug')==slug), ''))\" $1"
}
check_optional_catalog_app() {
  local slug="$1" src
  src="$(source_for "$slug")"
  if [ -z "$src" ]; then
    ok "$slug app not installed (optional)"
  elif [ "${src#/data/apps/}" != "$src" ]; then
    ok "$slug app installed from /data/apps"
  else
    bad "$slug app installed from unexpected source_dir: $src"
  fi
}
check_optional_catalog_app memory
check_optional_catalog_app reflection

# 5. app crons exist only when their apps are installed.
for slug in memory reflection; do
  src="$(source_for "$slug")"
  [ -n "$src" ] || continue
  cron="$(dex "crontab -u mobius -l 2>/dev/null | grep -c '$slug/fetch.sh'" | tr -d ' ')"
  [ "${cron:-0}" -ge 1 ] && ok "$slug cron installed" || bad "$slug cron NOT installed"
done

# 6. deployed reflection skill has the brief-path FIX (writes reports/<date>.html,
#    not briefs/). We check the fix, not byte-identity with the seed — the
#    Reflection agent legitimately edits its own skill over time.
dm=/data/shared/skills/reflection.md
nrep="$(dex "grep -c 'reports/' $dm 2>/dev/null" | tr -d ' ')"
nbrief="$(dex "grep -c 'briefs/<date>\|briefs/\$(date' $dm 2>/dev/null" | tr -d ' ')"
if [ "${nrep:-0}" -ge 1 ] && [ "${nbrief:-0}" -eq 0 ]; then
  ok "reflection skill writes briefs to reports/ (fix present)"
else
  bad "reflection skill brief path looks wrong (reports refs=$nrep, stale briefs refs=$nbrief)"
fi

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
