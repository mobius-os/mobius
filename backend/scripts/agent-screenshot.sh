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

# agent-browser includes launch/runtime configuration in its daemon identity.
# Never vary AGENT_BROWSER_* env between commands: doing so silently splits one
# logical capture across multiple browsers. Bound individual CLI waits with the
# process-level `timeout` utility instead, which preserves daemon identity and
# lets the caller inspect/close the same session after this helper returns.

browser_eval_retry() {
  local expression="$1"
  local output=""
  local attempt
  for attempt in 1 2 3; do
    if output="$(timeout 5s agent-browser eval "$expression" 2>/dev/null)"; then
      printf '%s' "$output"
      return 0
    fi
    sleep 0.3
  done
  return 1
}

browser_screenshot_retry() {
  local output_path="$1"
  local attempt
  local byte_count
  for attempt in 1 2 3; do
    # Navigation/service-worker handoffs can replace the page after an earlier
    # viewport command. Configure the page that will produce THIS screenshot,
    # not merely the page that existed at helper startup.
    if ! prepare_capture_viewport; then
      continue
    fi
    if timeout 5s agent-browser screenshot "$output_path" >/dev/null 2>&1; then
      byte_count="$(wc -c < "$output_path" 2>/dev/null || printf '0')"
      if { [ "${CAPTURE_MIN_BYTES:-0}" -le 0 ] \
            || [ "${byte_count:-0}" -ge "${CAPTURE_MIN_BYTES}" ]; } \
          && python3 - "$output_path" "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" <<'PY'
import struct
import sys

path, expected_w, expected_h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
  with open(path, "rb") as handle:
    header = handle.read(24)
  valid = header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR"
  width, height = struct.unpack(">II", header[16:24]) if valid else (0, 0)
except (OSError, struct.error):
  width, height = 0, 0
raise SystemExit(0 if (width, height) == (expected_w, expected_h) else 1)
PY
      then
        return 0
      fi
      # A shell screenshot smaller than the minimum is the observed solid-
      # background compositor frame, not useful evidence. Yield two renderer
      # frames before retrying; do not accept a successful CDP command as proof
      # that the shell's paint reached the captured surface.
      browser_eval_retry \
        "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))" \
        >/dev/null || true
    fi
    sleep 0.5
  done
  return 1
}

browser_open_origin_retry() {
  local url="$1"
  local mode="$2"
  local current=""
  local confirmed=""
  local settled=""
  local attempt
  for attempt in 1 2 3 4 5; do
    timeout 5s agent-browser open "$url" >/dev/null 2>&1 || true
    sleep 0.2
    current="$(
      timeout 5s agent-browser get url 2>/dev/null || true
    )"
    sleep 0.2
    confirmed="$(
      timeout 5s agent-browser get url 2>/dev/null || true
    )"
    if [ "$current" != "$confirmed" ]; then
      # In-shell deep links intentionally canonicalize to `/shell/` after the
      # router consumes their intent. That redirect can land between these two
      # reads. Require the canonical URL to remain stable for one more read
      # instead of retrying the deep link forever; the final auth/readiness
      # checks below still reject login or an unmounted target.
      case "$mode:$confirmed" in
        target:"${API_BASE_URL}"*)
          case "$confirmed" in
            "${API_BASE_URL}/api/browser-bootstrap"*) continue ;;
          esac
          sleep 0.2
          settled="$(
            timeout 5s agent-browser get url 2>/dev/null || true
          )"
          [ "$confirmed" = "$settled" ] && return 0
          ;;
      esac
      continue
    fi
    case "$mode:$confirmed" in
      bootstrap:"${API_BASE_URL}/api/browser-bootstrap"*) return 0 ;;
      target:"${API_BASE_URL}"*)
        case "$confirmed" in
          "${API_BASE_URL}/api/browser-bootstrap"*) : ;;
          *) return 0 ;;
        esac
        ;;
    esac
  done
  return 1
}

browser_open_exact_retry() {
  local url="$1"
  local current=""
  local confirmed=""
  local attempt
  for attempt in 1 2 3 4 5; do
    timeout 5s agent-browser open "$url" >/dev/null 2>&1 || true
    sleep 0.2
    current="$(
      timeout 5s agent-browser get url 2>/dev/null || true
    )"
    sleep 0.2
    confirmed="$(
      timeout 5s agent-browser get url 2>/dev/null || true
    )"
    [ "$current" = "$url" ] && [ "$confirmed" = "$url" ] && return 0
  done
  return 1
}

browser_set_viewport_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if timeout 5s agent-browser set viewport "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" \
        >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

clear_stale_browser_profile_lock() {
  local lock_path="${AGENT_BROWSER_PROFILE:-}/SingletonLock"
  local lock_target=""
  local owner_host=""
  local owner_pid=""
  local current_host=""
  local artifact=""

  [ -n "${AGENT_BROWSER_PROFILE:-}" ] || return 0
  [ -L "$lock_path" ] || return 0

  lock_target="$(readlink "$lock_path" 2>/dev/null || true)"
  owner_host="${lock_target%-*}"
  owner_pid="${lock_target##*-}"
  case "$owner_pid" in
    ''|*[!0-9]*)
      # An unfamiliar lock shape may belong to a newer Chromium contract.
      # Preserve it rather than guessing that the automation profile is idle.
      echo "agent-screenshot.sh: browser profile lock has an unfamiliar owner; leaving it untouched" >&2
      return 0
      ;;
  esac
  if [ -z "$owner_host" ] || [ "$owner_host" = "$lock_target" ]; then
    echo "agent-screenshot.sh: browser profile lock has an unfamiliar owner; leaving it untouched" >&2
    return 0
  fi

  current_host="$(hostname)"
  if [ "$owner_host" = "$current_host" ] && kill -0 "$owner_pid" 2>/dev/null; then
    # Never disturb a browser that still owns this profile in this container.
    return 0
  fi

  # Chromium records its singleton owner as <hostname>-<pid>. The per-chat
  # automation profile survives container restarts, while the previous
  # container and its processes do not. Remove only Chromium's three singleton
  # symlinks; authenticated browser state, cache, and the partner's real browser
  # profile remain untouched. A same-host dead PID is the equivalent crash case.
  for artifact in SingletonLock SingletonCookie SingletonSocket; do
    if [ -L "${AGENT_BROWSER_PROFILE}/${artifact}" ]; then
      rm -f "${AGENT_BROWSER_PROFILE}/${artifact}"
    fi
  done
}

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

clear_stale_browser_profile_lock

# Start the browser, then wait narrowly for its command socket before applying
# viewport state. A cold launch can return before that socket is connectable.
timeout 5s agent-browser open "${API_BASE_URL}/api/browser-bootstrap" \
  >/dev/null 2>&1 || true
if ! browser_set_viewport_retry; then
  echo "agent-screenshot.sh: browser did not become ready for viewport configuration" >&2
  exit 1
fi

# A retained test profile can start under an older service worker that handles
# `/api/browser-bootstrap` as an app navigation and immediately canonicalizes
# it to `/shell/`. Requiring the inert bootstrap before detaching that
# controller makes authentication fail even though the authenticated shell is
# already on screen. Unregister from whichever same-origin document the cold
# open produced, then leave that controlled document before requiring the
# bootstrap URL below. The later detach still protects the final target
# navigation from a controller installed between authentication and capture.
if [ "$PRESERVE_CACHE" -eq 0 ]; then
  browser_eval_retry \
    "(async () => { try { const regs = await navigator.serviceWorker?.getRegistrations?.() || []; await Promise.all(regs.map((r) => r.unregister())); } catch {} return true })()" \
    >/dev/null || true
  if ! browser_open_exact_retry "about:blank"; then
    echo "agent-screenshot.sh: browser did not detach its stale bootstrap page" >&2
    exit 1
  fi
fi

# Seed the token, ephemeral visual mode, and default service-worker reset in
# one same-origin evaluation. The dedicated bootstrap is inert HTML, so it
# cannot restore the last chat or disappear like Chromium's JSON viewer.
# The JWT travels via stdin, never argv or /proc/<pid>/cmdline.
TOKEN_READY=0
for attempt in 1 2 3; do
  if ! browser_open_origin_retry \
    "${API_BASE_URL}/api/browser-bootstrap" bootstrap; then
    continue
  fi
  if AGENT_TOKEN="$AGENT_TOKEN" CONTENT_ONLY="$CONTENT_ONLY" PRESERVE_CACHE="$PRESERVE_CACHE" \
    python3 -c '
import json, os
token = json.dumps(os.environ["AGENT_TOKEN"])
visual = (
  "sessionStorage.setItem(\"mobius:visual-content-only\", \"1\");"
  if os.environ["CONTENT_ONLY"] == "1"
  else "sessionStorage.removeItem(\"mobius:visual-content-only\");"
)
reset = (
  "try { const regs = await navigator.serviceWorker?.getRegistrations?.() || []; "
  "await Promise.all(regs.map((r) => r.unregister())); } catch {}"
  if os.environ["PRESERVE_CACHE"] == "0"
  else ""
)
print(
  "(async () => { localStorage.setItem(\"token\", " + token + "); "
  + visual + reset + " return true })()"
)
' \
      | timeout 5s agent-browser eval --stdin >/dev/null 2>&1; then
    TOKEN_READY=1
    break
  fi
done
if [ "$TOKEN_READY" -ne 1 ]; then
  echo "agent-screenshot.sh: browser origin did not remain ready for authentication" >&2
  exit 1
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
  if ! browser_open_exact_retry "about:blank"; then
    echo "agent-screenshot.sh: browser did not detach before target navigation" >&2
    exit 1
  fi
  CAPTURE_NONCE="$(date +%s%N)"
  case "$TARGET_ROUTE" in
    *\?*) TARGET_ROUTE="${TARGET_ROUTE}&__mobius_capture=${CAPTURE_NONCE}" ;;
    *) TARGET_ROUTE="${TARGET_ROUTE}?__mobius_capture=${CAPTURE_NONCE}" ;;
  esac
fi

# Now navigate to the actual target route, authenticated.
if ! browser_open_origin_retry "${API_BASE_URL}${TARGET_ROUTE}" target; then
  echo "agent-screenshot.sh: browser did not reach the target route" >&2
  exit 1
fi

# The URL can canonicalize before the replacement document has committed.
# Applying device metrics during that gap reports success on the outgoing page,
# then the newly-created shell page falls back to Chromium's default viewport.
# Wait on browser paint ownership before configuring the final page.
if ! agent-browser wait --fn \
  "document.readyState === 'complete' && performance.getEntriesByName('first-contentful-paint').length > 0" \
  >/dev/null 2>&1; then
  echo "agent-screenshot.sh: target document did not finish its initial paint" >&2
  exit 1
fi

# Let the navigation commit without asking the renderer to poll the transcript.
# A long, actively streaming chat can keep agent-browser's DOM wait inside one
# Runtime.evaluate call until its global timeout even though the browser is
# otherwise responsive. The authoritative checks below retry narrowly instead.
sleep 0.3

# Dismiss the PWA install banner if it surfaces — it covers the bottom
# of the view and would distract from the actual page.
timeout 2s agent-browser find text "Not now" click >/dev/null 2>&1 || true
sleep 0.3

# Token presence alone is not proof of authentication: App mounts Shell from
# localStorage immediately, then a later protected request can reject the token,
# clear it, and reload onto LoginForm. Verify the token with a protected request
# at the FINAL capture boundary, after the settle/banner work above. The token is
# read inside the page and never appears in argv or output.
AUTH_OK="$(browser_eval_retry \
  "(async () => { const token = localStorage.getItem('token'); if (!token || document.querySelector('input[type=password]')) return false; try { const res = await fetch('/api/chats?agent-screenshot-auth=' + Date.now(), { cache: 'no-store', headers: { Authorization: 'Bearer ' + token } }); return res.status === 200 && !!localStorage.getItem('token') && !document.querySelector('input[type=password]'); } catch { return false; } })()" \
  || true)"
if [ "$AUTH_OK" != "true" ]; then
  echo "agent-screenshot.sh: authentication failed; the token was rejected or the login page remained visible" >&2
  exit 1
fi

# For shell routes, prove the browser loaded the same hashed entry asset that
# exists in the currently-built dist. This turns stale screenshots into a clear
# failure instead of misleading visual evidence. Standalone app PWAs have their
# own entry shape, but still receive controller detachment + cache-busted
# navigation.
SHELL_SETTLED_EXPR=""
case "$ROUTE" in
  /apps/*) : ;;
  *)
    # An authenticated shell frame always contains chrome/text and is far
    # larger than the ~3 KiB one-colour PNG Chromium emits before its first
    # useful compositor submission. Standalone app PWAs may intentionally be a
    # solid canvas, so this evidence check is shell-only.
    CAPTURE_MIN_BYTES=8192
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
        browser_eval_retry \
          "(() => { const src = document.querySelector('script[type=\"module\"][src*=\"/assets/index-\"]')?.src || ''; return src.split('/').pop() })()" \
          || true
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

    # Shell mode changes and chat-to-chat handoffs deliberately retain multiple
    # fully laid-out surfaces. Shell owns which world is actually painted and
    # publishes one stable visual-readiness contract; automation must not learn
    # its private handoff classes or compositor attributes. Once the owner says
    # settled, give style/layout two frames to commit.
    SHELL_SETTLED_EXPR="document.querySelector('.shell[data-workspace-visual-state=\"settled\"]') !== null && performance.getEntriesByName('first-contentful-paint').length > 0"
    if ! agent-browser wait --fn "$SHELL_SETTLED_EXPR" >/dev/null 2>&1; then
      echo "agent-screenshot.sh: shell did not reach a settled visual state before capture" >&2
      exit 1
    fi
    if ! browser_eval_retry \
      "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))" \
      >/dev/null; then
      echo "agent-screenshot.sh: shell did not commit its settled frame before capture" >&2
      exit 1
    fi
    ;;
esac

# Device metrics attach to one CDP page. A canonical redirect, a service-worker
# handoff, or agent-browser's about:blank detach can replace that page after the
# startup viewport command. Reapply immediately before each capture attempt;
# browser_screenshot_retry validates the kept PNG's exact IHDR dimensions.
prepare_capture_viewport() {
  # Do not insert a separate renderer round-trip here. A document replacement
  # can land between configuration and that probe; the kept PNG's IHDR below is
  # the exact, race-free proof of which viewport actually produced evidence.
  browser_set_viewport_retry
}

if ! prepare_capture_viewport; then
  echo "agent-screenshot.sh: target page did not retain the requested viewport" >&2
  exit 1
fi

# A fresh phone-width shell can restore with the modal navigation drawer open
# or still exiting, which makes an otherwise-correct app screenshot capture the
# scrim/drawer transition. Close only the mobile modal form; the desktop docked
# sidebar is part of the partner's actual layout and stays untouched.
browser_eval_retry \
  "(() => { const b = document.querySelector('button[aria-label=\"Toggle navigation\"][aria-expanded=\"true\"]'); if (window.innerWidth < 768 && b) b.click(); return true })()" \
  >/dev/null || true
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

# Chromium can expose a complete DOM and a first-contentful-paint timing entry
# one compositor submission before CDP's first screenshot contains that paint.
# The symptom is a successful, solid-background PNG; the immediately following
# capture is correct. Prime the screenshot path into a disposable file, then
# wait two frames before keeping evidence. This is a renderer handshake, not a
# guessed sleep, and the temporary image never enters chat media.
WARMUP_OUT="$(mktemp "${TMPDIR:-/tmp}/mobius-screenshot-warmup.XXXXXX.png")"
trap 'rm -f "$WARMUP_OUT"' EXIT
if ! browser_screenshot_retry "$WARMUP_OUT"; then
  echo "agent-screenshot.sh: page remained too busy to prime capture" >&2
  exit 1
fi
if ! browser_eval_retry \
  "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))" \
  >/dev/null; then
  echo "agent-screenshot.sh: page did not commit after capture priming" >&2
  exit 1
fi

if ! browser_screenshot_retry "$OUT"; then
  echo "agent-screenshot.sh: page remained too busy to capture after bounded retries" >&2
  exit 1
fi
rm -f "$WARMUP_OUT"
trap - EXIT
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
