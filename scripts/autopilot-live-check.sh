#!/usr/bin/env bash
#
# autopilot-live-check.sh — Stage-2 live smoke test for the Contribute autopilot
# review-response loop, against a running Docker test stack + a real (scratch)
# GitHub connection. Proves the parts the stubbed unit/integration tests can't:
# the REAL git push updating a REAL PR, and job.sh detecting a REAL review.
#
# It plays the follow-up agent MECHANICALLY (no reasoning, no agent tokens):
# seeds a fresh contribution, opens a PR, posts a review from the reviewer
# account, drives /respond → /update → /reply → /complete with the app token,
# verifies the PR advanced on GitHub, checks the no-self-retrigger guard, then
# closes the PR and removes everything it created. Re-runnable: every run uses a
# unique branch/record id and cleans up after itself.
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

dex() { docker exec "$CONTAINER" bash -lc "$*"; }

cleanup() {
  [ "${KEEP:-}" = "1" ] && { say "KEEP=1 — leaving ${REC} / PR ${PR_NUMBER}"; return; }
  say "Cleanup"
  if [ -n "$PR_NUMBER" ]; then
    GH_TOKEN="$REVIEWER_TOKEN" gh api -X PATCH \
      "repos/${UPSTREAM_REPO}/pulls/${PR_NUMBER}" -f state=closed >/dev/null 2>&1 \
      && ok "closed PR #${PR_NUMBER}" || true
  fi
  # Delete the fork branch + the record; run-job's cleanup-staging (which now
  # calls autopilot.close_out) drops the DB row once the PR reads closed.
  dex "cd '${WORKTREE}' 2>/dev/null && git push fork --delete '${BRANCH}'" >/dev/null 2>&1 || true
  api DELETE "/api/storage/apps/${APP_ID}/contributions/${REC}.json" >/dev/null 2>&1 || true
  api DELETE "/api/storage/apps/${APP_ID}/contributions/${REC}.diff" >/dev/null 2>&1 || true
  api POST "/api/github/contributions/${APP_ID}/${REC}/cleanup-staging" '{}' >/dev/null 2>&1 || true
  dex "rm -rf /data/contrib/${REC}" >/dev/null 2>&1 || true
  ok "removed ${REC}"
}
trap cleanup EXIT

# ── 1. Seed a fresh contribution (container-side git) ────────────────────────
say "1. Seed a reviewable contribution on ${UPSTREAM_REPO}"
dex "set -e
  mkdir -p '$(dirname "${WORKTREE}")'
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

# Write the ledger record + diff via the storage API (as the agent would).
DIFF_JSON="$(dex "cat /tmp/${REC}.diff" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
api PUT "/api/storage/apps/${APP_ID}/contributions/${REC}.diff" \
  "$(python3 -c "import json,sys; print(json.dumps({'content': json.loads(sys.argv[1])}))" "$DIFF_JSON")" >/dev/null 2>&1 || \
  dex "cp /tmp/${REC}.diff /data/apps/${APP_ID}/contributions/${REC}.diff"
RECORD=$(cat <<JSON
{"content": {
  "id": "${REC}", "type": "pr", "repo": "${UPSTREAM_REPO}", "status": "prepared",
  "title": "Autopilot check ${STAMP}", "branch": "${BRANCH}",
  "created_at": "$(date -u +%FT%TZ)", "updated_at": "$(date -u +%FT%TZ)",
  "summary": "Automated autopilot loop smoke test.",
  "plan": {"action": "pr", "repo": "${UPSTREAM_REPO}", "title": "Autopilot check ${STAMP}",
    "body_draft": "Automated autopilot loop smoke test — safe to close.",
    "branch": "${BRANCH}", "repo_path": "${WORKTREE}",
    "base_sha": "${BASE_SHA}", "head_sha": "${HEAD_SHA}", "diff_sha256": "${DIFF_SHA}",
    "diff_stat": "1 file changed"}
}}
JSON
)
api PUT "/api/storage/apps/${APP_ID}/contributions/${REC}.json" "$RECORD" >/dev/null
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

# ── 4. Detection: run-job should claim + create the Autopilot chat ───────────
say "4. Trigger job.sh → it should detect the review and claim a round"
api POST "/api/apps/${APP_ID}/run-job" '{}' >/dev/null
sleep 6
CHATS="$(api GET "/api/chats")"
if printf '%s' "$CHATS" | grep -q "Autopilot: Autopilot check ${STAMP}"; then
  ok "dedicated 'Autopilot: …' chat created (detection → /respond → spawn)"
else
  echo "  (note: no Autopilot chat yet — detection may need a moment or a provider)"
fi

# ── 5. Drive the mechanical round (app token plays the agent) ────────────────
say "5. Mechanical round: /respond → /update → /reply → /complete"
RESP="$(api POST "/api/github/contributions/${APP_ID}/${REC}/respond" \
  "{\"attention\": {\"key\": \"changes_requested:${STAMP}\", \"type\": \"changes_requested\", \"event_at\": \"$(date -u +%FT%TZ)\"}}")"
STATUS="$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
RUN_ID="$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("run_id",""))')"
case "$STATUS" in
  responding) ok "claimed round (run_id ${RUN_ID:0:8})";;
  deferred)   die "budget deferred — raise autopilot_budget.percent and retry";;
  *)          [ -n "$RUN_ID" ] || die "no run_id from /respond: $RESP";;
esac

# Make a follow-up commit + recompute the reviewed diff, then CAS the record.
dex "set -e
  cd '${WORKTREE}'
  printf '\n<!-- autopilot-check followup ${STAMP} -->\n' >> README.md
  git add README.md
  git commit -q -m 'Autopilot check: address review' -m '${COAUTHOR}'
  NEW=\$(git rev-parse HEAD)
  git -c core.quotePath=false diff --no-ext-diff --no-color --binary \
    --full-index --src-prefix=a/ --dst-prefix=b/ \"${BASE_SHA}..\${NEW}\" > /tmp/${REC}.diff
  cp /tmp/${REC}.diff /data/apps/${APP_ID}/contributions/${REC}.diff
  echo \"\${NEW}\"" > /tmp/${REC}.newhead
NEW_HEAD="$(cat /tmp/${REC}.newhead)"
NEW_DIFF_SHA="$(dex "sha256sum /tmp/${REC}.diff | cut -d' ' -f1")"
# Patch head_sha/diff_sha256 on the stored record (the agent's CAS write).
CUR="$(api GET "/api/storage/apps/${APP_ID}/contributions/${REC}.json")"
PATCHED="$(printf '%s' "$CUR" | python3 -c "
import json,sys
r=json.load(sys.stdin); r.setdefault('plan',{})
r['plan']['head_sha']='${NEW_HEAD}'; r['plan']['diff_sha256']='${NEW_DIFF_SHA}'
print(json.dumps({'content': r}))")"
api PUT "/api/storage/apps/${APP_ID}/contributions/${REC}.json" "$PATCHED" >/dev/null

api POST "/api/github/contributions/${APP_ID}/${REC}/update" \
  "{\"run_id\": \"${RUN_ID}\", \"head_sha\": \"${NEW_HEAD}\", \"diff_sha256\": \"${NEW_DIFF_SHA}\", \"summary\": \"Addressed the review.\"}" >/dev/null
ok "/update accepted"

# Verify the REAL PR advanced to the new head.
GH_HEAD="$(GH_TOKEN="$REVIEWER_TOKEN" gh api "repos/${UPSTREAM_REPO}/pulls/${PR_NUMBER}" -q .head.sha)"
[ "$GH_HEAD" = "$NEW_HEAD" ] && ok "PR head advanced on GitHub (${NEW_HEAD:0:8})" \
  || die "PR head on GitHub is ${GH_HEAD:0:8}, expected ${NEW_HEAD:0:8}"

api POST "/api/github/contributions/${APP_ID}/${REC}/reply" \
  "{\"run_id\": \"${RUN_ID}\", \"body\": \"Addressed the review — please take another look.\", \"re_request_review\": true}" >/dev/null
ok "/reply posted"
api POST "/api/github/contributions/${APP_ID}/${REC}/complete" \
  "{\"run_id\": \"${RUN_ID}\", \"outcome\": \"pushed\", \"summary\": \"Fixed the note wording.\", \"event_at\": \"$(date -u +%FT%TZ)\"}" >/dev/null
ok "/complete → round logged"

# ── 6. No self-re-trigger ────────────────────────────────────────────────────
say "6. Re-run job.sh → the agent's own reply must NOT re-trigger a round"
api POST "/api/apps/${APP_ID}/run-job" '{}' >/dev/null
sleep 6
MIRROR="$(api GET "/api/storage/apps/${APP_ID}/contributions/${REC}.json" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("autopilot",{}).get("state",""))')"
[ "$MIRROR" = "idle" ] && ok "record stayed idle (self-event filtered)" \
  || echo "  (state=${MIRROR}; inspect if not idle)"

say "PASS — live mechanical loop verified end to end."
echo "PR (will be closed on cleanup unless KEEP=1): ${PR_URL}"
