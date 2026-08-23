#!/usr/bin/env bash
# Run the backend pytest suite from ANY worktree with the right deps.
#
# Worktrees don't get their own node_modules / backend venv, so this
# resolves both from the MAIN checkout (the same trick the pre-push hook
# uses): the locked frontend Rolldown tree + the
# shared venv for deps, while the WORKTREE's backend/ is the code under test.
# This removes the single most-repeated bit of friction — the long
# PATH=... SECRET_KEY=... venv-python incantation — and sidesteps the
# bundler-path-from-worktree trap that has caused a ~70-test false alarm
# (a missing compiler makes explicit app apply fail, which
# cascades and looks exactly like a mass regression).
#
# Usage (from anywhere inside a worktree or the main checkout):
#   scripts/wt-pytest.sh                       # full suite
#   scripts/wt-pytest.sh tests/test_foo.py -q  # a subset — args pass through
#   scripts/wt-pytest.sh backend/tests/test_foo.py -q  # repo-relative also works
#   scripts/wt-pytest.sh -k name -x            # any pytest args
#
# Bypass the deps resolution by exporting SECRET_KEY yourself; this script
# only fills it in when unset.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)" || {
  echo "wt-pytest: not inside a git checkout" >&2; exit 1; }
# Main checkout = parent of the SHARED git dir; equals $ROOT in the main
# checkout, and the real main checkout from any linked worktree.
MAIN="$(cd "$(dirname "$(git rev-parse --git-common-dir)")" && pwd 2>/dev/null)" \
  || MAIN="$ROOT"

# Make it obvious WHICH tree is under test — running from the main checkout
# silently tests `main`, not your worktree (a real this-session footgun).
if [ "$ROOT" = "$MAIN" ]; then
  echo "wt-pytest: testing the MAIN checkout (not a worktree)" >&2
else
  echo "wt-pytest: testing worktree '$(basename "$ROOT")'" >&2
fi

VENV="$MAIN/backend/.venv/bin/python"
WORKTREE_NODE_MODULES="$ROOT/frontend/node_modules"
SHARED_NODE_MODULES="$MAIN/frontend/node_modules"
CONTRIB_ROOT="$(dirname "$MAIN")/contrib"

backend_test_node_deps() {
  local frontend="$1"
  local modules="$frontend/node_modules"
  [ -x "$modules/.bin/rolldown" ] || return 1
  NODE_PATH="$modules" node -e \
    "require.resolve('acorn'); require.resolve('eslint-scope')" \
    >/dev/null 2>&1
}

# An integration worktree may carry a lockfile newer than main while another
# reviewed worktree already has that exact dependency tree installed. Reuse
# only an exact lockfile match and verify the small Node surface the backend
# tests actually execute; a full `npm ls` is a frontend-suite concern and made
# otherwise-hermetic backend tests depend on unrelated package completeness.
matching_contrib_node_modules() {
  local frontend
  [ "$ROOT" != "$MAIN" ] || return 1
  for frontend in "$CONTRIB_ROOT"/*/worktree/frontend; do
    [ "$frontend" != "$ROOT/frontend" ] || continue
    [ -f "$frontend/package-lock.json" ] || continue
    cmp -s "$ROOT/frontend/package-lock.json" "$frontend/package-lock.json" \
      || continue
    if backend_test_node_deps "$frontend"; then
      printf '%s\n' "$frontend/node_modules"
      return 0
    fi
  done
  return 1
}

if backend_test_node_deps "$ROOT/frontend"; then
  NODE_MODULES="$WORKTREE_NODE_MODULES"
elif [ "$ROOT" != "$MAIN" ] \
    && cmp -s "$ROOT/frontend/package-lock.json" "$MAIN/frontend/package-lock.json" \
    && backend_test_node_deps "$MAIN/frontend"; then
  NODE_MODULES="$SHARED_NODE_MODULES"
elif NODE_MODULES="$(matching_contrib_node_modules)"; then
  echo "wt-pytest: reusing exact-lock backend-test dependencies from $(dirname "$NODE_MODULES")" >&2
else
  echo "wt-pytest: no verified backend Node dependencies match this worktree" >&2
  echo "  install them with: (cd \"$ROOT/frontend\" && npm ci)" >&2
  exit 1
fi
NODE_BIN_DIR="$NODE_MODULES/.bin"

# The runner changes cwd to backend/ before invoking pytest. Accept both the
# backend-relative paths documented by pytest and the natural repo-relative
# paths callers get from search output, so a harmless path prefix does not
# require a failed run and retry.
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    backend/tests/*) PYTEST_ARGS+=("${arg#backend/}") ;;
    *) PYTEST_ARGS+=("$arg") ;;
  esac
done

if [ -x "$VENV" ]; then
  PYTHON="$VENV"
elif python3 -c 'import pytest' >/dev/null 2>&1; then
  # The running image already carries the backend dependencies. The explicit
  # MOBIUS_TEST_RUNTIME environment below is the safety boundary; using this
  # interpreter through the wrapper is not the guarded direct-pytest path.
  PYTHON="$(command -v python3)"
  echo "wt-pytest: shared venv absent; using the image's Python test runtime" >&2
  if [ -r /app/requirements.lock ] \
      && ! cmp -s "$ROOT/backend/requirements.lock" /app/requirements.lock; then
    echo "wt-pytest: WARNING — checkout requirements.lock differs from the image runtime" >&2
    echo "wt-pytest: results are useful but not dependency-authoritative; use a lock-matched venv or hosted checks" >&2
  fi
else
  echo "wt-pytest: neither shared venv nor image pytest is available" >&2
  echo "  create the shared venv once with:" >&2
  echo "    python3 -m venv \"$MAIN/backend/.venv\" \\" >&2
  echo "      && \"$MAIN/backend/.venv/bin/pip\" install --require-hashes -r \"$MAIN/backend/requirements.lock\"" >&2
  exit 1
fi
cd "$ROOT/backend" || exit 1
# The worktree's backend/ is on sys.path (cwd); the venv supplies deps; the
# generated SECRET_KEY satisfies pydantic Settings for tests that build it.
# NODE_PATH exposes the same shared dependency tree to Node subprocesses whose
# scripts live in the worktree; PATH alone finds binaries but cannot satisfy a
# script-level require('acorn') or similar package import.
# GIT_CEILING_DIRECTORIES="$ROOT" stops git's upward repo discovery at this
# checkout's root, so an app-git test (.pm/096) can't walk out of its tmpdir
# and mutate this checkout's .git (flip core.bare / append "Initialize app
# repo" commits). Verified safe: no backend test relies on implicit discovery
# of the enclosing repo, and the app-git tests use explicit -C <tmp_path>.
TEST_ENV=(env \
  GIT_CEILING_DIRECTORIES="$ROOT" \
  MOBIUS_TEST_RUNTIME=1 \
  MOEBIUS_SKIP_BOOTSTRAP=1 \
  API_BASE_URL=http://127.0.0.1:9 \
  PATH="$NODE_BIN_DIR:${PATH:-}" \
  MOBIUS_APP_NODE_PATH="$NODE_MODULES" \
  NODE_PATH="$NODE_MODULES${NODE_PATH:+:$NODE_PATH}" \
  SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}")

if [ "${#PYTEST_ARGS[@]}" -eq 0 ]; then
  exec "${TEST_ENV[@]}" "$PYTHON" -m pytest -p no:cacheprovider
fi

exec "${TEST_ENV[@]}" "$PYTHON" -m pytest -p no:cacheprovider \
  "${PYTEST_ARGS[@]}"
