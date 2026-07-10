#!/usr/bin/env bash
# Land the current session branch onto origin/main without clobbering siblings.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

err() { printf 'land: %s\n' "$*" >&2; }
info() { printf 'land: %s\n' "$*"; }

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ -z "$branch" ]; then
  err "detached HEAD; check out a session branch before landing"
  exit 2
fi

dirty="$(git status --porcelain)"
if [ -n "$dirty" ]; then
  err "working tree is dirty; commit or stash before landing"
  git status --short >&2
  exit 1
fi

if [ -x scripts/git-doctor.sh ]; then
  info "running git-doctor --fix"
  scripts/git-doctor.sh --fix
fi

info "fetching origin"
git fetch origin

head_sha="$(git rev-parse HEAD)"
short_sha="$(git rev-parse --short HEAD)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
safe_branch="$(printf '%s' "$branch" | tr -c 'A-Za-z0-9._-' '-')"
backup_ref="refs/heads/preserve/session-${safe_branch}-${stamp}-${short_sha}"
backup_name="${backup_ref#refs/heads/}"

info "backing up ${branch} (${short_sha}) to origin/${backup_name}"
git push origin "${head_sha}:${backup_ref}"

info "rebasing ${branch} onto latest origin/main"
if ! git rebase origin/main; then
  err "rebase stopped before landing; resolve conflicts, then continue or abort the rebase"
  err "your pre-rebase work is safe at origin/${backup_name}"
  git status --short >&2
  exit 1
fi

current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ "$current_branch" != "$branch" ]; then
  err "expected to remain on ${branch}, but HEAD is now ${current_branch:-detached}"
  err "your pre-rebase work is safe at origin/${backup_name}"
  exit 1
fi

dirty="$(git status --porcelain)"
if [ -n "$dirty" ]; then
  err "tree became dirty after rebase; resolve before pushing"
  err "your pre-rebase work is safe at origin/${backup_name}"
  git status --short >&2
  exit 1
fi

info "pushing HEAD to main with normal fast-forward semantics"
if git push origin HEAD:main; then
  landed="$(git rev-parse --short HEAD)"
  info "landed ${landed} on origin/main"
  exit 0
fi

err "push was rejected, likely because a sibling landed first."
err "Your work is preserved at origin/${backup_name}."
err "Recover/continue:"
err "  git fetch origin && git rebase origin/main && scripts/land.sh"
exit 1
