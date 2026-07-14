#!/usr/bin/env bash
# Fail if a git tree contains private workspace roots.
set -euo pipefail

ref="${1:-HEAD}"
private_paths=(docs demo-logs .claude .pm AGENTS.md CLAUDE.md)

if ! git cat-file -e "${ref}^{tree}" 2>/dev/null; then
  echo "check-private-paths: cannot resolve tree for ${ref}" >&2
  exit 2
fi

commits="$(git rev-list "$ref" -- "${private_paths[@]}")"
if [ -n "$commits" ]; then
  echo "check-private-paths: private workspace paths occur in history reachable from ${ref}:" >&2
  git log -n 8 --format='    %h %s' "$ref" -- "${private_paths[@]}" >&2
  echo "check-private-paths: deleting a path later does not remove it from history" >&2
  exit 1
fi

echo "check-private-paths: OK (${ref})"
