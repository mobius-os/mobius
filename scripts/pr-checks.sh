#!/usr/bin/env bash
# Wait condition for a pull request's checks on one exact commit.
#
#   scripts/pr-checks.sh <owner/repo> <pr-number> <commit-sha>
#
# Exit 0 when every check run on <commit-sha> has completed (pass or fail;
# read the results after waking), 1 while any is still pending, and 2 with a
# message when <commit-sha> is not the PR's public head. A rejected or
# never-pushed update therefore wakes the waiting chat at once instead of
# leaving it waiting for checks that will never run. Read-only.

set -uo pipefail

[ $# -eq 3 ] && [ ${#3} -ge 7 ] || {
  echo "usage: scripts/pr-checks.sh <owner/repo> <pr-number> <commit-sha (7+ chars)>" >&2; exit 2; }
repo=$1 pr=$2 sha=$3

head=$(gh api "repos/$repo/pulls/$pr" --jq .head.sha 2>/dev/null) || {
  echo "pr-checks: cannot read $repo#$pr" >&2; exit 2; }
case "$head" in
  "$sha"*) ;;
  *) echo "pr-checks: $repo#$pr head is ${head:0:12}, not ${sha:0:12}: that commit was never published (e.g. a rejected update) or was replaced" >&2
     exit 2 ;;
esac

# "<total> <pending>" for the head commit's check runs.
read -r total pending < <(gh api "repos/$repo/commits/$head/check-runs?per_page=100" \
  --jq '"\(.total_count) \([.check_runs[] | select(.status != "completed")] | length)"' 2>/dev/null) || {
  echo "pr-checks: cannot read checks for ${head:0:12}" >&2; exit 2; }
[ "${total:-0}" -gt 0 ] && [ "${pending:-1}" -eq 0 ] && exit 0
exit 1
