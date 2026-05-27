#!/usr/bin/env bash
# bundle-info.sh — "What is mobius-test serving and is it stale?"
#
# One-screen status: served bundle hash, theme --bg color, and a host-vs-
# container drift check on a sentinel file the developer is likely
# editing (frontend/src/components/ChatView/ChatInputBar.jsx).
#
# Refuses to talk to prod.
#
# Usage:
#   scripts/bundle-info.sh           # terse text
#   scripts/bundle-info.sh --json    # machine-readable
#
# Override target:
#   CONTAINER=mobius-test PORT=8001 scripts/bundle-info.sh

set -euo pipefail

CONTAINER="${CONTAINER:-mobius-test}"
PORT="${PORT:-8001}"
BASE="http://localhost:${PORT}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SENTINEL_REL="frontend/src/components/ChatView/ChatInputBar.jsx"
SENTINEL_IN_CONTAINER="/data/shell/src/components/ChatView/ChatInputBar.jsx"

if [ "$CONTAINER" = "mobius" ] || [ "$PORT" = "8000" ]; then
  echo "FATAL: bundle-info.sh refuses to target prod (mobius:8000)." >&2
  exit 2
fi

JSON=0
case "${1:-}" in
  --json) JSON=1 ;;
  -h|--help)
    sed -n '1,/^set -euo pipefail/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  '') ;;
  *)
    echo "unknown flag: ${1}" >&2
    exit 2
    ;;
esac

# 1. health gate.
health=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/health" || echo "000")
if [ "$health" != "200" ]; then
  if [ "$JSON" = "1" ]; then
    printf '{"container":"%s","port":%s,"health":"%s","ok":false}\n' \
      "$CONTAINER" "$PORT" "$health"
  else
    echo "container: ${CONTAINER} (:${PORT})"
    echo "health:    ${health}  ← not 200, container down or unhealthy"
  fi
  exit 1
fi

# 2. served bundle + theme --bg.
index_html=$(curl -fsS "${BASE}/" 2>/dev/null || true)
bundle=$(printf '%s' "$index_html" | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -n1 || true)
# The server injects --bg into the inline <style> block of index.html.
# Grab the first `--bg: <value>;` occurrence.
bg=$(printf '%s' "$index_html" | grep -oE -- '--bg:[[:space:]]*[^;]+;' | head -n1 \
  | sed -E 's/^--bg:[[:space:]]*//; s/;[[:space:]]*$//' || true)

# 3. sentinel drift: host vs container sha.
host_sentinel="${REPO_ROOT}/${SENTINEL_REL}"
host_sha=""
if [ -f "$host_sentinel" ]; then
  host_sha=$(sha256sum "$host_sentinel" | awk '{print $1}')
fi
container_sha=$(docker exec "$CONTAINER" sh -c \
  "[ -f '${SENTINEL_IN_CONTAINER}' ] && sha256sum '${SENTINEL_IN_CONTAINER}' | awk '{print \$1}' || true" \
  2>/dev/null || true)

# Drift flag: in-sync / drift / unknown.
if [ -z "$host_sha" ] || [ -z "$container_sha" ]; then
  drift="unknown"
elif [ "$host_sha" = "$container_sha" ]; then
  drift="in-sync"
else
  drift="DRIFT"
fi

if [ "$JSON" = "1" ]; then
  printf '{"container":"%s","port":%s,"health":"200","bundle":"%s","bg":"%s","sentinel":"%s","host_sha":"%s","container_sha":"%s","drift":"%s"}\n' \
    "$CONTAINER" "$PORT" "${bundle:-}" "${bg:-}" "$SENTINEL_REL" \
    "${host_sha:-}" "${container_sha:-}" "$drift"
  exit 0
fi

printf 'container: %s (:%s)\n' "$CONTAINER" "$PORT"
printf 'health:    200\n'
printf 'bundle:    %s\n' "${bundle:-<none>}"
printf 'theme --bg: %s\n' "${bg:-<none>}"
printf 'sentinel:  %s\n' "$SENTINEL_REL"
printf '  host:      %s\n' "${host_sha:0:12}${host_sha:+...}"
printf '  container: %s\n' "${container_sha:0:12}${container_sha:+...}"
printf '  status:    %s\n' "$drift"
if [ "$drift" = "DRIFT" ]; then
  printf '\nRun: scripts/sync-test-shell.sh\n'
fi
