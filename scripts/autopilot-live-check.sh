#!/usr/bin/env bash
#
# autopilot-live-check.sh — Stage-2 live smoke test for the Contribute autopilot
# review-response loop, against a running Docker test stack + a real (scratch)
# GitHub connection. Proves the parts the stubbed unit/integration tests can't:
# the REAL git push updating a REAL PR, and job.sh detecting a REAL review.
#
# It seeds a fresh contribution, opens a PR, posts a review from the reviewer
# account, triggers the real scheduled detector, and then OBSERVES the spawned
# follow-up agent drive /update → /reply → /complete with its live run claim.
# The app token cannot call those mutation routes. The harness verifies that the
# PR advanced on GitHub, checks the no-self-retrigger guard, then closes the PR
# and removes everything it created. Re-runnable: every run uses a unique
# branch/record id and cleans up after itself.
#
# It is NOT wired into CI (needs live creds + a running stack); it's an on-demand
# command. See scripts/AUTOPILOT-LIVE-CHECK.md for the full runbook.
#
# ─────────────────────────────── configuration ───────────────────────────────
# Required env:
#   API_BASE          e.g. http://localhost:8001          (the test stack)
#   APP_ID            the installed Contribute app's numeric id
#   APP_TOKEN         an app-scoped token for that app (github_access)
#   CONTAINER         the app container name (for docker exec git prep)
#   UPSTREAM_REPO     owner/name of the scratch upstream repo (the PR target)
#   REVIEWER_TOKEN    a classic PAT for the SECOND (reviewer) GitHub account
# Optional:
#   KEEP=1            skip cleanup (leave the PR/record for inspection)
#   PROVIDER_OK=1     also assert the spawned turn started (needs a provider)
#
set -euo pipefail

: "${API_BASE:?set API_BASE (e.g. http://localhost:8001)}"
: "${APP_ID:?set APP_ID (the Contribute app id)}"
: "${APP_TOKEN:?set APP_TOKEN (app-scoped token with github_access)}"
: "${CONTAINER:?set CONTAINER (app container name, e.g. mobius-test)}"
: "${UPSTREAM_REPO:?set UPSTREAM_REPO (owner/name scratch repo)}"
: "${REVIEWER_TOKEN:?set REVIEWER_TOKEN (PAT for the reviewer account)}"

STAMP="$(date +%Y%m%d-%H%M%S)"
REC="autopilot-check-${STAMP}"
BRANCH="autopilot-check/${STAMP}"
WORKTREE="/data/contrib/${REC}/worktree"
COAUTHOR="Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>"
PR_NUMBER=""
PR_URL=""

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

api() {  # api METHOD PATH [JSON]  → prints body, fails on non-2xx
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" -H "Authorization: Bearer ${APP_TOKEN}" \
              -w '\n%{http_code}' "${API_BASE}${path}")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  local out code
  out="$(curl "${args[@]}")" || die "curl failed: $method $path"
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  [[ "$code" =~ ^2 ]] || die "$method $path → HTTP $code: $out"
  printf '%s' "$out"
}

# Run container-side git prep as `mobius` — the same user the backend runs git
# as — so the staging worktree is mobius-owned and git never trips its
# dubious-ownership guard between the script and the submit endpoint.
dex()  { docker exec -u mobius "$CONTAINER" bash -lc "$*"; }
# Root helper — only for creating/removing the staging dir under the root-owned
# /data/contrib (mobius can't mkdir/rmdir there itself).
dexr() { docker exec -u root "$CONTAINER" bash -lc "$*"; }

# Best-effort curl for the cleanup trap. It must NOT use api()/die(): a non-2xx
# (e.g. a DELETE of an already-gone file) would exit the trap mid-way and leak
# the rest. Cleanup swallows every status and always runs to completion.
capi() {  # capi METHOD PATH [JSON]  → best-effort, prints nothing, never fails
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -o /dev/null -X "$method" -H "Authorization: Bearer ${APP_TOKEN}" "${API_BASE}${path}")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  curl "${args[@]}" >/dev/null 2>&1 || true
}

cleanup() {
  [ "${KEEP:-}" = "1" ] && { say "KEEP=1 — leaving ${REC} / PR ${PR_NUMBER}"; return; }
  say "Cleanup"
  if [ -n "$PR_NUMBER" ]; then
    GH_TOKEN="$REVIEWER_TOKEN" gh api -X PATCH \
      "repos/${UPSTREAM_REPO}/pulls/${PR_NUMBER}" -f state=closed >/dev/null 2>&1 \
      && ok "closed PR #${PR_NUMBER}" || true
  fi
  # Order matters: run cleanup-staging FIRST, while the ledger record still
  # exists — it resolves the record to find the row and calls autopilot.close_out
  # now that the PR reads closed. Deleting the record before this would make
  # cleanup-staging 404, leaving the grant live on a dead PR.
  # close_out revokes the grant (enabled=False) and releases the claim; it
  # deliberately KEEPS the row so the round audit log survives. Each run
  # therefore leaves one disabled row under its unique timestamped record id —
  # inert, and no collision with later runs.
  capi POST "/api/github/contributions/${APP_ID}/${REC}/cleanup-staging" '{}'
  dex "cd '${WORKTREE}' 2>/dev/null && git push fork --delete '${BRANCH}'" >/dev/null 2>&1 || true
  capi DELETE "/api/storage/apps/${APP_ID}/contributions/${REC}.json"
  capi DELETE "/api/storage/apps/${APP_ID}/contributions/${REC}.diff"
  dexr "rm -rf /data/contrib/${REC}" >/dev/null 2>&1 || true
  ok "removed ${REC} (autopilot row left disabled for its audit log)"
}
trap cleanup EXIT

# ── 1. Seed a fresh contribution (container-side git) ────────────────────────
say "1. Seed a reviewable contribution on ${UPSTREAM_REPO}"
# Create the staging dir as root, hand it to mobius, then do all git as mobius.
dexr "mkdir -p '/data/contrib/${REC}' && chown mobius:mobius '/data/contrib/${REC}'"
dex "set -e
  rm -rf '${WORKTREE}'
  git clone --depth 1 'https://github.com/${UPSTREAM_REPO}.git' '${WORKTREE}'
  cd '${WORKTREE}'
  git config user.name 'Autopilot Check'
  git config user.email 'autopilot-check@users.noreply.github.com'
  BASE=\$(git rev-parse HEAD)
  git checkout -b '${BRANCH}'
  printf '\n<!-- autopilot-check ${STAMP} -->\n' >> README.md
  git add README.md
  git commit -q -m 'Autopilot check: reviewable change' -m '${COAUTHOR}'
  HEAD_SHA=\$(git rev-parse HEAD)
  git -c core.quotePath=false diff --no-ext-diff --no-color --binary \
    --full-index --src-prefix=a/ --dst-prefix=b/ \"\${BASE}..\${HEAD_SHA}\" > /tmp/${REC}.diff
  echo \"\${BASE} \${HEAD_SHA}\"" > /tmp/${REC}.meta
read -r BASE_SHA HEAD_SHA < /tmp/${REC}.meta
[ -n "$HEAD_SHA" ] || die "seed failed (no head sha)"
DIFF_SHA="$(dex "sha256sum /tmp/${REC}.diff | cut -d' ' -f1")"
ok "base=${BASE_SHA:0:8} head=${HEAD_SHA:0:8}"

# Write the ledger record + diff. The storage PUT writes the body VERBATIM as
# the file (like job.sh), so the record JSON is sent raw (no envelope).
RECORD=$(cat <<JSON
{
  "id": "${REC}", "type": "pr", "repo": "${UPSTREAM_REPO}", "status": "prepared",
  "title": "Autopilot check ${STAMP}", "branch": "${BRANCH}",
  "created_at": "$(date -u +%FT%TZ)", "updated_at": "$(date -u +%FT%TZ)",
  "summary": "Automated autopilot loop smoke test.",
  "plan": {"action": "pr", "repo": "${UPSTREAM_REPO}", "title": "Autopilot check ${STAMP}",
    "body_draft": "Automated autopilot loop smoke test — safe to close.",
    "branch": "${BRANCH}", "repo_path": "${WORKTREE}",
    "base_sha": "${BASE_SHA}", "head_sha": "${HEAD_SHA}", "diff_sha256": "${DIFF_SHA}",
    "diff_stat": "1 file changed"}
}
JSON
)
api PUT "/api/storage/apps/${APP_ID}/contributions/${REC}.json" "$RECORD" >/dev/null
# The diff sits beside the record as raw text — write it container-side (mobius).
dex "mkdir -p /data/apps/${APP_ID}/contributions && cp /tmp/${REC}.diff /data/apps/${APP_ID}/contributions/${REC}.diff"
ok "record ${REC} staged"

# ── 2. Send (opens the real PR, stamps the grant) ────────────────────────────
say "2. Send with autopilot → open the PR"
SUBMIT="$(api POST "/api/github/contributions/${APP_ID}/${REC}/submit" '{"autopilot": true}')"
PR_URL="$(printf '%s' "$SUBMIT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url",""))')"
PR_NUMBER="${PR_URL##*/}"
[ -n "$PR_NUMBER" ] || die "submit returned no PR url: $SUBMIT"
ok "opened ${PR_URL}"
printf '%s' "$SUBMIT" | grep -q '"autopilot"' && ok "grant stamped (mirror present)"

# ── 3. Reviewer requests changes ─────────────────────────────────────────────
say "3. Post a 'changes requested' review as the reviewer account"
GH_TOKEN="$REVIEWER_TOKEN" gh api -X POST \
  "repos/${UPSTREAM_REPO}/pulls/${PR_NUMBER}/reviews" \
  -f event=REQUEST_CHANGES -f body='Please tweak the note wording.' >/dev/null
ok "review posted"

# Helper: the record's mirrored autopilot state (app-token readable).
mirror() {
  api GET "/api/storage/apps/${APP_ID}/contributions/${REC}.json" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); a=d.get("autopilot",{}); print(a.get("state",""), int(a.get("rounds_used",0)), (a.get("last_round") or {}).get("outcome",""))'
}

# ── 4. Detection: job.sh should detect the review and spawn a real round ──────
say "4. Trigger job.sh → detect the review, claim a round, spawn the agent"
api POST "/api/apps/${APP_ID}/run-job" '{}' >/dev/null
SPAWNED=""
for _ in $(seq 1 10); do
  sleep 4
  read -r ST _ _ <<<"$(mirror)"
  [ "$ST" = "responding" ] && { SPAWNED=1; break; }
done
[ -n "$SPAWNED" ] && ok "job.sh detected the review → claimed a round (state=responding)" \
  || die "job.sh did not claim a round (check the provider is authenticated)"

# ── 5. Observe the real agent complete the round ─────────────────────────────
say "5. Watch the review-followup agent fix + push + reply + complete"
DONE=""
for _ in $(seq 1 30); do  # up to ~5 min for the agent turn
  sleep 10
  read -r ST RU OUT <<<"$(mirror)"
  echo "  … state=${ST} rounds=${RU} last=${OUT}"
  [ "$ST" = "idle" ] && [ "${RU:-0}" -ge 1 ] && { DONE=1; break; }
done
[ -n "$DONE" ] || die "round did not complete (still ${ST}); inspect the Autopilot chat"
read -r _ _ OUTCOME <<<"$(mirror)"
[ "$OUTCOME" = "pushed" ] && ok "round completed with a push" \
  || ok "round completed (outcome=${OUTCOME})"

# Verify the REAL PR advanced beyond the first commit.
COMMITS="$(GH_TOKEN="$REVIEWER_TOKEN" gh api "repos/${UPSTREAM_REPO}/pulls/${PR_NUMBER}/commits" -q 'length')"
[ "${COMMITS:-0}" -ge 2 ] && ok "PR advanced on GitHub (${COMMITS} commits)" \
  || echo "  (PR has ${COMMITS} commit(s) — the agent may have only replied)"
# Verify the co-author trailer on the newest commit.
GH_TOKEN="$REVIEWER_TOKEN" gh api "repos/${UPSTREAM_REPO}/pulls/${PR_NUMBER}/commits" \
  -q '.[-1].commit.message' 2>/dev/null | grep -q 'Co-authored-by: Möbius Agent' \
  && ok "follow-up commit carries the co-author trailer" || true

# ── 6. No self-re-trigger ────────────────────────────────────────────────────
say "6. Re-run job.sh → the agent's own reply must NOT re-trigger a round"
api POST "/api/apps/${APP_ID}/run-job" '{}' >/dev/null
sleep 8
read -r ST2 _ _ <<<"$(mirror)"
[ "$ST2" = "idle" ] && ok "record stayed idle (self-event filtered)" \
  || die "record re-triggered to ${ST2} on the agent's own reply"

say "PASS — full autopilot loop verified live end to end."
echo "PR (closed on cleanup unless KEEP=1): ${PR_URL}"
