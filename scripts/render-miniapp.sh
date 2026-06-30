#!/usr/bin/env bash
# render-miniapp.sh — headless screenshot of a Möbius mini-app's real UI.
#
# Why this exists: headless agent-browser cannot render the shell's
# sandboxed AppCanvas iframe — it never fires `frame-mounted` headless, so a
# mini-app sits on the loading spinner forever (proven with a trivial
# hello-world app). The standalone route (`GET /apps/<slug>/`,
# backend/app/routes/standalone.py) renders the app module DIRECTLY into
# #root via an importmap + dynamic import, with NO sandboxed iframe — so it
# DOES render headless. This wraps that path: mint an owner JWT, inject it
# into localStorage on the app origin (the standalone shell reads
# localStorage['token'] and otherwise redirects to /login), suppress the
# opportunistic PWA install card, navigate, wait for the app to mount, and
# screenshot.
#
# This is the durable answer to "we can't screenshot-verify mini-app UIs."
# Every future mini-app change is now visually verifiable from the host.
#
# Usage:
#   scripts/render-miniapp.sh <slug> [output.png]
#
# Env (all optional):
#   MOBIUS_PORT   test container host port            (default 8001)
#   MOBIUS_HOST   container host/IP                   (default 172.17.0.1)
#   MOBIUS_USER   owner username                      (default admin)
#   MOBIUS_PASS   owner password                      (default admin)
#   VIEWPORT      mobile viewport "W H"               (default "412 915")
#   AB_SESSION    agent-browser session (isolation)   (default render-miniapp)
#   FULL_PAGE     "1" to capture the full scroll height (default viewport only)
#
# The browser session is left OPEN on success so you can keep driving it with
# the same AB_SESSION (e.g. open a brief, toggle the chat) and screenshot
# again. Run `agent-browser --session <AB_SESSION> close` when done.
set -euo pipefail

SLUG="${1:-}"
if [[ -z "$SLUG" ]]; then
  echo "usage: $0 <slug> [output.png]" >&2
  exit 2
fi

PORT="${MOBIUS_PORT:-8001}"
HOST="${MOBIUS_HOST:-172.17.0.1}"
USER="${MOBIUS_USER:-admin}"
PASS="${MOBIUS_PASS:-admin}"
VIEWPORT="${VIEWPORT:-412 915}"
AB_SESSION="${AB_SESSION:-render-miniapp}"
OUT="${2:-/tmp/render-${SLUG}.png}"
BASE="http://${HOST}:${PORT}"

# Resolve agent-browser: prefer the repo-local install, fall back to PATH.
AB_BIN=""
for cand in \
  "/home/hmzmrzx/projects/node_modules/.bin/agent-browser" \
  "$(command -v agent-browser 2>/dev/null || true)"; do
  if [[ -n "$cand" && -x "$cand" ]]; then AB_BIN="$cand"; break; fi
done
if [[ -z "$AB_BIN" ]]; then
  echo "error: agent-browser not found (npm i -g agent-browser)" >&2
  exit 3
fi
ab() { "$AB_BIN" --session "$AB_SESSION" "$@"; }

# 1. Mint an owner JWT (form-encoded, not JSON).
TOK="$(curl -s -X POST "${BASE}/api/auth/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "username=${USER}&password=${PASS}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
if [[ -z "$TOK" ]]; then
  echo "error: could not authenticate as ${USER} at ${BASE} (is the container up?)" >&2
  exit 4
fi

# 2. Set viewport, land on a lightweight same-origin page, inject the token +
#    the install-card dismiss key (the standalone page reads both from web
#    storage on the app origin).
ab set viewport $VIEWPORT >/dev/null
ab open "${BASE}/api/health" >/dev/null
ab eval "localStorage.setItem('token', $(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$TOK")); sessionStorage.setItem('mobius:install-card:dismissed:${SLUG}', '1'); 'ok'" >/dev/null

# 3. Navigate to the standalone app shell and wait for the module to mount
#    (#loading gets .hidden after root.render completes).
ab open "${BASE}/apps/${SLUG}/" >/dev/null
ab wait --fn "(function(){var l=document.getElementById('loading');return l&&l.classList.contains('hidden');})()" >/dev/null 2>&1 || true

# 4. Verify we actually rendered the app (not a login redirect / empty root).
URL="$(ab get url 2>/dev/null | tr -d '[:space:]')"
KIDS="$(ab eval "(document.getElementById('root')||{children:[]}).children.length" 2>/dev/null | tr -d '[:space:]"' )"
if [[ "$URL" != *"/apps/${SLUG}"* ]]; then
  echo "error: navigation left the standalone page (url=${URL}); auth/token likely rejected" >&2
  exit 5
fi
if [[ "${KIDS:-0}" == "0" ]]; then
  echo "error: app did not mount (#root is empty) for slug=${SLUG}" >&2
  ab screenshot "$OUT" >/dev/null 2>&1 || true
  echo "  (debug screenshot saved to ${OUT})" >&2
  exit 6
fi

# 5. Screenshot.
if [[ "${FULL_PAGE:-}" == "1" ]]; then
  ab screenshot --full "$OUT" >/dev/null
else
  ab screenshot "$OUT" >/dev/null
fi

echo "$OUT"
