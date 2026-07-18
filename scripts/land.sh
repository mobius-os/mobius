#!/usr/bin/env bash
# Land the current session branch onto origin/main without clobbering siblings.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
cd "$ROOT"

err() { printf 'land: %s\n' "$*" >&2; }
info() { printf 'land: %s\n' "$*"; }

# Keep short pushes resilient to transient network idleness. The main push also
# uses an exact-SHA preflight below, so the remote transport is never held open
# while the full backend suite runs.
git_push() {
  local ssh_command="${GIT_SSH_COMMAND:-ssh}"
  GIT_SSH_COMMAND="${ssh_command} -o ServerAliveInterval=30 -o ServerAliveCountMax=30" \
    git push "$@"
}

check_private_history() {
  local ref="${1:-HEAD}"
  local commits
  commits="$(git rev-list "$ref" -- \
    docs demo-logs .claude .pm AGENTS.md CLAUDE.md 2>/dev/null || true)"
  if [ -n "$commits" ]; then
    err "refusing to land history containing private workspace paths from ${ref}:"
    git log -n 8 --format='    %h %s' "$ref" -- \
      docs demo-logs .claude .pm AGENTS.md CLAUDE.md >&2
    err "deleting a path later is insufficient; purge it from reachable history"
    err "this privacy gate must not be bypassed"
    return 1
  fi
}

preflight_main_push() {
  local local_sha="$1"
  local remote_sha="$2"
  local remote_url
  remote_url="$(git remote get-url origin)"

  info "running the pre-push gate before opening the main transport"
  printf 'HEAD %s refs/heads/main %s\n' "$local_sha" "$remote_sha" \
    | env -u MOBIUS_PREPUSH_VERIFIED_SHA \
          -u MOBIUS_PREPUSH_VERIFIED_REMOTE_SHA \
          scripts/githooks/pre-push origin "$remote_url"

  if [ "$(git rev-parse HEAD)" != "$local_sha" ]; then
    err "HEAD changed during preflight; refusing to push an unverified object"
    return 1
  fi
}

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ -z "$branch" ]; then
  err "detached HEAD; check out a session branch before landing"
  exit 2
fi

# This must run before the preservation push below. Otherwise the backup ref
# intended to protect work could itself publish private workspace material.
check_private_history HEAD

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
git_push origin "${head_sha}:${backup_ref}"

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

# Rebase can introduce paths from the updated base. Re-check the exact tree
# that will be sent to main.
check_private_history HEAD

verified_sha="$(git rev-parse HEAD)"
verified_remote_sha="$(git rev-parse origin/main)"
preflight_main_push "$verified_sha" "$verified_remote_sha"

info "pushing verified HEAD to main with normal fast-forward semantics"
if MOBIUS_PREPUSH_VERIFIED_SHA="$verified_sha" \
   MOBIUS_PREPUSH_VERIFIED_REMOTE_SHA="$verified_remote_sha" \
   git_push origin "${verified_sha}:refs/heads/main"; then
  landed="${verified_sha:0:10}"
  info "landed ${landed} on origin/main"
  exit 0
fi

err "push was rejected, likely because a sibling landed first."
err "Your work is preserved at origin/${backup_name}."
err "Recover/continue:"
err "  git fetch origin && git rebase origin/main && scripts/land.sh"
exit 1
