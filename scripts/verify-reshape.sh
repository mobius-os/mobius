#!/usr/bin/env bash
# verify-reshape.sh — post-deploy gate for the three-layer agent context
# (core.md constitution + /data/shared/skills/ + /data/shared/memory/ graph)
# and the Mind + Dreaming core apps. Confirms the reshape actually took effect
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

# 4. Mind + Dreaming registered.
TOK="$(docker exec "$C" cat /data/service-token.txt 2>/dev/null)"
dex "curl -s -H 'Authorization: Bearer $TOK' $BASE/api/apps/ > /tmp/vr_apps.json"
have() { dex "python3 -c \"import json,sys; print('yes' if any(a.get('slug')==sys.argv[1] for a in json.load(open('/tmp/vr_apps.json'))) else 'no')\" $1"; }
[ "$(have mind)" = "yes" ]     && ok "Mind app registered"     || bad "Mind app NOT registered"
[ "$(have dreaming)" = "yes" ] && ok "Dreaming app registered" || bad "Dreaming app NOT registered"

# 5. dreaming cron installed for the mobius user.
cron="$(dex 'crontab -u mobius -l 2>/dev/null | grep -c "dreaming/fetch.sh"' | tr -d ' ')"
[ "${cron:-0}" -ge 1 ] && ok "dreaming cron installed" || bad "dreaming cron NOT installed"

# 6. deployed dreaming skill has the brief-path FIX (writes reports/<date>.html,
#    not briefs/). We check the fix, not byte-identity with the seed — the
#    Dreaming agent legitimately edits its own skill over time.
dm=/data/shared/skills/dreaming.md
nrep="$(dex "grep -c 'reports/' $dm 2>/dev/null" | tr -d ' ')"
nbrief="$(dex "grep -c 'briefs/<date>\|briefs/\$(date' $dm 2>/dev/null" | tr -d ' ')"
if [ "${nrep:-0}" -ge 1 ] && [ "${nbrief:-0}" -eq 0 ]; then
  ok "dreaming skill writes briefs to reports/ (fix present)"
else
  bad "dreaming skill brief path looks wrong (reports refs=$nrep, stale briefs refs=$nbrief)"
fi

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
