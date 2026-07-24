#!/usr/bin/env bash
# Run the backend pytest suite from ANY worktree with the right deps.
#
# Worktrees don't get their own node_modules / backend venv, so this
# resolves both from the MAIN checkout (the same trick the pre-push hook
# uses): esbuild on PATH (the compile/install tests shell out to it) + the
# shared venv for deps, while the WORKTREE's backend/ is the code under test.
# This removes the single most-repeated bit of friction — the long
# PATH=... SECRET_KEY=... venv-python incantation — and sidesteps the
# esbuild-PATH-from-worktree trap that has caused a ~70-test false alarm
# (an empty PATH makes explicit app apply return "esbuild not installed", which
# cascades and looks exactly like a mass regression).
#
# Usage (from anywhere inside a worktree or the main checkout):
#   scripts/wt-pytest.sh                       # full suite
#   scripts/wt-pytest.sh tests/test_foo.py -q  # a subset — args pass through
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

if [ -d "$WORKTREE_NODE_MODULES" ] \
    && (cd "$ROOT/frontend" && npm ls --depth=0 >/dev/null 2>&1); then
  NODE_MODULES="$WORKTREE_NODE_MODULES"
elif [ "$ROOT" != "$MAIN" ] \
    && cmp -s "$ROOT/frontend/package-lock.json" "$MAIN/frontend/package-lock.json" \
    && [ -d "$SHARED_NODE_MODULES" ] \
    && (cd "$MAIN/frontend" && npm ls --depth=0 >/dev/null 2>&1); then
  NODE_MODULES="$SHARED_NODE_MODULES"
else
  echo "wt-pytest: no complete frontend dependencies match this worktree" >&2
  echo "  install them with: (cd \"$ROOT/frontend\" && npm ci)" >&2
  exit 1
fi
ESB_DIR="$NODE_MODULES/.bin"

if [ ! -x "$VENV" ]; then
  echo "wt-pytest: no shared venv at $VENV" >&2
  echo "  create it once with:" >&2
  echo "    python3 -m venv \"$MAIN/backend/.venv\" \\" >&2
  echo "      && \"$MAIN/backend/.venv/bin/pip\" install -r \"$MAIN/backend/requirements.txt\"" >&2
  exit 1
fi
if [ ! -x "$ESB_DIR/esbuild" ]; then
  echo "wt-pytest: warning — esbuild not at $ESB_DIR; compile/install tests" >&2
  echo "  will 422-cascade. Run 'npm ci' in $MAIN/frontend if you need them." >&2
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
exec env \
  GIT_CEILING_DIRECTORIES="$ROOT" \
  MOBIUS_TEST_RUNTIME=1 \
  MOEBIUS_SKIP_BOOTSTRAP=1 \
  API_BASE_URL=http://127.0.0.1:9 \
  PATH="$ESB_DIR:${PATH:-}" \
  NODE_PATH="$NODE_MODULES${NODE_PATH:+:$NODE_PATH}" \
  SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}" \
  "$VENV" -m pytest -p no:cacheprovider "$@"
