#!/usr/bin/env bash
#
# mobius-session.sh — bootstrap an isolated worktree for a parallel session.
#
# Solves: when two sessions edit this repo at once, running Compose from the
# main tree can bind-mount a sibling's uncommitted WIP into a container. Linked
# worktrees also have a `.git` pointer file, while the runtime identity gate
# deliberately requires a complete standalone checkout. The supported wrappers
# printed below create safe test environments without weakening that gate.
#
# Usage:
#   scripts/mobius-session.sh <slug>
#
# What it does:
#   1. Verifies we're at the mobius repo root.
#   2. Creates .claude/worktrees/<slug> from origin/main (idempotent).
#   3. Copies .env into the worktree if one exists at the root.
#   4. Links installed Node dependencies into the worktree.
#   5. Prints the worktree-safe unit and browser-test commands.
#
# Does NOT start containers, run tests, or commit anything.

set -euo pipefail

if [[ $# -ne 1 || -z "${1:-}" ]]; then
  echo "usage: $0 <slug>" >&2
  exit 2
fi
slug="$1"

if [[ ! -f docker-compose.test.yml ]]; then
  echo "error: run from the mobius repo root (docker-compose.test.yml not found)" >&2
  exit 2
fi

# Refresh the shared hooks before creating a worktree. One install covers all
# current and future worktrees attached to this clone.
scripts/install-hooks.sh

worktree=".claude/worktrees/${slug}"
branch="session-${slug}"

if [[ -d "$worktree" ]]; then
  echo "worktree already exists: $worktree (skipping create)"
else
  git worktree add "$worktree" -b "$branch" origin/main
fi

if [[ -f .env && ! -f "$worktree/.env" ]]; then
  cp .env "$worktree/.env"
  echo "copied .env into $worktree/"
fi

# Share the main checkout's installed dependencies. These symlinks are
# untracked; remove them before `git worktree remove`.
for dir in . frontend; do
  if [[ -d "$dir/node_modules" && ! -e "$worktree/$dir/node_modules" ]]; then
    ln -s "$(pwd)/$dir/node_modules" "$worktree/$dir/node_modules"
    echo "linked $dir/node_modules into $worktree/$dir/"
  fi
done

cat <<EOF

Worktree ready:  $worktree
Branch:          $branch

Next steps (run from the repo root or from inside the worktree):

  # Backend tests through the worktree-safe container wrapper:
  cd $worktree && scripts/wt-pytest.sh tests

  # Frontend unit tests + production build:
  cd $worktree/frontend && npm test && npm run build

  # Browser tests use a committed, disposable standalone snapshot. The script
  # owns its isolated containers and port; pass one or more focused specs:
  cd $worktree && scripts/playwright-local.sh --allow-local-e2e tests/bootstrap.spec.mjs

  # When the branch is ready, publish/update its required-check PR:
  cd $worktree && scripts/submit-pr.sh

  # If the rebase conflicts, resolve it and rerun submit-pr.sh.

  # Remove the shared dependency symlinks, then tear down the worktree:
  unlink $worktree/node_modules 2>/dev/null || true
  unlink $worktree/frontend/node_modules 2>/dev/null || true
  git worktree remove $worktree
  git branch -D $branch   # only if shipped or intentionally abandoned

EOF
