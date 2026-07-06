#!/bin/bash
# check-core-apps-sync.sh — CI guard: fail if a CATALOG-SYNCED core app drifts
# from its pinned catalog commit. Re-runs the sync into a temp dir and diffs
# each pinned slug against the committed core-apps/ tree. Needs network (clones
# the catalog repos), so it runs as its own CI step, not inside the hermetic
# backend pytest.
#
# Core apps come in two kinds:
#   - catalog-synced: listed in core-apps/SOURCES as `<slug> <repo> <commit>`;
#     a committed snapshot of that repo. Drift from the pinned commit fails here.
#   - native: authored directly in-repo, with no upstream catalog repo (e.g.
#     skills, tasks). There is nothing to diff them against, so they are exempt
#     from this check. Their manifest is still enforced —
#     tests/test_core_apps_manifests.py requires EVERY core app under
#     core-apps/ to ship a valid mobius.json — so a broken/manifest-less dir is
#     still caught, just by pytest rather than here.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Regenerate only the catalog-synced apps (those pinned in SOURCES) into $tmp.
"$ROOT/scripts/sync-core-apps.sh" "$tmp" >/dev/null

fail=0
while read -r slug repo commit _rest; do
  case "$slug" in ''|\#*) continue ;; esac
  if ! diff -rq "$tmp/$slug" "$ROOT/core-apps/$slug" >/dev/null 2>&1; then
    echo "ERROR: core-apps/$slug has drifted from $repo@${commit:0:10}." >&2
    diff -rq "$tmp/$slug" "$ROOT/core-apps/$slug" >&2 || true
    fail=1
  fi
done < "$ROOT/core-apps/SOURCES"

if [ "$fail" -eq 0 ]; then
  echo "catalog-synced core-apps match the pinned catalog ✓"
else
  echo "Run scripts/sync-core-apps.sh and commit the result (or fix core-apps/SOURCES)." >&2
  exit 1
fi
