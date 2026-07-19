#!/usr/bin/env bash
# preship-gate.sh — mechanical gate that MUST pass before any push to a
# shared branch or prod deploy. Born from twice shipping files with git
# conflict markers (a `<<<<<<<` in a .py is a SyntaxError → backend dead).
#
# Exits non-zero on the first failure. Run it as its OWN step; only push /
# deploy if it returned 0. Never batch this with the push itself.
#
#   bash scripts/preship-gate.sh            # backend + marker checks (fast)
#   bash scripts/preship-gate.sh --full     # + full pytest + Node-24 build
#   PRESHIP_OVERRIDE=1 bash scripts/preship-gate.sh   # bypass the CI-status gate
#
# Intentionally has NO side effects — it only reads and reports.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FULL=0; [ "${1:-}" = "--full" ] && FULL=1
fail() { echo "GATE FAIL: $1" >&2; exit 1; }

# Latest CI on main must be green before we ship onto it. Fail CLOSED:
# any state we can't positively confirm as a successful completed run —
# gh missing/unauthed, rate-limited, network down, zero runs, or a
# non-success conclusion (failure/cancelled/timed_out) — is a GATE FAIL.
# A green-looking local tree on top of a red main is exactly how a push
# lands on broken upstream code; the local checks below can't see that.
# `gh run list --branch main --status completed --limit 1` lets GitHub do
# the filtering server-side (we don't fetch 5 and grep). Override with
# PRESHIP_OVERRIDE=1 when you've eyeballed CI yourself or main has no runs
# yet (brand-new repo). NOTE: this check assumes GitHub CI exists
# (.github/workflows/ + a remote); it is NOT omitted-as-moot — CI is live.
echo "[1/5] main CI status (fail-closed)"
if [ "${PRESHIP_OVERRIDE:-0}" = "1" ]; then
  echo "  SKIPPED — PRESHIP_OVERRIDE=1"
elif ! command -v gh >/dev/null 2>&1; then
  fail "gh CLI not found — cannot confirm main CI is green (set PRESHIP_OVERRIDE=1 to bypass)"
else
  ci_conclusion="$(gh run list --branch main --status completed --limit 1 \
    --json conclusion --jq '.[0].conclusion' 2>/dev/null)" || ci_conclusion=""
  if [ -z "$ci_conclusion" ] || [ "$ci_conclusion" = "null" ]; then
    fail "could not determine main CI status (gh unauthed / rate-limited / no completed runs) — set PRESHIP_OVERRIDE=1 to bypass"
  elif [ "$ci_conclusion" != "success" ]; then
    fail "latest completed main CI run is '$ci_conclusion', not success — fix main or set PRESHIP_OVERRIDE=1 to bypass"
  fi
  echo "  ok — latest completed main CI: success"
fi

echo "[2/5] conflict markers"
# `git grep` only tracked content; check working tree too.
if grep -rnE '^(<{7}|={7}|>{7})( |$)' --include='*.py' --include='*.js' \
   --include='*.jsx' --include='*.mjs' --include='*.html' \
   backend/app frontend/src frontend/public 2>/dev/null | grep .; then
  fail "unresolved conflict markers (above)"
fi
echo "  ok — none"

echo "[3/5] python syntax (changed + key files)"
python3 - <<'PY' || fail "python syntax error (above)"
import ast, glob, sys
bad = 0
for f in glob.glob('backend/app/**/*.py', recursive=True):
    try:
        ast.parse(open(f, encoding='utf-8').read())
    except SyntaxError as e:
        print(f'  {f}:{e.lineno}: {e.msg}'); bad += 1
sys.exit(1 if bad else 0)
PY
echo "  ok — all backend .py parse"

if [ "$FULL" = "0" ]; then
  echo "[4/5] SKIPPED full pytest (use --full)"
  echo "[5/5] SKIPPED frontend build (use --full)"
  echo "GATE PASS (fast)"; exit 0
fi

echo "[4/5] full backend pytest (worktree-scoped container)"
slug="$(basename "$PWD" | sed 's/^session-//')"
# pytest's EXIT CODE is the source of truth (non-zero on any failure or a
# non-pytest error like a missing image) — grepping "N passed" off the tail
# is fragile to plugin output ordering and can both false-pass (a stray
# "N passed" in unrelated output) and false-fail (summary pushed past the
# last 3 lines). Keep the grep only for the human-readable count on success.
if ! docker compose -p "mobius-test-$slug" -f docker-compose.test.yml \
     --project-directory "$PWD" run --rm pytest >/tmp/preship-pytest.log 2>&1; then
  tail -15 /tmp/preship-pytest.log >&2
  fail "pytest not green (see /tmp/preship-pytest.log)"
fi
echo "  ok — $(grep -oE '[0-9]+ passed' /tmp/preship-pytest.log | tail -1)"

echo "[5/5] frontend build (Node 24) + offline-build check"
docker run --rm -v "$PWD/frontend":/app -w /app node:24-slim sh -c \
  "npm install --no-audit --no-fund >/tmp/preship-npm.log 2>&1 && \
   npm run build >/tmp/preship-build.log 2>&1 && \
   node scripts/check-offline-build.mjs" >/tmp/preship-fe.log 2>&1 \
  || { tail -15 /tmp/preship-build.log /tmp/preship-fe.log >&2; fail "frontend build/check failed"; }
echo "  ok — $(tail -1 /tmp/preship-fe.log)"

echo "GATE PASS (full)"
