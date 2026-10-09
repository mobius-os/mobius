#!/usr/bin/env bash
# Real-Docker upgrade-path check: can the PREVIOUS published release install
# this candidate through the in-product update, exactly as its owner would?
#
#   scripts/test-upgrade-path.sh <previous-image> <candidate-image> [repo]
#
# Both images must be official builds whose /app/build-info.json names their
# commit; <repo> (default .) must contain both commits so the candidate can be
# handed to the old instance as a git bundle.
#
# 1. Boot the previous image on a fresh volume (production path, like an owner).
# 2. Offer it the candidate release and create its owner through the setup API.
#    Like an agent customizing its instance, commit local edits to image-owned
#    files (Dockerfile, Python package list). They must never block the update,
#    and must still be in the source afterwards.
# 3. Stand in for an available self-hosted host helper. This checks the old
#    updater and the new image's boot against the helper's contract; the
#    helper's own code (scripts/mobius-rebuild-host.py) is not exercised.
# 4. Press Update through the same HTTP calls Settings makes. Any refusal
#    fails the check: that is how a release strands existing instances. A helper
#    migration may install source with an explicit manual maintenance handoff;
#    that obligation must survive restart, not disappear because the app is healthy.
# 5. Image updates: take the queued request as the helper would, recreate the
#    container on the candidate image with the same volume, report success,
#    and require the candidate to serve its own tree and settle the update.
#    Source-only updates: Apply, restart, and require the candidate tree.

set -euo pipefail

PREVIOUS="${1:?previous image}"
CANDIDATE="${2:?candidate image}"
REPO="${3:-.}"
name="mobius-upgrade-path-$$"
volume="${name}-data"
work=$(mktemp -d)
record=/data/.platform-prepared-update.json

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "upgrade path: $*" >&2
  docker logs "$name" --tail 80 >&2 2>&1 || true
  exit 1
}

as_mobius() { docker exec -u mobius "$name" "$@"; }

image_sha() {
  docker run --rm --entrypoint cat "$1" /app/build-info.json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["sha"])'
}

start() {  # <image>
  docker run -d --name "$name" --init --restart no \
    -v "$volume:/data" \
    -e "SECRET_KEY=upgrade-path-regression-key-0123456789" \
    -e "DATABASE_URL=sqlite:////data/db/upgrade-path.db" \
    -e "DATA_DIR=/data" \
    -e "DOMAIN=localhost" \
    -e "FRONTEND_ORIGIN=http://localhost" \
    -e "MOEBIUS_SKIP_BOOTSTRAP=1" \
    "$1" >/dev/null
  for _ in $(seq 1 240); do
    if docker exec "$name" curl -fsS -o /dev/null http://127.0.0.1:8000/api/health 2>/dev/null; then
      return 0
    fi
    [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ] \
      || fail "the $1 container stopped while booting"
    sleep 1
  done
  fail "the $1 container did not serve /api/health within 240s"
}

# api <method> <path> [json]: prints the body, then the HTTP status on its own line.
api() {
  local args=(-sS -X "$1" -w '\n%{http_code}' -H 'Content-Type: application/json')
  [ -n "${token:-}" ] && args+=(-H "Authorization: Bearer $token")
  [ -n "${3:-}" ] && args+=(-d "$3")
  docker exec "$name" curl "${args[@]}" "http://127.0.0.1:8000$2"
}
body() { sed '$d' <<<"$1"; }
code() { tail -n1 <<<"$1"; }
field() {  # <json> <python expression over d>; JSON on stdin (a preview can exceed ARG_MAX)
  python3 -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1]))' "$2" <<<"$1"
}

# A helper migration is a successful source install plus an explicit operator
# handoff, not proof of a completed host upgrade. Other external work is not
# covered by this fixture and must remain a failing, actionable result.
helper_maintenance_pending() {
  [ "$(field "$1" 'd.get("activation", {}).get("required_actions") == ["host_maintenance"] and len(d.get("activation", {}).get("reasons", [])) == 1 and any(r.get("code") == "host_helper_migration" and r.get("paths") == ["deployment/self-hosted-helper.required"] for r in d.get("activation", {}).get("reasons", []))')" = True ]
}

# Before activation, the same migration may also need an ordinary server
# restart. That restart must disappear after boot; only the exact helper work
# may remain. Do not treat proxy, topology, or image work as this handoff.
reviewed_helper_preview() {
  [ "$(field "$1" 'set((d.get("activation") or {}).get("required_actions") or []) in ({"host_maintenance"}, {"server_restart", "host_maintenance"}) and any(r.get("code") == "host_helper_migration" and r.get("paths") == ["deployment/self-hosted-helper.required"] for r in (d.get("activation") or {}).get("reasons", []))')" = True ]
}

previous=$(image_sha "$PREVIOUS")
candidate=$(image_sha "$CANDIDATE")
[[ "$previous" =~ ^[0-9a-f]{40}$ && "$candidate" =~ ^[0-9a-f]{40}$ ]] \
  || fail "both images must record their commit in /app/build-info.json"
[ "$previous" != "$candidate" ] || fail "the previous and candidate images are the same release"
echo "upgrade path: ${previous:0:12} -> ${candidate:0:12}"

# The image seeds a shallow checkout, so the candidate travels with its whole
# history: a bundle that excluded the previous release would need every commit
# the candidate branched from (a merge of an older branch does), which the seed
# lacks. A bundle carries refs, not bare commits, so name the candidate.
bundle_ref=refs/upgrade-path/candidate-$$
git -C "$REPO" update-ref "$bundle_ref" "$candidate" \
  || fail "the repository does not contain the candidate ${candidate:0:12}"
git -C "$REPO" merge-base --is-ancestor "$previous" "$candidate" \
  || fail "the candidate ${candidate:0:12} does not descend from ${previous:0:12}"
bundled=0
git -C "$REPO" bundle create "$work/candidate.bundle" "$bundle_ref" \
  >/dev/null 2>"$work/bundle.err" && bundled=1
git -C "$REPO" update-ref -d "$bundle_ref"
[ "$bundled" = 1 ] || fail "could not bundle ${candidate:0:12} on ${previous:0:12}: $(cat "$work/bundle.err")"

echo "1. the previous release boots like an owner's instance"
docker volume create "$volume" >/dev/null
start "$PREVIOUS"

echo "2. the candidate release is offered and the owner signs in"
docker cp "$work/candidate.bundle" "$name:/tmp/candidate.bundle"
docker exec "$name" chmod 0644 /tmp/candidate.bundle
as_mobius git -C /data/platform fetch -q /tmp/candidate.bundle \
  "$bundle_ref:refs/remotes/origin/main" || fail "the previous release could not fetch the candidate"
reply=$(api POST /api/auth/setup '{"username":"owner","password":"upgrade-path-owner-password"}')
[ "$(code "$reply")" = 200 ] || fail "owner setup failed: $(body "$reply")"
token=$(field "$(body "$reply")" 'd["access_token"]')
# Releases before #1561 refused such edits by design; test the promise from
# the first release that makes it.
local_edits=false
if as_mobius grep -q "def local_image_changes" /data/platform/backend/app/platform_update.py; then
  local_edits=true
  as_mobius sh -c 'cd /data/platform &&
    printf "\n# upgrade-path: local image customization\n" >> Dockerfile &&
    printf "# upgrade-path: local package note\n" >> backend/requirements.txt &&
    git -c user.name=upgrade-path -c user.email=upgrade-path@localhost \
      commit -q -m "Local image-owned customization" -- Dockerfile backend/requirements.txt' \
    || fail "could not commit the local image-owned customization"
fi

echo "3. an available host helper supports the replacement handshake"
docker exec "$name" sh -c '
  set -e
  mkdir -p /data/mobius-rebuild/inbox
  chown mobius:mobius /data/mobius-rebuild/inbox
  chmod 0700 /data/mobius-rebuild/inbox
  printf "%s\n" "{\"supported\":true,\"state\":\"idle\",\"handoff\":\"external-cutover-v1\",\"request_versions\":[1,2]}" \
    > /data/mobius-rebuild/status.json
  chmod 0644 /data/mobius-rebuild/status.json'

echo "4. the owner presses Update"
reply=$(api GET /api/platform/update-preview)
[ "$(code "$reply")" = 200 ] || fail "the update preview failed: $(body "$reply")"
preview=$(body "$reply")
[ "$(field "$preview" 'd.get("target_sha")')" = "$candidate" ] \
  || fail "the previous release does not offer the candidate: $preview"
conflicts=$(field "$preview" 'len(d.get("conflict_paths") or []) + len(d.get("blocking_paths") or [])')
[ "$conflicts" = 0 ] || fail "a fresh instance reports update conflicts or blockers: $preview"
[ "$(field "$preview" 'd.get("actionable", True)')" = True ] \
  || fail "the previous release does not offer the candidate as an actionable update: $preview"
plan=$(field "$preview" 'json.dumps({k: d.get(k) for k in ("plan_id", "current_sha", "target_sha", "image_digest")})')
needs_image=$(field "$preview" '"image_rebuild" in ((d.get("activation") or {}).get("required_actions") or [])')
needs_helper=false
if reviewed_helper_preview "$preview"; then
  needs_helper=true
fi
if [ "$(field "$preview" '"host_maintenance" in ((d.get("activation") or {}).get("required_actions") or [])')" = True ] \
    && [ "$needs_helper" != true ]; then
  fail "the review includes host work outside the supported helper-only handoff: $preview"
fi
if [ "$local_edits" = true ] && [ "$needs_image" = True ]; then
  [ "$(field "$preview" '"Dockerfile" in (d.get("local_image_paths") or [])')" = True ] \
    || fail "the review does not report the local Dockerfile customization: $preview"
fi

if [ "$needs_image" = True ]; then
  reply=$(api POST /api/platform/rebuild "$plan")
  [ "$(code "$reply")" = 202 ] && [ "$(field "$(body "$reply")" 'd.get("state")')" = queued ] \
    || fail "the previous release refused to install the candidate ($(code "$reply")): $(body "$reply")"
  request=$(docker exec "$name" cat /data/mobius-rebuild/inbox/request.json) \
    || fail "Update did not queue a container replacement: $(body "$reply")"
  [ "$(field "$request" 'd.get("expected_sha")')" = "$candidate" ] \
    || fail "the queued replacement names another release: $request"
  nonce=$(field "$request" 'd.get("nonce") or ""')

  echo "5. the helper replaces the container with the candidate image"
  # The same sequence as scripts/mobius-rebuild-host.py run(): claim, drain
  # through the root ledger (an old updater swaps its update here), recreate,
  # verify, finalize the chat handoff, report success.
  operation=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  helper_status() {  # <state> <message>: what the helper mirrors into /data
    printf '{"supported":true,"operation_id":"%s","state":"%s","expected_sha":"%s","request_nonce":"%s","code":null,"message":"%s","handoff":"external-cutover-v1","request_versions":[1,2],"updated_at":"%s"}\n' \
      "$operation" "$1" "$candidate" "$nonce" "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  }
  write_status() { helper_status "$@" | docker exec -i "$name" sh -c 'cat > /data/mobius-rebuild/status.json'; }
  ledger() { docker exec "$name" python3 -P /app/runtime/restart_ledger.py "$1" "$operation"; }
  docker exec "$name" rm -f /data/mobius-rebuild/inbox/request.json
  write_status preparing "Downloading and checking the official image."
  ledger open-cutover || fail "the previous release does not support a safe cutover"
  docker exec "$name" python3 /data/platform/backend/scripts/prepare-container-cutover.py "$operation" \
    || fail "the previous release could not drain for the cutover"
  ledger accept-cutover || fail "the previous release's supervisor did not accept the handoff"
  write_status replacing "Rebuilding the container."
  docker stop -t 60 "$name" >/dev/null
  docker rm "$name" >/dev/null
  helper_status verifying "Checking the new container." \
    | docker run --rm -i --entrypoint sh -v "$volume:/data" "$CANDIDATE" \
      -c 'cat > /data/mobius-rebuild/status.json'
  start "$CANDIDATE"
  [ "$(docker exec "$name" curl -fsS http://127.0.0.1:8000/api/version \
      | python3 -c 'import json, sys; print(json.load(sys.stdin).get("sha"))')" = "$candidate" ] \
    || fail "the new container does not report the candidate revision"
  ledger finalize-cutover || fail "the candidate could not finalize the chat handoff"
  write_status succeeded "Container rebuilt successfully."
else
  reply=$(api POST /api/platform/apply "$plan")
  # Every applied outcome is fine (updated, up_to_date for live-only changes,
  # restart_needed); conflict, rolled_back or an error is a refusal.
  [ "$(code "$reply")" = 200 ] \
    || fail "the previous release refused to apply the candidate ($(code "$reply")): $(body "$reply")"
  applied_state=$(field "$(body "$reply")" 'd.get("state")') \
    || fail "the previous release returned an invalid apply response ($(code "$reply")): $(body "$reply")"
  if [ "$needs_helper" = true ]; then
    [ "$applied_state" = activation_needed ] && reviewed_helper_preview "$(body "$reply")" \
      || fail "the installed candidate lost its reviewed helper-maintenance handoff: $(body "$reply")"
  else
    case "$applied_state" in
      updated|up_to_date|restart_needed) ;;
      *) fail "the previous release refused to apply the candidate ($(code "$reply")): $(body "$reply")" ;;
    esac
  fi
  echo "5. the owner restarts to load the candidate"
  docker restart "$name" >/dev/null
  for _ in $(seq 1 240); do
    docker exec "$name" curl -fsS -o /dev/null http://127.0.0.1:8000/api/health 2>/dev/null && break
    sleep 1
  done
fi

[ "$(docker exec "$name" cat /tmp/serving-source)" = platform ] \
  || fail "the updated instance does not serve its platform checkout"
as_mobius git -C /data/platform merge-base --is-ancestor "$candidate" HEAD \
  || fail "the served checkout does not contain the candidate"
if [ "$local_edits" = true ]; then
  as_mobius grep -q "upgrade-path: local image customization" /data/platform/Dockerfile \
    && as_mobius grep -q "upgrade-path: local package note" /data/platform/backend/requirements.txt \
    || fail "the update dropped the local image-owned customization"
fi
if [ "$needs_image" = True ]; then
  for _ in $(seq 1 60); do  # reading the status confirms the exact replacement
    api GET /api/admin/rebuild >/dev/null
    as_mobius test -e "$record" || break
    sleep 2
  done
  as_mobius test ! -e "$record" || fail "the update never settled: $(as_mobius cat "$record")"
fi
if [ "$needs_helper" = true ]; then
  reply=$(api GET /api/platform/status)
  [ "$(code "$reply")" = 200 ] && helper_maintenance_pending "$(body "$reply")" \
    || fail "a healthy restart incorrectly discharged host maintenance: $(body "$reply")"
  echo "upgrade path: candidate source installed; exact helper migration remains an operator handoff"
fi
echo "upgrade path: ${previous:0:12} installs ${candidate:0:12}"
