#!/usr/bin/env bash
# render-miniapp.sh — headless screenshot of a Möbius mini-app's real UI.
#
# Why this exists: headless agent-browser cannot render the shell's
# sandboxed AppCanvas iframe — it never fires `frame-mounted` headless, so a
# mini-app sits on the loading spinner forever (proven with a trivial
# hello-world app). The standalone route (`GET /apps/<slug>/`,
# backend/app/routes/standalone.py) renders the app module DIRECTLY into
# #root via the same self-contained compiled module, with NO sandboxed iframe — so it
# DOES render headless. This wraps that path: mint an owner JWT, inject it
# into localStorage on the app origin (the standalone shell reads
# localStorage['token'] and otherwise redirects to /login), suppress the
# opportunistic PWA install card, navigate, wait for the app to mount, and
# screenshot.
#
# This is the durable answer to "we can't screenshot-verify mini-app UIs."
# Every future mini-app change is now visually verifiable from the host.
#
# SECURITY MODEL — this is a host-side DEV tool for a TEST container, not a
# production utility. It mints a real owner JWT and injects it into the browser
# session's localStorage on the app origin, so the standalone page authenticates.
# Consequences and bounds:
#   * Test-only: the defaults (port 8001 + admin/admin) can't authenticate to
#     prod, and prod isn't host-published on 8000. As a cheap backstop for the
#     obvious mistake it also refuses the prod port (8000) unless
#     RENDER_ALLOW_NONTEST=1 is set. Point it elsewhere only on purpose.
#   * The minted JWT is short-lived but, while valid, lives in the agent-browser
#     session profile's localStorage (and appears on the `agent-browser eval`
#     argv) until it expires or the session is closed. Intended for a
#     single-user dev host against a throwaway test instance. Do NOT point this
#     at an instance whose owner JWT you wouldn't expose to local processes.
#   * `<slug>` is validated to the mobius slug charset and JSON-encoded before
#     it reaches the in-page eval, so it can't break out of the JS string and
#     run arbitrary script in the (JWT-bearing) app origin.
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
# Validate the slug to the mobius slug charset (allocate_unique_slug emits
# lowercase alphanumerics + hyphens). This is the FIRST line of defence against
# a slug that could break out of the in-page JS eval (which runs in the
# JWT-bearing app origin) or distort the request URL / default output path — the
# eval below also JSON-encodes it as a second layer.
if [[ ! "$SLUG" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "error: invalid slug '$SLUG' (expected lowercase letters, digits, hyphens)" >&2
  exit 2
fi

PORT="${MOBIUS_PORT:-8001}"
HOST="${MOBIUS_HOST:-172.17.0.1}"
USER="${MOBIUS_USER:-admin}"
PASS="${MOBIUS_PASS:-admin}"
VIEWPORT="${VIEWPORT:-412 915}"
# Validate VIEWPORT to "WIDTH HEIGHT" (two integers) and split it into separate
# args below, so the deliberate word-split that the viewport needs can't be
# abused to smuggle extra arguments into the agent-browser invocation.
if [[ ! "$VIEWPORT" =~ ^[0-9]+[[:space:]]+[0-9]+$ ]]; then
  echo "error: invalid VIEWPORT '$VIEWPORT' (expected 'WIDTH HEIGHT', e.g. '412 915')" >&2
  exit 2
fi
read -r VW VH <<<"$VIEWPORT"
AB_SESSION="${AB_SESSION:-render-miniapp}"
OUT="${2:-/tmp/render-${SLUG}.png}"
BASE="http://${HOST}:${PORT}"

# Test-only foot-gun guard. This tool mints + injects a real OWNER JWT, so it's
# for a LOCAL test container. The natural safety is the defaults: port 8001 +
# admin/admin can't authenticate to prod (whose owner isn't admin/admin), and
# prod isn't host-published on 8000 anyway. We add ONE cheap explicit guard for
# the obvious mistake — refuse the prod port — and otherwise trust the operator.
# Set RENDER_ALLOW_NONTEST=1 to aim it elsewhere on purpose (you own the target).
if [[ "${RENDER_ALLOW_NONTEST:-}" != "1" && "$PORT" == "8000" ]]; then
  echo "error: refusing port 8000 (the prod port) — this tool is test-only." >&2
  echo "       set RENDER_ALLOW_NONTEST=1 to override." >&2
  exit 2
fi

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

# 1. Mint an owner JWT (form-encoded, not JSON). --data-urlencode percent-
#    encodes each field, so a username/password containing `&` or `=` can't
#    inject extra form fields into the request body.
TOK="$(curl -s --connect-timeout 5 --max-time 20 -X POST "${BASE}/api/auth/token" \
  --data-urlencode "username=${USER}" \
  --data-urlencode "password=${PASS}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
if [[ -z "$TOK" ]]; then
  echo "error: could not authenticate as ${USER} at ${BASE} (is the container up?)" >&2
  exit 4
fi

# 2. Set viewport, land on a lightweight same-origin page, inject the token +
#    the install-card dismiss key (the standalone page reads both from web
#    storage on the app origin). Both values are JSON-encoded into JS string
#    literals — never concatenated raw — so a value containing a quote can't
#    break out of the eval and run script in the JWT-bearing app origin. The
#    secrets are fed to python on STDIN, not argv, so they don't sit in `ps`
#    for the encode step (the token unavoidably reaches the `ab eval` argv,
#    which is the documented exposure window — see SECURITY MODEL above).
json_str() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
TOK_JS="$(printf '%s' "$TOK" | json_str)"
SLUG_JS="$(printf '%s' "$SLUG" | json_str)"
ab set viewport "$VW" "$VH" >/dev/null
ab open "${BASE}/api/health" >/dev/null
ab eval "localStorage.setItem('token', ${TOK_JS}); sessionStorage.setItem('mobius:install-card:dismissed:' + ${SLUG_JS}, '1'); 'ok'" >/dev/null

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
