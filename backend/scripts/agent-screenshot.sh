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
#   agent-screenshot.sh [--content-only] [--preserve-cache] <route> <out.png>
#   <route> is path-absolute (starts with /); it is appended to
#   $API_BASE_URL.
#
# --content-only removes product-owned walkthrough/install overlays from this
# browser document after auth. It is deliberately ephemeral: no completion or
# dismissal state is written to the owner's account or browser storage.
#
# Normal captures require the exact currently-built shell: the helper detaches
# this test profile's service worker, cache-busts the navigation, and verifies
# the loaded entry asset. --preserve-cache is reserved for explicitly
# testing PWA upgrade/offline behavior where retained browser state is the
# subject of the test.
#
# Prints the output path on stdout, or non-zero if the auth dance
# fails (no token, no API_BASE_URL, no viewport, no agent-browser).

set -euo pipefail

CONTENT_ONLY=0
PRESERVE_CACHE=0
while [ "${1:-}" = "--content-only" ] || [ "${1:-}" = "--preserve-cache" ]; do
  if [ "$1" = "--content-only" ]; then
    CONTENT_ONLY=1
  else
    PRESERVE_CACHE=1
  fi
  shift
done

ROUTE="${1:-}"
OUT="${2:-}"

if [ -z "$ROUTE" ]; then
  echo "agent-screenshot.sh: route required" >&2
  echo "Usage: agent-screenshot.sh [--content-only] [--preserve-cache] <route> [out.png]" >&2
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
# Existing agent sessions keep the env snapshot they started with, and manual
# callers can bypass chat.py entirely. Normalize again at this executable
# boundary so a valid fractional CSS size (for example 956.6667px from a scaled
# desktop pane) never reaches agent-browser's integer-only viewport command.
if ! NORMALIZED_VIEWPORT="$(
  VIEWPORT_WIDTH="$VIEWPORT_WIDTH" VIEWPORT_HEIGHT="$VIEWPORT_HEIGHT" \
  python3 - <<'PY'
import math
import os
import sys

try:
  values = (
    float(os.environ["VIEWPORT_WIDTH"]),
    float(os.environ["VIEWPORT_HEIGHT"]),
  )
except (KeyError, ValueError):
  raise SystemExit(1)
if not all(math.isfinite(value) and value > 0 for value in values):
  raise SystemExit(1)
print(*(max(1, round(value)) for value in values))
PY
)"; then
  echo "agent-screenshot.sh: VIEWPORT_WIDTH and VIEWPORT_HEIGHT must be positive numbers" >&2
  exit 1
fi
read -r VIEWPORT_WIDTH VIEWPORT_HEIGHT <<<"$NORMALIZED_VIEWPORT"

# Origin must be loaded before localStorage.setItem (localStorage is
# per-origin and only writable once a same-origin document exists).
# Both the shell and the standalone /apps/<slug>/ page read the owner
# JWT from localStorage['token'] on the same origin.
agent-browser open "${API_BASE_URL}/" >/dev/null
# `set viewport` requires a live browser connection. Setting it before the
# first `open` happened to work only when a previous turn had leaked/reused a
# daemon; a correctly closed, cold session failed before it could launch.
agent-browser set viewport "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" >/dev/null
# Seed the token via stdin (eval --stdin), never argv: the JWT must not
# appear in /proc/<pid>/cmdline. python reads it from the env (not argv)
# and JSON-encodes it so any character is a safe JS string literal.
AGENT_TOKEN="$AGENT_TOKEN" python3 -c 'import json,os; print("localStorage.setItem(\"token\", "+json.dumps(os.environ["AGENT_TOKEN"])+")")' | agent-browser eval --stdin >/dev/null

# Content mode is a browser-session presentation flag, not onboarding or
# install completion state. Set it while the origin document is live and before
# the target mounts so React never opens its modal. It survives the about:blank
# detour below because sessionStorage is scoped to this tab + origin.
if [ "$CONTENT_ONLY" -eq 1 ]; then
  agent-browser eval \
    "sessionStorage.setItem('mobius:visual-content-only', '1')" >/dev/null
else
  agent-browser eval \
    "sessionStorage.removeItem('mobius:visual-content-only')" >/dev/null
fi

# A per-chat Chromium profile deliberately survives browser close, which is
# useful for realistic PWA tests but unsafe as the default proof of current
# source. An old service worker can otherwise serve a stale precached shell
# after a rebuild and make correct work look absent. Unregister it, then leave
# its currently-controlled document via about:blank before the cache-busted
# target navigation. Deleting every CacheStorage entry synchronously here used
# to wedge CDP on larger profiles; detaching the controller is both sufficient
# and bounded. The owner's real browser/profile is never touched.
TARGET_ROUTE="$ROUTE"
if [ "$PRESERVE_CACHE" -eq 0 ]; then
  agent-browser eval \
    "(async () => { try { const regs = await navigator.serviceWorker?.getRegistrations?.() || []; await Promise.all(regs.map((r) => r.unregister())); } catch {} return true })()" \
    >/dev/null
  agent-browser open "about:blank" >/dev/null
  CAPTURE_NONCE="$(date +%s%N)"
  case "$TARGET_ROUTE" in
    *\?*) TARGET_ROUTE="${TARGET_ROUTE}&__mobius_capture=${CAPTURE_NONCE}" ;;
    *) TARGET_ROUTE="${TARGET_ROUTE}?__mobius_capture=${CAPTURE_NONCE}" ;;
  esac
fi

# Now navigate to the actual target route, authenticated.
agent-browser open "${API_BASE_URL}${TARGET_ROUTE}" >/dev/null

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

# For shell routes, prove the browser loaded the same hashed entry asset that
# exists in the currently-built dist. This turns stale screenshots into a clear
# failure instead of misleading visual evidence. Standalone app PWAs have their
# own entry shape, but still receive controller detachment + cache-busted
# navigation.
case "$ROUTE" in
  /apps/*) : ;;
  *)
    if [ "$PRESERVE_CACHE" -eq 0 ]; then
      DIST_INDEX="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../frontend" && pwd)/dist/index.html"
      if [ ! -f "$DIST_INDEX" ]; then
        echo "agent-screenshot.sh: current frontend build not found at $DIST_INDEX" >&2
        exit 1
      fi
      CURRENT_SHELL_ENTRY="$(
        python3 - "$DIST_INDEX" <<'PY'
import re
import sys
from pathlib import Path

html = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'<script[^>]+src="([^"]*/assets/index-[^"]+\.js)"', html)
if not match:
  raise SystemExit(1)
print(match.group(1).rsplit("/", 1)[-1])
PY
      )" || {
        echo "agent-screenshot.sh: current shell entry asset could not be resolved" >&2
        exit 1
      }
      LOADED_SHELL_ENTRY_RAW="$(
        agent-browser eval \
          "(() => { const src = document.querySelector('script[type=\"module\"][src*=\"/assets/index-\"]')?.src || ''; return src.split('/').pop() })()" \
          2>/dev/null || true
      )"
      LOADED_SHELL_ENTRY="$(
        printf '%s' "$LOADED_SHELL_ENTRY_RAW" | python3 -c \
          'import json,sys; raw=sys.stdin.read().strip(); value=json.loads(raw) if raw else ""; print(value if isinstance(value, str) else "")' \
          2>/dev/null || printf '%s' "$LOADED_SHELL_ENTRY_RAW"
      )"
      if [ "$LOADED_SHELL_ENTRY" != "$CURRENT_SHELL_ENTRY" ]; then
        echo "agent-screenshot.sh: stale shell loaded (expected $CURRENT_SHELL_ENTRY, got ${LOADED_SHELL_ENTRY:-none})" >&2
        exit 1
      fi
    fi
    ;;
esac

# A fresh phone-width shell can restore with the modal navigation drawer open
# or still exiting, which makes an otherwise-correct app screenshot capture the
# scrim/drawer transition. Close only the mobile modal form; the desktop docked
# sidebar is part of the partner's actual layout and stays untouched.
agent-browser eval \
  "(() => { const b = document.querySelector('button[aria-label=\"Toggle navigation\"][aria-expanded=\"true\"]'); if (window.innerWidth < 768 && b) b.click(); return true })()" \
  >/dev/null 2>&1 || true
if [ "$VIEWPORT_WIDTH" -lt 768 ]; then
  if ! agent-browser wait --fn \
    "!document.querySelector('.drawer-overlay--blocking') && !document.querySelector('.drawer:not(.drawer--persistent).drawer--open')" \
    >/dev/null 2>&1; then
    echo "agent-screenshot.sh: mobile navigation did not finish closing before capture" >&2
    exit 1
  fi
fi

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
