#!/usr/bin/env bash
# Rebase the current session branch, publish it safely, and open/update its PR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
cd "$ROOT"

err() { printf 'submit-pr: %s\n' "$*" >&2; }
info() { printf 'submit-pr: %s\n' "$*"; }

check_private_history() {
  local commits
  commits="$(git rev-list HEAD -- \
    docs demo-logs .claude .pm AGENTS.md CLAUDE.md 2>/dev/null || true)"
  if [ -n "$commits" ]; then
    err "refusing to publish history containing private workspace paths:"
    git log -n 8 --format='    %h %s' HEAD -- \
      docs demo-logs .claude .pm AGENTS.md CLAUDE.md >&2
    err "deleting a path later is insufficient; purge it from reachable history"
    return 1
  fi
}

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ -z "$branch" ] || [ "$branch" = "main" ]; then
  err "check out a topic branch before submitting a pull request"
  exit 2
fi

if [ -n "$(git status --porcelain)" ]; then
  err "working tree is dirty; commit or stash before submitting"
  git status --short >&2
  exit 1
fi

check_private_history

if [ -x scripts/git-doctor.sh ]; then
  info "running git-doctor --fix"
  scripts/git-doctor.sh --fix
fi

command -v gh >/dev/null 2>&1 || {
  err "GitHub CLI is required to create or find the pull request"
  exit 1
}

info "rebasing ${branch} onto current origin/main"
git fetch origin main
git rebase origin/main

if [ -n "$(git status --porcelain)" ]; then
  err "tree became dirty after rebase; resolve it before publishing"
  git status --short >&2
  exit 1
fi
check_private_history

# origin/main may have advanced the landed hook policy since the initial
# doctor run. Re-check after the fetch and rebase so a stale installed hook
# cannot approve a push under rules that main has already replaced.
info "verifying landed git-hook policy"
if [ -x scripts/git-doctor.sh ]; then
  scripts/git-doctor.sh --fix
fi

# A rebase rewrites the topic branch. Lease protection rejects the update if a
# collaborator moved the remote branch since the fetch above.
info "publishing ${branch}"
git push --force-with-lease="refs/heads/${branch}" \
  --set-upstream origin "HEAD:refs/heads/${branch}"

if url="$(gh pr view "$branch" --json url --jq .url 2>/dev/null)"; then
  info "updated ${url}"
else
  info "opening pull request"
  gh pr create --base main --head "$branch" --fill
fi
