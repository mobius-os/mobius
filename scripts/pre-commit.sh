#!/usr/bin/env bash
# pre-commit.sh — Lightweight syntax-only checks for staged files.
#
# Runs in well under 1 second on a typical 5-file commit. NOT a substitute
# for tests or linting — just catches "I committed a file with a typo /
# unbalanced code fence" mistakes that block CI.
#
# Checks performed per staged file (added/copied/modified, NOT deleted):
#   *.py         python3 -c "import ast; ast.parse(...)"
#   *.js, *.mjs  node --check
#   *.md         even count of ``` code-fence markers (balance)
#
# Deliberately skipped:
#   *.jsx / *.ts / *.tsx — node --check cannot parse these natively, and
#                          we don't want to pull in a bundler on pre-commit.
#                          esbuild runs in the build path; let it catch them.
#   tests, linters, formatters — too slow for a pre-commit gate.
#
# Install (pick one — this script is NOT auto-installed):
#
#   # Option A — point core.hooksPath at the scripts/ dir (requires a
#   # `pre-commit` filename, so symlink it once):
#   ln -sf "$(pwd)/scripts/pre-commit.sh" scripts/pre-commit
#   git config core.hooksPath scripts
#
#   # Option B — symlink directly into .git/hooks (per-clone, not tracked):
#   ln -sf "$(pwd)/scripts/pre-commit.sh" .git/hooks/pre-commit
#
# Bypass (always available, by design):
#   git commit --no-verify
#
# Run manually against currently-staged files:
#   bash scripts/pre-commit.sh

set -uo pipefail

# Collect staged files. --diff-filter=ACMR skips deletions (D) and
# unmerged (U); -z + NUL-delimited handles paths with spaces/newlines.
mapfile -d '' STAGED < <(git diff --cached --name-only --diff-filter=ACMR -z)

if [ "${#STAGED[@]}" -eq 0 ]; then
  exit 0
fi

FAILURES=0
fail() {
  printf 'pre-commit: %s\n' "$*" >&2
  FAILURES=$((FAILURES + 1))
}

# Pre-collect by type so we run each tool at most once per language.
PY_FILES=()
JS_FILES=()
MD_FILES=()
for f in "${STAGED[@]}"; do
  # Skip if the file is staged-as-added but missing on disk (rare; git
  # records the blob, not the worktree copy). The checks below rely on
  # reading the worktree file, which matches what git will commit.
  [ -f "$f" ] || continue
  case "$f" in
    *.py)        PY_FILES+=("$f") ;;
    *.js|*.mjs)  JS_FILES+=("$f") ;;
    *.md)        MD_FILES+=("$f") ;;
  esac
done

# --- Python: AST parse -------------------------------------------------------
for f in "${PY_FILES[@]:-}"; do
  [ -n "$f" ] || continue
  if ! err=$(python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])" "$f" 2>&1); then
    fail "python syntax error in $f"
    printf '  %s\n' "$err" >&2
  fi
done

# --- JS / MJS: node --check --------------------------------------------------
if [ "${#JS_FILES[@]}" -gt 0 ] && command -v node >/dev/null 2>&1; then
  for f in "${JS_FILES[@]:-}"; do
    [ -n "$f" ] || continue
    if ! err=$(node --check "$f" 2>&1); then
      fail "javascript syntax error in $f"
      printf '  %s\n' "$err" >&2
    fi
  done
fi

# --- Markdown: code-fence balance --------------------------------------------
# A literal ``` at the start of a line opens or closes a fence. An odd count
# means an unclosed fence — usually a paste-snippet mistake.
for f in "${MD_FILES[@]:-}"; do
  [ -n "$f" ] || continue
  fences=$(grep -c '^```' "$f" 2>/dev/null || echo 0)
  if [ $((fences % 2)) -ne 0 ]; then
    fail "unbalanced code fences in $f (found $fences fence markers, expected even count)"
  fi
done

if [ "$FAILURES" -gt 0 ]; then
  printf '\npre-commit: %d check(s) failed. Fix the issues above, or bypass with `git commit --no-verify`.\n' \
    "$FAILURES" >&2
  exit 1
fi

exit 0
