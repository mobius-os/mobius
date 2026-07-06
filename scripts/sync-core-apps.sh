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
  # Preserve mobius-owned local files (core-apps/SYNC_LOCAL) across the
  # re-materialize — they are NOT sourced from the catalog pin, so a pin bump
  # must not clobber them. Stash before the rm, restore after the untar (the
  # untar first writes the pin's copy; the restore overwrites it with mobius's).
  stash="$tmp/_local_$slug"
  if [ -f "$ROOT/core-apps/SYNC_LOCAL" ]; then
    while read -r ls_slug ls_file _; do
      case "$ls_slug" in ''|\#*) continue ;; esac
      [ "$ls_slug" = "$slug" ] && [ -f "$dest/$ls_file" ] || continue
      mkdir -p "$stash/$(dirname "$ls_file")"
      cp -p "$dest/$ls_file" "$stash/$ls_file"
    done < "$ROOT/core-apps/SYNC_LOCAL"
  fi
  rm -rf "$dest"
  mkdir -p "$dest"
  ( cd "$tmp/$slug" \
      && tar --exclude=./.git --exclude=./README.md --exclude=./LICENSE --exclude=./.gitignore -cf - . ) \
    | ( cd "$dest" && tar -xf - )
  if [ -d "$stash" ]; then
    ( cd "$stash" && tar -cf - . ) | ( cd "$dest" && tar -xf - )
  fi
done < "$SOURCES"

echo "core-apps synced into $DEST_ROOT"
