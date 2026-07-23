#!/usr/bin/env bash
# agent-screenshot.sh — screenshot ANY authenticated Möbius route.
#
# The in-product agent drives a FRESH agent-browser with an empty
# localStorage, so a bare `agent-browser open <route>` lands on the
# login wall — every screenshot is the password form, not the page
# the agent meant to capture. This helper does the auth dance once:
# load the origin, write the agent's scoped token into localStorage,
# THEN navigate to the target route inside the authenticated shell.
#
# It is the generalization of the older preview_shell.sh /
# preview_app.sh helpers (now thin wrappers around this script). Any
# in-shell route works:
#   /                      the shell at the current/last chat
#   /chat/<id>             a specific chat
#   /app/<id>              a mini-app inside the shell (numeric app id)
#   /apps/<slug>/          a mini-app's STANDALONE PWA page (by slug)
#   /settings              owner settings, etc.
#
# Usage:
#   agent-screenshot.sh <route> <out.png>
#   <route> is path-absolute (starts with /); it is appended to
#   $API_BASE_URL.
#
# Prints the output path on stdout, or non-zero if the auth dance
# fails (no token, no API_BASE_URL, no viewport, no agent-browser).

set -euo pipefail

ROUTE="${1:-}"
OUT="${2:-}"

if [ -z "$ROUTE" ]; then
  echo "agent-screenshot.sh: route required" >&2
  echo "Usage: agent-screenshot.sh <route> [out.png]" >&2
  exit 1
fi

# Default the output INTO the chat's served media dir, so the shot can be
# embedded — ![](/api/chats/$CHAT_ID/media/<name>) — with no copy step. A shot
# written elsewhere (e.g. /tmp) is viewable by the agent but 404s if embedded.
if [ -z "$OUT" ]; then
  if [ -z "${CHAT_ID:-}" ]; then
    echo "agent-screenshot.sh: no out.png given and CHAT_ID unset" >&2
    exit 1
  fi
  OUT="/data/chats/${CHAT_ID}/media/shot-$(date +%s%N).png"
fi

# Route must be path-absolute so it appends cleanly to the origin.
case "$ROUTE" in
  /*) : ;;
  *) ROUTE="/$ROUTE" ;;
esac

mkdir -p "$(dirname "$OUT")"

if [ -z "${AGENT_TOKEN:-}" ] || [ -z "${API_BASE_URL:-}" ]; then
  echo "agent-screenshot.sh: AGENT_TOKEN and API_BASE_URL must be set" >&2
  exit 1
fi

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "agent-screenshot.sh: agent-browser not on PATH" >&2
  exit 1
fi

# Prefer the runner-provided per-chat session/profile. Fall back from CHAT_ID
# inside the helper so authenticated screenshots stay isolated even if a
# provider forgets to export AGENT_BROWSER_SESSION. Raw agent-browser calls
# still depend on the provider env; this only protects the screenshot path.
if [ -n "${CHAT_ID:-}" ]; then
  if [ -z "${AGENT_BROWSER_SESSION:-}" ]; then
    export AGENT_BROWSER_SESSION="chat-${CHAT_ID}"
  fi
  if [ -z "${AGENT_BROWSER_PROFILE:-}" ]; then
    CHAT_ID_SAFE="$(printf '%s' "$CHAT_ID" | tr -c 'A-Za-z0-9_-' '_')"
    export AGENT_BROWSER_PROFILE="/data/agent-browser-profiles/chat-${CHAT_ID_SAFE}"
  fi
fi

# Match the partner's actual viewport so the screenshot frames what
# they see. chat.py exports VIEWPORT_WIDTH/HEIGHT from the React
# shell's per-turn payload; screenshots require those values.
if [ -z "${VIEWPORT_WIDTH:-}" ] || [ -z "${VIEWPORT_HEIGHT:-}" ]; then
  echo "agent-screenshot.sh: VIEWPORT_WIDTH and VIEWPORT_HEIGHT must be set" >&2
  exit 1
fi
agent-browser set viewport "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" >/dev/null

# Origin must be loaded before localStorage.setItem (localStorage is
# per-origin and only writable once a same-origin document exists).
# Both the shell and the standalone /apps/<slug>/ page read the owner
# JWT from localStorage['token'] on the same origin.
agent-browser open "${API_BASE_URL}/" >/dev/null
# Seed the token via stdin (eval --stdin), never argv: the JWT must not
# appear in /proc/<pid>/cmdline. python reads it from the env (not argv)
# and JSON-encodes it so any character is a safe JS string literal.
AGENT_TOKEN="$AGENT_TOKEN" python3 -c 'import json,os; print("localStorage.setItem(\"token\", "+json.dumps(os.environ["AGENT_TOKEN"])+")")' | agent-browser eval --stdin >/dev/null

# Now navigate to the actual target route, authenticated.
agent-browser open "${API_BASE_URL}${ROUTE}" >/dev/null

# Give the target a bounded render window. The missing password field is only a
# settling signal — token presence mounts Shell before the server has accepted
# it — so the protected request below remains the authoritative auth check.
agent-browser wait --fn \
  "!document.querySelector('input[type=password]')" >/dev/null 2>&1 || \
  agent-browser wait 1500 >/dev/null

# Dismiss the PWA install banner if it surfaces — it covers the bottom
# of the view and would distract from the actual page.
agent-browser find text "Not now" click >/dev/null 2>&1 || true
agent-browser wait 300 >/dev/null

# Token presence alone is not proof of authentication: App mounts Shell from
# localStorage immediately, then a later protected request can reject the token,
# clear it, and reload onto LoginForm. Verify the token with a protected request
# at the FINAL capture boundary, after the settle/banner work above. The token is
# read inside the page and never appears in argv or output.
AUTH_OK="$(agent-browser eval \
  "(async () => { const token = localStorage.getItem('token'); if (!token || document.querySelector('input[type=password]')) return false; try { const res = await fetch('/api/chats?agent-screenshot-auth=' + Date.now(), { cache: 'no-store', headers: { Authorization: 'Bearer ' + token } }); return res.status === 200 && !!localStorage.getItem('token') && !document.querySelector('input[type=password]'); } catch { return false; } })()" \
  2>/dev/null || true)"
if [ "$AUTH_OK" != "true" ]; then
  echo "agent-screenshot.sh: authentication failed; the token was rejected or the login page remained visible" >&2
  exit 1
fi

# A fresh phone-width shell can restore with the modal navigation drawer open
# or still exiting, which makes an otherwise-correct app screenshot capture the
# scrim/drawer transition. Close only the mobile modal form; the desktop docked
# sidebar is part of the partner's actual layout and stays untouched.
agent-browser eval \
  "(() => { const b = document.querySelector('button[aria-label=\"Toggle navigation\"][aria-expanded=\"true\"]'); if (window.innerWidth < 768 && b) b.click(); return true })()" \
  >/dev/null 2>&1 || true

# `/app/<id>` has an exact readiness signal: AppCanvas removes its
# `.canvas-loading` overlay only after the opaque iframe posts
# `moebius:frame-mounted`, which itself fires after the app's first React commit.
# Waiting for that state avoids successful-looking screenshots of the branded
# loading skeleton. Keep the predicate as a simple boolean expression —
# agent-browser's wait parser has timed out on equivalent IIFE forms.
case "$ROUTE" in
  /app/[0-9]*)
    APP_ID="${ROUTE#/app/}"
    APP_ID="${APP_ID%%[/?#]*}"
    READY_EXPR="document.querySelector('iframe[data-app-id=\"${APP_ID}\"]') !== null && document.querySelector('iframe[data-app-id=\"${APP_ID}\"]')?.parentElement.querySelector('.canvas-loading') === null"
    if ! agent-browser wait --fn "$READY_EXPR" >/dev/null 2>&1; then
      echo "agent-screenshot.sh: app ${APP_ID} did not reach its mounted frame before capture" >&2
      exit 1
    fi
    ;;
esac

agent-browser screenshot "${OUT}" >/dev/null
echo "${OUT}"

# Also print the ready-to-paste chat embed. The partner sees ONLY embedded
# images in chat — never a file path or your prose description of a shot. Paste
# this line into your reply (same message, BEFORE describing the screenshot) so
# it actually shows. Only files under /data/chats/<id>/media/ are servable, so
# only emit an embed for those.
case "$OUT" in
  /data/chats/*/media/*)
    echo "PASTE into your reply (the partner cannot see the PNG otherwise): ![screenshot](/api/chats/${OUT#/data/chats/})" ;;
esac
