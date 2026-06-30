#!/bin/bash
# check-core-apps-sync.sh — CI guard: fail if core-apps/ drifts from the pinned
# catalog. Re-runs the sync into a temp dir and diffs it against the committed
# core-apps/ tree. Needs network (clones the catalog repos), so it runs as its
# own CI step, not inside the hermetic backend pytest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

"$ROOT/scripts/sync-core-apps.sh" "$tmp" >/dev/null

# Compare the freshly-synced tree against the committed one (ignore the SOURCES
# manifest, which lives only in the committed tree).
if diff -rq --exclude=SOURCES "$tmp" "$ROOT/core-apps" >/dev/null; then
  echo "core-apps matches the pinned catalog ✓"
else
  echo "ERROR: core-apps/ has drifted from the pinned catalog commits." >&2
  echo "Run scripts/sync-core-apps.sh and commit the result (or fix core-apps/SOURCES)." >&2
  diff -rq --exclude=SOURCES "$tmp" "$ROOT/core-apps" >&2 || true
  exit 1
fi
