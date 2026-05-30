#!/usr/bin/env bash
# preship-gate.sh — mechanical gate that MUST pass before any push to a
# shared branch or prod deploy. Born from twice shipping files with git
# conflict markers (a `<<<<<<<` in a .py is a SyntaxError → backend dead).
#
# Exits non-zero on the first failure. Run it as its OWN step; only push /
# deploy if it returned 0. Never batch this with the push itself.
#
#   bash scripts/preship-gate.sh            # backend + marker checks (fast)
#   bash scripts/preship-gate.sh --full     # + full pytest + Node-22 build
#
# Intentionally has NO side effects — it only reads and reports.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FULL=0; [ "${1:-}" = "--full" ] && FULL=1
fail() { echo "GATE FAIL: $1" >&2; exit 1; }

echo "[1/4] conflict markers"
# `git grep` only tracked content; check working tree too.
if grep -rnE '^(<{7}|={7}|>{7})( |$)' --include='*.py' --include='*.js' \
   --include='*.jsx' --include='*.mjs' --include='*.html' \
   backend/app frontend/src frontend/public 2>/dev/null | grep .; then
  fail "unresolved conflict markers (above)"
fi
echo "  ok — none"

echo "[2/4] python syntax (changed + key files)"
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
  echo "[3/4] SKIPPED full pytest (use --full)"
  echo "[4/4] SKIPPED frontend build (use --full)"
  echo "GATE PASS (fast)"; exit 0
fi

echo "[3/4] full backend pytest (worktree-scoped container)"
slug="$(basename "$PWD" | sed 's/^session-//')"
docker compose -p "mobius-test-$slug" -f docker-compose.test.yml \
  --project-directory "$PWD" run --rm pytest >/tmp/preship-pytest.log 2>&1
if ! tail -3 /tmp/preship-pytest.log | grep -qE '[0-9]+ passed'; then
  tail -15 /tmp/preship-pytest.log >&2
  fail "pytest not green (see /tmp/preship-pytest.log)"
fi
echo "  ok — $(grep -oE '[0-9]+ passed' /tmp/preship-pytest.log | tail -1)"

echo "[4/4] frontend build (Node 22) + offline-build check"
docker run --rm -v "$PWD/frontend":/app -w /app node:22-slim sh -c \
  "npm install --no-audit --no-fund >/tmp/preship-npm.log 2>&1 && \
   npm run build >/tmp/preship-build.log 2>&1 && \
   node scripts/check-offline-build.mjs" >/tmp/preship-fe.log 2>&1 \
  || { tail -15 /tmp/preship-build.log /tmp/preship-fe.log >&2; fail "frontend build/check failed"; }
echo "  ok — $(tail -1 /tmp/preship-fe.log)"

echo "GATE PASS (full)"
