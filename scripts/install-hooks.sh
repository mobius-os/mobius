#!/usr/bin/env bash
# Install Möbius git hooks for this clone.
#
# Copies scripts/githooks/* into the repo's SHARED hooks dir
# (git-common-dir/hooks), so one install covers every linked worktree.
# Idempotent — re-run after pulling updated hooks to refresh them.
#
#   ./scripts/install-hooks.sh
#
# A copy (not a symlink) is used so the installed hook is self-contained
# and survives removal of whatever worktree it was installed from. Bypass
# a hook once with `git <cmd> --no-verify`.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$(git rev-parse --git-common-dir)/hooks"
SRC="$ROOT/scripts/githooks"

if [ ! -d "$SRC" ]; then
  echo "error: $SRC not found (run from inside a checkout that has it)" >&2
  exit 1
fi

mkdir -p "$HOOKS_DIR"
for hook in "$SRC"/*; do
  [ -e "$hook" ] || continue
  name="$(basename "$hook")"
  install -m 0755 "$hook" "$HOOKS_DIR/$name"
  echo "installed $name -> $HOOKS_DIR/$name"
done
echo "Hooks installed (covers all worktrees sharing $HOOKS_DIR)."
