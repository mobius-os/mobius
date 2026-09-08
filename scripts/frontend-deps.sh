#!/usr/bin/env bash
# Shared lockfile/provenance checks for frontend dependency trees used from
# linked worktrees. Source this file; it intentionally does not execute work.

_MOBIUS_FRONTEND_DEPS_DIR="$(
  cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
)"

mobius_frontend_deps_status() {
  local frontend="$1"
  local modules="$frontend/node_modules"
  local resolved target_frontend

  if [ ! -e "$modules" ]; then
    printf '%s\n' missing
    return 2
  fi

  if [ -L "$modules" ]; then
    resolved="$(readlink -f "$modules" 2>/dev/null || true)"
    target_frontend="$(dirname "$resolved")"
    if [ -z "$resolved" ] \
        || [ ! -f "$frontend/package-lock.json" ] \
        || [ ! -f "$target_frontend/package-lock.json" ] \
        || ! cmp -s "$frontend/package-lock.json" "$target_frontend/package-lock.json"; then
      printf '%s\n' lock-mismatch
      return 3
    fi
  fi

  if ! command -v node >/dev/null 2>&1 \
      || ! command -v npm >/dev/null 2>&1; then
    printf '%s\n' incomplete
    return 4
  fi
  node "$_MOBIUS_FRONTEND_DEPS_DIR/check-frontend-deps.mjs" "$frontend"
}

mobius_frontend_deps_path() {
  local frontend="$1"
  mobius_frontend_deps_status "$frontend" >/dev/null || return $?
  readlink -f "$frontend/node_modules"
}
