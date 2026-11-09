#!/usr/bin/env bash
# Regression test: the pre-push gate runs real Vite-based frontend tests on an
# exact-lock dependency tree borrowed from a read-only directory (such as the
# image's install) without writing through it or leaving a loan behind, while
# lock drift, a genuine test failure, and private history still block a push.
#
#   scripts/test-prepush-dependency-loan.sh [frontend/node_modules to mirror]
#
# The default source is this checkout's own install. It must have been built
# from this checkout's exact frontend/package-lock.json. Run unprivileged:
# root ignores the read-only permissions the fixture depends on.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
SOURCE_MODULES="$(readlink -f "${1:-$ROOT/frontend/node_modules}" || true)"

fail() {
  echo "prepush-loan-test: $*" >&2
  exit 1
}

[ "$(id -u)" -ne 0 ] || fail "run as an unprivileged user"
[ -d "$SOURCE_MODULES" ] || fail "no frontend dependency install to mirror"
cmp -s "$ROOT/frontend/package-lock.json" \
  "$(dirname "$SOURCE_MODULES")/package-lock.json" \
  || fail "$SOURCE_MODULES was not installed from this package-lock.json"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/mobius-prepush-loan.XXXXXX")"
IMAGE="$TMP/image"
cleanup() {
  # Only directories were made read-only; files may be hard links shared with
  # the source install, so their modes are never touched.
  [ -d "$IMAGE" ] && find "$IMAGE" -type d -exec chmod u+w {} +
  rm -rf "$TMP"
}
trap cleanup EXIT

# The read-only "image" install. Hard links keep it cheap on one filesystem.
mkdir -p "$IMAGE"
cp "$ROOT/frontend/package.json" "$ROOT/frontend/package-lock.json" "$IMAGE/"
if ! cp -al "$SOURCE_MODULES" "$IMAGE/node_modules" 2>/dev/null; then
  rm -rf "$IMAGE/node_modules"
  cp -a "$SOURCE_MODULES" "$IMAGE/node_modules"
fi
# Vite's default write sites inside a dependency tree. A fresh install lacks
# them and Vite then falls back elsewhere for some writes; a used tree has
# them, so make both present and read-only.
mkdir -p "$IMAGE/node_modules/.vite" "$IMAGE/node_modules/.vite-temp"
find "$IMAGE" -type d -exec chmod a-w {} +
if mkdir "$IMAGE/node_modules/.write-probe" 2>/dev/null; then
  fail "fixture dependency tree is writable"
fi
MARKER="$TMP/before-gate"
touch "$MARKER"

assert_image_untouched() {
  local written
  written="$(find "$IMAGE" -newer "$MARKER" -print -quit)"
  [ -z "$written" ] || fail "$1 wrote into the borrowed dependency tree: $written"
}

repo="$TMP/repo"
git init -q -b main "$repo"
git -C "$repo" config user.name Loan-Test
git -C "$repo" config user.email loan-test@invalid
for path in \
    .gitignore \
    scripts/githooks/pre-push scripts/install-hooks.sh scripts/pre-commit.sh \
    scripts/wt-npm.sh scripts/frontend-deps.sh scripts/check-frontend-deps.mjs \
    frontend/package-lock.json frontend/vite.config.js \
    frontend/scripts/precache-policy.mjs frontend/scripts/check-js-syntax.mjs \
    frontend/src/lib/speech/speechWorkerAsset.js \
    frontend/src/lib/speech/speechPitchAsset.js \
    frontend/src/lib/__tests__/ssrRenderServer.mjs; do
  mkdir -p "$(dirname "$repo/$path")"
  cp -p "$ROOT/$path" "$repo/$path"
done
# The same manifest with the suite replaced by one component render through
# the real Vite configuration and test server helper.
node -e '
  const fs = require("node:fs")
  const [source, target] = process.argv.slice(1)
  const manifest = JSON.parse(fs.readFileSync(source, "utf8"))
  manifest.scripts = { test: "node --test src/loan.test.js" }
  fs.writeFileSync(target, JSON.stringify(manifest, null, 2) + "\n")
' "$ROOT/frontend/package.json" "$repo/frontend/package.json"
cat >"$repo/frontend/src/Greeting.jsx" <<'EOF'
export default function Greeting() {
  return <p>ready</p>
}
EOF
cat >"$repo/frontend/src/loan.test.js" <<'EOF'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from './lib/__tests__/ssrRenderServer.mjs'

test('renders a component through the real Vite configuration', async () => {
  const vite = await createSsrRenderServer()
  try {
    const { default: Greeting } = await vite.ssrLoadModule('/src/Greeting.jsx')
    assert.equal(renderToStaticMarkup(createElement(Greeting)), '<p>ready</p>')
  } finally {
    await vite.close()
  }
})
EOF
git -C "$repo" add -A
git -C "$repo" commit -qm baseline --no-verify
git clone -q --bare "$repo" "$TMP/origin.git"
git -C "$repo" remote add origin "$TMP/origin.git"
git -C "$repo" fetch -q origin
(cd "$repo" && scripts/install-hooks.sh >/dev/null)

# A review worktree whose dependencies are an explicit exact-lock loan.
review="$TMP/review"
git -C "$repo" worktree add -q -b fix/loan "$review" main
ln -s "$IMAGE/node_modules" "$review/frontend/node_modules"

commit_frontend() {
  printf '%s\n' "$2" >"$review/frontend/src/Greeting.jsx"
  git -C "$review" commit -qam "$1"
}

push_review() {
  git -C "$review" push origin HEAD:refs/heads/fix/loan 2>&1
}

assert_no_loan_residue() {
  [ "$(readlink "$review/frontend/node_modules")" = "$IMAGE/node_modules" ] \
    || fail "$1 replaced the review checkout's dependency link"
  if compgen -G "$review/frontend/.node_modules.borrow.*" >/dev/null \
      || compgen -G "$review/frontend/vite.config.js.timestamp-*" >/dev/null; then
    fail "$1 left temporary files in the review checkout"
  fi
  [ -z "$(git -C "$review" status --porcelain)" ] \
    || fail "$1 left the review checkout dirty"
}

commit_frontend passing \
  'export default function Greeting() { return <p>ready</p> }'
output="$(push_review)" || fail "gate rejected a passing read-only loan: $output"
printf '%s\n' "$output" | grep -q 'frontend-unit passed' \
  || fail "gate did not run frontend-unit: $output"
assert_image_untouched "a passing gate"
assert_no_loan_residue "a passing gate"

commit_frontend failing \
  'export default function Greeting() { return <p>broken</p> }'
output="$(push_review)" && fail "gate accepted a failing frontend test"
printf '%s\n' "$output" | grep -q 'verdict=blocked cause=checks checks=frontend-unit$' \
  || fail "gate did not attribute the test failure: $output"
assert_image_untouched "a failing gate"
assert_no_loan_residue "a failing gate"
git -C "$review" reset -q --hard HEAD~1

printf '\n' >>"$review/frontend/package-lock.json"
git -C "$review" commit -qam lock-drift
output="$(push_review)" && fail "gate accepted a dependency lock mismatch"
printf '%s\n' "$output" | grep -q 'checks=frontend-deps-lock' \
  || fail "gate did not report the lock mismatch: $output"
git -C "$review" reset -q --hard HEAD~1

mkdir -p "$review/.pm"
printf 'private\n' >"$review/.pm/private.md"
git -C "$review" add -f .pm/private.md
git -C "$review" commit -qm private --no-verify
git -C "$review" rm -q .pm/private.md
git -C "$review" commit -qm remove-private --no-verify
output="$(push_review)" && fail "gate accepted private history"
printf '%s\n' "$output" | grep -q 'verdict=blocked cause=privacy' \
  || fail "gate did not report private history: $output"
git -C "$review" reset -q --hard HEAD~2

# The runner's own loan: a worktree with no dependency tree borrows the primary
# checkout's exact-lock link to the read-only install, then returns it.
ln -s "$IMAGE/node_modules" "$repo/frontend/node_modules"
borrower="$TMP/borrower"
git -C "$repo" worktree add -q -b borrower "$borrower" fix/loan
output="$(cd "$borrower" && bash "$repo/.git/hooks/wt-npm.sh" test 2>&1)" \
  || fail "runner could not borrow the read-only install: $output"
printf '%s\n' "$output" | grep -q 'borrowing exact-lock dependencies' \
  || fail "runner did not borrow: $output"
[ ! -e "$borrower/frontend/node_modules" ] && [ ! -L "$borrower/frontend/node_modules" ] \
  || fail "runner left its dependency loan behind"
[ -z "$(git -C "$borrower" status --porcelain)" ] \
  || fail "runner left the borrowing checkout dirty"
assert_image_untouched "the borrowing runner"

echo "prepush-loan-test: ok"
