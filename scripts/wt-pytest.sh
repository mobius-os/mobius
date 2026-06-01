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
# (an empty PATH makes POST /api/apps/ 422 "esbuild not installed", which
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

VENV="$MAIN/backend/.venv/bin/python"
ESB_DIR="$MAIN/frontend/node_modules/.bin"

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
exec env \
  PATH="$ESB_DIR:$PATH" \
  SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}" \
  "$VENV" -m pytest -p no:cacheprovider "$@"
