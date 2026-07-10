#!/bin/bash
# check-core-apps-sync.sh — compatibility CI guard for platform-owned app
# snapshots. SOURCES is intentionally empty today; if a future platform snapshot
# is added, this re-runs the sync into a temp dir and diffs each pinned slug.
#
# Catalog apps install through the App Store into /data/apps, not from this tree.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Regenerate only the catalog-synced apps (those pinned in SOURCES) into $tmp.
"$ROOT/scripts/sync-core-apps.sh" "$tmp" >/dev/null

# Files a slug owns LOCALLY (core-apps/SYNC_LOCAL) are mobius infra living in the
# app tree — excluded from the drift diff so editing them never fails the check.
sync_local_excludes() {  # $1=slug -> prints one `--exclude=<file>` per line
  local sl="$ROOT/core-apps/SYNC_LOCAL"
  [ -f "$sl" ] || return 0
  while read -r ls_slug ls_file _; do
    case "$ls_slug" in ''|\#*) continue ;; esac
    [ "$ls_slug" = "$1" ] && printf -- '--exclude=%s\n' "$ls_file"
  done < "$sl"
}

fail=0
while read -r slug repo commit _rest; do
  case "$slug" in ''|\#*) continue ;; esac
  mapfile -t excludes < <(printf '%s\n' '--exclude=.build*'; sync_local_excludes "$slug")
  if ! diff -rq "${excludes[@]}" "$tmp/$slug" "$ROOT/core-apps/$slug" >/dev/null 2>&1; then
    echo "ERROR: core-apps/$slug has drifted from $repo@${commit:0:10}." >&2
    diff -rq "${excludes[@]}" "$tmp/$slug" "$ROOT/core-apps/$slug" >&2 || true
    fail=1
  fi
done < "$ROOT/core-apps/SOURCES"

if [ "$fail" -eq 0 ]; then
  echo "platform app snapshots are in sync"
else
  echo "Run scripts/sync-core-apps.sh and commit the result (or fix core-apps/SOURCES)." >&2
  exit 1
fi
