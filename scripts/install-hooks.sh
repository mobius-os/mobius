#!/usr/bin/env bash
# Install Möbius git hooks for this clone.
#
# Copies scripts/githooks/* plus scripts/pre-commit.sh into the repo's SHARED
# hooks dir (git-common-dir/hooks), so one install covers every linked worktree.
# Idempotent — re-run after pulling updated hooks to refresh them.
#
#   ./scripts/install-hooks.sh
#
# A copy (not a symlink) is used so the installed hook is self-contained
# and survives removal of whatever worktree it was installed from. Privacy
# failures must never be bypassed with `--no-verify`.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir)"
HOOKS_DIR="$COMMON_DIR/hooks"
SRC="$ROOT/scripts/githooks"

if [ ! -d "$SRC" ]; then
  echo "error: $SRC not found (run from inside a checkout that has it)" >&2
  exit 1
fi

mkdir -p "$HOOKS_DIR"
# Pin the active hook path in repository-local config. This prevents a global
# core.hooksPath setting from silently bypassing the hooks we install, without
# affecting any other repository.
git config --local core.hooksPath "$HOOKS_DIR"
install -m 0755 "$ROOT/scripts/pre-commit.sh" "$HOOKS_DIR/pre-commit"
echo "installed pre-commit -> $HOOKS_DIR/pre-commit"
for hook in "$SRC"/*; do
  [ -e "$hook" ] || continue
  name="$(basename "$hook")"
  install -m 0755 "$hook" "$HOOKS_DIR/$name"
  echo "installed $name -> $HOOKS_DIR/$name"
done
echo "Hooks installed (covers all worktrees sharing $HOOKS_DIR)."
