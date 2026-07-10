#!/usr/bin/env bash
#
# mobius-session.sh — bootstrap an isolated worktree + test-container project
# for a parallel Claude Code session.
#
# Solves: when two sessions edit this repo at once, the one that runs
# `docker compose ... up -d` from the main tree (or with default paths)
# bind-mounts the sibling's uncommitted WIP into the container — silent
# cross-contamination that surfaces as unrelated test failures. This
# script makes worktree-relative bind-mounts and per-slug ports the
# path of least resistance, so the cross-contamination class of bug
# can't recur.
#
# Usage:
#   scripts/mobius-session.sh <slug>
#
# What it does:
#   1. Verifies we're at the mobius repo root.
#   2. Creates .claude/worktrees/<slug> from origin/main (idempotent).
#   3. Copies .env into the worktree if one exists at the root.
#   4. Computes a deterministic per-slug port in 8001-8090.
#   5. Prints the compose / pytest / gh-pr commands you need next.
#
# Does NOT start containers, run tests, or commit anything — those
# steps are intentionally left to you.

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

# Node deps: share the main checkout's installed node_modules via symlinks
# (the same trick wt-pytest.sh uses for the backend venv), so `npm test`,
# esbuild smoke-checks, and playwright work in the worktree immediately —
# no per-worktree npm ci. The symlinks are untracked; remove them before
# `git worktree remove`.
for dir in . frontend; do
  if [[ -d "$dir/node_modules" && ! -e "$worktree/$dir/node_modules" ]]; then
    ln -s "$(pwd)/$dir/node_modules" "$worktree/$dir/node_modules"
    echo "linked $dir/node_modules into $worktree/$dir/"
  fi
done

port=$((8001 + $(echo "$slug" | cksum | cut -d' ' -f1) % 90))
project="mobius-test-${slug}"

# Export compose vars so a custom run is fully isolated.
# container_name and image are GLOBAL (not project-scoped); -p alone is not
# enough.  These exports pair with -p "$project" to avoid colliding with or
# nuking the default mobius-test container or its image.
export MOBIUS_CONTAINER="mobius-test-${slug}"
export MOBIUS_IMAGE="mobius-test-${slug}:ci"

cat <<EOF

Worktree ready:  $worktree
Branch:          $branch
Test port:       $port
Compose proj:    $project
Container name:  $MOBIUS_CONTAINER
Image tag:       $MOBIUS_IMAGE

MOBIUS_CONTAINER and MOBIUS_IMAGE are already exported; include them on every
docker compose call so container_name + image (both global, not project-scoped)
don't collide with the default mobius-test container.

Next steps (run from the repo root or from inside the worktree):

  # Build + start your test container with worktree-relative bind-mounts:
  MOBIUS_CONTAINER=$MOBIUS_CONTAINER MOBIUS_IMAGE=$MOBIUS_IMAGE \\
    TEST_PORT=$port docker compose -p $project -f docker-compose.test.yml \\
    --project-directory $worktree build
  MOBIUS_CONTAINER=$MOBIUS_CONTAINER MOBIUS_IMAGE=$MOBIUS_IMAGE \\
    TEST_PORT=$port docker compose -p $project -f docker-compose.test.yml \\
    --project-directory $worktree up -d

  # Run pytest from the worktree (so compose mounts YOUR files):
  cd $worktree && MOBIUS_IMAGE=$MOBIUS_IMAGE \\
    docker compose -p $project -f docker-compose.test.yml run --rm pytest

  # When the branch is ready to ship (direct-to-main, no PR needed):
  cd $worktree && scripts/land.sh

  # If a sibling lands first, land.sh prints the manual recovery loop:
  # git fetch origin && git rebase origin/main && scripts/land.sh

  # Tear down when done:
  docker compose -p $project -f docker-compose.test.yml down -v
  git worktree remove $worktree
  git branch -D $branch   # only if PR is merged or you're abandoning the work

EOF
