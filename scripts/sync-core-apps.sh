#!/bin/bash
# sync-core-apps.sh — regenerate core-apps/<slug>/ from its catalog repo.
#
# The catalog repos (mobius-os/app-<slug>) are the single source of truth for
# the platform's built-in apps; core-apps/<slug>/ is a committed snapshot of
# one, pinned by commit in core-apps/SOURCES. To update a built-in app: bump
# its commit in core-apps/SOURCES, run this, and commit the resulting diff.
#
# Usage: sync-core-apps.sh [DEST_ROOT]
#   DEST_ROOT defaults to the repo's core-apps/. check-core-apps-sync.sh passes
#   a temp dir so it can diff a fresh sync against the committed tree.
#
# Repo-meta files (.git, README, LICENSE, .gitignore) are dropped — core-apps
# carries only the installable app (mobius.json, index.jsx, icon, scripts,
# tests, …). Everything else the catalog repo ships is copied verbatim.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCES="$ROOT/core-apps/SOURCES"
DEST_ROOT="${1:-$ROOT/core-apps}"

[ -f "$SOURCES" ] || { echo "sync-core-apps: missing $SOURCES" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

while read -r slug repo commit _rest; do
  case "$slug" in ''|\#*) continue ;; esac
  [ -n "$repo" ] && [ -n "$commit" ] || { echo "sync-core-apps: bad line: $slug $repo $commit" >&2; exit 1; }
  echo "sync $slug <- $repo@${commit:0:10}"
  git clone --quiet "https://github.com/$repo.git" "$tmp/$slug"
  git -C "$tmp/$slug" checkout --quiet "$commit"
  dest="$DEST_ROOT/$slug"
  rm -rf "$dest"
  mkdir -p "$dest"
  ( cd "$tmp/$slug" \
      && tar --exclude=./.git --exclude=./README.md --exclude=./LICENSE --exclude=./.gitignore -cf - . ) \
    | ( cd "$dest" && tar -xf - )
done < "$SOURCES"

echo "core-apps synced into $DEST_ROOT"
