#!/usr/bin/env bash
# git-doctor — diagnose (and optionally repair) git-state corruption that
# leaks from test runs into this shared repo.
#
# The one auto-repairable class is `core.bare=true`: app-git tests (.pm/096),
# run with cwd inside a real checkout, can escape their tmpdir and flip
# core.bare in the SHARED .git/config — which makes every worktree's
# `git status`/`commit` fail with "this operation must be run in a work tree".
# The files are intact; only the flag is wrong. This repairs it.
#
# Detached HEAD and an inaccessible HEAD are REPORTED but not auto-repaired —
# they're usually legitimate (a failed push leaves a detached worktree, an
# interrupted rebase, etc.), not silent corruption like core.bare.
#
# Usage:
#   scripts/git-doctor.sh          # report only (exit 1 if issues found)
#   scripts/git-doctor.sh --fix    # auto-repair core.bare only
set -uo pipefail

FIX="${1:-}"
FAILS=0

c_warn=$'\033[1;33m'; c_err=$'\033[1;31m'; c_off=$'\033[0m'
err()  { printf '%s%s%s\n' "$c_err"  "$*" "$c_off" >&2; }
warn() { printf '%s%s%s\n' "$c_warn" "$*" "$c_off" >&2; }

# 1. core.bare — the critical, auto-repairable case. `git config --local` in a
#    worktree writes the SHARED .git/config, so this repair propagates to all
#    sibling worktrees — which is exactly right for this corruption.
if git config --local core.bare 2>/dev/null | grep -q '^true$'; then
  err "core.bare=true in the shared config (app-git test escape, .pm/096)"
  if [ "$FIX" = "--fix" ]; then
    git config --local core.bare false && warn "repaired: core.bare -> false"
  else
    warn "fix with: scripts/git-doctor.sh --fix"
    FAILS=$((FAILS + 1))
  fi
fi

# 2. HEAD accessible? An inaccessible HEAD means an orphaned/corrupted repo.
if ! git rev-parse HEAD >/dev/null 2>&1; then
  err "HEAD is not accessible (orphaned or corrupted repo) — investigate manually"
  FAILS=$((FAILS + 1))
fi

# 3. Detached HEAD — informational (legitimate in worktrees / after a rebase).
if git rev-parse HEAD >/dev/null 2>&1 && ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
  warn "detached HEAD (expected after a failed push or interrupted rebase)"
fi

# 4. Pollution commits on the current branch vs origin/main — the .pm/096
#    fingerprint. Report (don't auto-drop; the user may have intentional work).
BASE="$(git merge-base HEAD origin/main 2>/dev/null || true)"
if [ -n "$BASE" ]; then
  n="$(git log --format='%an <%ae>' "$BASE"..HEAD 2>/dev/null \
    | grep -c 'Mobius <mobius@localhost>' || true)"
  if [ "${n:-0}" -gt 0 ]; then
    err "$n 'Mobius <mobius@localhost>' commit(s) on this branch — likely app-git test pollution"
    warn "drop with: git reset --hard origin/main  (only if they aren't real work)"
    FAILS=$((FAILS + 1))
  fi
fi

if [ "$FAILS" -eq 0 ]; then
  echo "[git-doctor] OK"
  exit 0
fi
err "[git-doctor] $FAILS issue(s) detected"
exit 1
