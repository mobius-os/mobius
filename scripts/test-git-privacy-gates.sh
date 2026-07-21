#!/usr/bin/env bash
# Regression tests for the public-repository privacy gates.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mobius-privacy-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

fail() {
  echo "privacy-gates-test: $*" >&2
  exit 1
}

# Private workspace roots must not be sent to the Docker daemon either. Exact
# root patterns cover both directories and the local symlinks used here.
for path in docs demo-logs .claude .pm AGENTS.md CLAUDE.md; do
  grep -Fxq "$path" "$ROOT/.dockerignore" \
    || fail ".dockerignore missed $path"
done

repo="$TMP/repo"
git init -q "$repo"
git -C "$repo" config user.name Privacy-Test
git -C "$repo" config user.email privacy-test@invalid
mkdir -p "$repo/scripts/githooks"
cp "$ROOT/.gitignore" "$repo/.gitignore"
cp "$ROOT/scripts/pre-commit.sh" "$repo/scripts/pre-commit.sh"
cp "$ROOT/scripts/install-hooks.sh" "$repo/scripts/install-hooks.sh"
cp "$ROOT/scripts/githooks/pre-push" "$repo/scripts/githooks/pre-push"
cp "$ROOT/scripts/land.sh" "$repo/scripts/land.sh"
cp "$ROOT/scripts/check-private-paths.sh" "$repo/scripts/check-private-paths.sh"
chmod +x "$repo/scripts/"*.sh "$repo/scripts/githooks/pre-push"

# Root-anchored ignores must cover directories, filenames, and the symlink form
# used to expose out-of-repo private state in a clean local checkout.
for path in docs demo-logs .claude .pm AGENTS.md CLAUDE.md; do
  git -C "$repo" check-ignore -q "$path" || fail ".gitignore missed $path"
done
ln -s "$TMP/private-docs" "$repo/docs"
git -C "$repo" check-ignore -q docs || fail ".gitignore missed docs symlink"
rm "$repo/docs"

# A legacy frontend publish-backup spelling once entered production as a
# 100-file local reconciliation commit. It is generated output, not source.
git -C "$repo" check-ignore -q frontend/dist.old/assets/app.js \
  || fail ".gitignore missed frontend/dist.old build backup"

git -C "$repo" add .gitignore scripts
git -C "$repo" commit -qm baseline

(cd "$repo" && scripts/install-hooks.sh >/dev/null)
cmp -s "$repo/scripts/pre-commit.sh" "$repo/.git/hooks/pre-commit" \
  || fail "installer did not install pre-commit"
cmp -s "$repo/scripts/githooks/pre-push" "$repo/.git/hooks/pre-push" \
  || fail "installer did not install pre-push"
[ "$(git -C "$repo" rev-parse --path-format=absolute --git-path hooks)" = \
  "$repo/.git/hooks" ] || fail "installer did not activate the repository hook directory"

# Pre-commit must reject all private roots even when force-added.
mkdir -p "$repo/docs" "$repo/demo-logs" "$repo/.claude" "$repo/.pm"
for path in \
  docs/private.md demo-logs/private.log .claude/private.json .pm/private.md \
  AGENTS.md CLAUDE.md; do
  printf 'private\n' >"$repo/$path"
  git -C "$repo" add -f "$path"
done
if (cd "$repo" && scripts/pre-commit.sh >/dev/null 2>&1); then
  fail "pre-commit accepted private paths"
fi
git -C "$repo" reset -q --hard HEAD

printf 'public\n' >"$repo/public.txt"
printf 'markdown without a code fence\n' >"$repo/public.md"
git -C "$repo" add public.txt public.md
(cd "$repo" && scripts/pre-commit.sh >/dev/null) || fail "pre-commit rejected a public path"
git -C "$repo" commit -qm public

# A seed-skill prose-only change must not pay for the seven-minute backend
# suite on a direct main push. The hook still runs its privacy and syntax gates;
# assert only that it does not classify this shipped Markdown as backend code.
mkdir -p "$repo/backend/scripts/seed-skills"
printf '# Building apps\n' >"$repo/backend/scripts/seed-skills/building-apps.md"
git -C "$repo" add backend/scripts/seed-skills/building-apps.md
git -C "$repo" commit -qm seed-skill-doc
doc_sha="$(git -C "$repo" rev-parse HEAD)"
doc_base="$(git -C "$repo" rev-parse HEAD^)"
doc_push_output="$({
  printf 'refs/heads/master %s refs/heads/main %s\n' "$doc_sha" "$doc_base"
} | (cd "$repo" && scripts/githooks/pre-push) 2>&1)" \
  || fail "pre-push rejected a seed-skill documentation change"
if printf '%s\n' "$doc_push_output" | grep -qE 'running backend pytest|backend changed,'; then
  fail "pre-push classified seed-skill Markdown as executable backend code"
fi
printf '%s\n' "$doc_push_output" \
  | grep -q 'backend seed-skill docs changed' \
  || fail "pre-push did not report its seed-skill documentation fast path"

# land.sh may reuse a backend result only for the exact local object and remote
# base that the preflight verified. The real push still invokes the hook; an
# exact receipt avoids re-running the long suite with a remote transport open,
# while any stale or forged receipt fails closed.
mkdir -p "$repo/backend"
printf 'VALUE = 1\n' >"$repo/backend/preflight_fixture.py"
git -C "$repo" add backend/preflight_fixture.py
git -C "$repo" commit -qm backend-preflight-fixture
backend_sha="$(git -C "$repo" rev-parse HEAD)"
backend_base="$(git -C "$repo" rev-parse HEAD^)"
backend_push_output="$({
  printf 'refs/heads/master %s refs/heads/main %s\n' "$backend_sha" "$backend_base"
} | (cd "$repo" \
  && MOBIUS_PREPUSH_VERIFIED_SHA="$backend_sha" \
     MOBIUS_PREPUSH_VERIFIED_REMOTE_SHA="$backend_base" \
     scripts/githooks/pre-push) 2>&1)" \
  || fail "pre-push rejected an exact preflight receipt"
printf '%s\n' "$backend_push_output" \
  | grep -q 'backend pytest already passed' \
  || fail "pre-push did not reuse the exact preflight result"
if {
  printf 'refs/heads/master %s refs/heads/main %s\n' "$backend_sha" "$backend_base"
} | (cd "$repo" \
  && MOBIUS_PREPUSH_VERIFIED_SHA="$backend_sha" \
     MOBIUS_PREPUSH_VERIFIED_REMOTE_SHA="$doc_base" \
     scripts/githooks/pre-push >/dev/null 2>&1); then
  fail "pre-push accepted a stale remote SHA receipt"
fi

# A committed private path must be rejected by the reusable CI check, the
# pre-push hook, and land.sh before land.sh attempts any remote backup push.
git -C "$repo" switch -qc private-fixture
mkdir -p "$repo/.pm"
printf 'private\n' >"$repo/.pm/private.md"
git -C "$repo" add -f .pm/private.md
git -C "$repo" commit -qm private-fixture --no-verify
private_sha="$(git -C "$repo" rev-parse HEAD)"

# Delete the path again so the tip tree looks clean while its object remains
# reachable. Every history-level gate must still reject this branch.
git -C "$repo" rm -q -f .pm/private.md
git -C "$repo" commit -qm remove-private-fixture
history_sha="$(git -C "$repo" rev-parse HEAD)"

if git -C "$repo" show HEAD:.pm/private.md >/dev/null 2>&1; then :; else
  git -C "$repo" show "$private_sha":.pm/private.md >/dev/null 2>&1 \
    || fail "private fixture was not committed"
fi
if git -C "$repo" ls-tree -r --name-only HEAD -- .pm | grep -q .; then
  fail "private fixture still exists in the tip tree"
fi
if (cd "$repo" && scripts/check-private-paths.sh >/dev/null 2>&1); then
  fail "CI history check accepted an add-then-delete private path"
fi
if (cd "$repo" && scripts/githooks/pre-push </dev/null >/dev/null 2>&1); then
  fail "pre-push accepted an add-then-delete private path"
fi
if (cd "$repo" && scripts/land.sh >/dev/null 2>&1); then
  fail "land.sh accepted an add-then-delete private path"
fi

# The pushed object can differ from HEAD (`git push other:remote`). Verify the
# hook checks the object supplied on pre-push stdin, not merely the checkout.
git -C "$repo" switch -q master
zero=0000000000000000000000000000000000000000
if printf 'refs/heads/private-fixture %s refs/heads/leak %s\n' \
     "$history_sha" "$zero" \
     | (cd "$repo" && scripts/githooks/pre-push >/dev/null 2>&1); then
  fail "pre-push accepted private history from a non-HEAD ref"
fi

echo "privacy-gates-test: OK"
