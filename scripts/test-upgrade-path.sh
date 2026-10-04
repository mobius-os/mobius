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
# 3. Stand in for a current self-hosted host helper. This checks the old
#    updater and the new image's boot against the helper's contract; the
#    helper's replacement loop (scripts/mobius-rebuild-host.py run) is not
#    exercised.
# 4. Press Update through the same HTTP calls Settings makes. Any refusal
#    fails the check: that is how a release strands existing instances. The
#    one exception is a release that advances
#    deployment/self-hosted-helper.required: older releases must refuse it
#    with external_activation_required, and the documented host path must
#    then finish it. Like an owner, reinstall the helper from the candidate's
#    checkout (its real worker seeds and verifies the ACTIVE state the
#    installer mounts read-only), and let install-rebuild-helper.sh's bridge
#    (scripts/finish-helper-update.py) queue the update through the old
#    release's own updater. The candidate then boots with that mount and
#    must convert a fixture chat exactly.
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
host_state="${name}-host"
start_args=()
work=$(mktemp -d)
record=/data/.platform-prepared-update.json

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" "$host_state" >/dev/null 2>&1 || true
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
    "${start_args[@]}" \
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

previous=$(image_sha "$PREVIOUS")
candidate=$(image_sha "$CANDIDATE")
helper_required() {  # <sha>: the helper revision that release requires (0 = none)
  git -C "$REPO" show "$1:deployment/self-hosted-helper.required" 2>/dev/null \
    | tr -d '[:space:]' || true
}
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

# A chat with duplicate and missing ids, Unicode and typed values, stored as
# the previous release stores it. Storage conversions must keep it exact.
fixture='[{"id":"m1","role":"user","content":"h\u00e9llo \ud83d\ude00","ts":1},{"id":"m1","role":"assistant","content":"same id","blocks":[{"type":"text","content":"x"}],"n":1.0,"flag":true},{"role":"assistant","content":"no id","values":[0,-0.0,2.5,null]}]'
docker exec -i -u mobius -w /data/platform/backend "$name" python3 - "$fixture" <<'PY' \
  || fail "could not store the fixture chat"
import json, sys
sys.path.insert(0, ".")
from app import models
from app.database import SessionLocal
db = SessionLocal()
db.add(models.Chat(id="upgrade-path-fixture", title="Upgrade fixture",
                   messages=json.loads(sys.argv[1]), has_messages=True))
db.commit()
db.close()
PY
reply=$(api GET "/api/chats/upgrade-path-fixture?limit=50")
[ "$(code "$reply")" = 200 ] && [ "$(field "$(body "$reply")" 'd.get("total")')" = 3 ] \
  || fail "the previous release does not serve the fixture chat: $(body "$reply")"

echo "3. a current host helper is installed"
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
if [ "$local_edits" = true ] && [ "$needs_image" = True ]; then
  [ "$(field "$preview" '"Dockerfile" in (d.get("local_image_paths") or [])')" = True ] \
    || fail "the review does not report the local Dockerfile customization: $preview"
fi

host_migration=false
if [ "$(helper_required "$candidate")" != "$(helper_required "$previous")" ]; then
  host_migration=true
fi

if [ "$needs_image" = True ]; then
  reply=$(api POST /api/platform/rebuild "$plan")
  if [ "$host_migration" = true ]; then
    [ "$(code "$reply")" = 409 ] \
      && [ "$(field "$(body "$reply")" '(d.get("detail") or {}).get("code")')" = external_activation_required ] \
      || fail "Settings must refuse a release that needs a newer host helper ($(code "$reply")): $(body "$reply")"
    echo "   Settings refuses it: the release needs a newer host helper"
    echo "3b. the owner reinstalls the helper from the candidate checkout"
    # The installer's own steps for the host state: a private root directory,
    # then the checkout's worker seeds and must verify as ACTIVE.
    docker volume create "$host_state" >/dev/null
    host_worker() {
      docker run --rm --network none --user 0 --entrypoint python3 \
        -v "$host_state:/var/lib/mobius-rebuild" "$CANDIDATE" \
        -I -S /app/platform-baked/scripts/mobius-rebuild-host.py "$1"
    }
    docker run --rm --network none --user 0 --entrypoint chmod \
      -v "$host_state:/var/lib/mobius-rebuild" "$CANDIDATE" 0700 /var/lib/mobius-rebuild \
      || fail "could not prepare the helper state"
    host_worker adopt-self || fail "the candidate worker could not be seeded"
    host_worker verify-active >/dev/null || fail "the seeded worker is not a verified floor-aware ACTIVE worker"
    # The installer's last step: start the waiting update through the old
    # release's own reviewed updater.
    finish=$(git -C "$REPO" show "$candidate:scripts/finish-helper-update.py" \
      | docker exec -i -u mobius -w /data/platform/backend "$name" \
          python3 -I - "$candidate" "$(helper_required "$candidate")") \
      || fail "the installer could not start the waiting update: $finish"
    [ "$(field "$(tail -n1 <<<"$finish")" 'd.get("state")')" = queued ] \
      || fail "the installer did not queue the waiting update: $finish"
    # Like the installed override, the replacement mounts the Host state
    # read-only and declares that Host recovery is required.
    start_args=(-e MOBIUS_HOST_RECOVERY_REQUIRED=1
      --mount "type=volume,src=$host_state,dst=/run/mobius-rebuild-host,readonly")
  else
    [ "$(code "$reply")" = 202 ] && [ "$(field "$(body "$reply")" 'd.get("state")')" = queued ] \
      || fail "the previous release refused to install the candidate ($(code "$reply")): $(body "$reply")"
  fi
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
  applied_state=$([ "$(code "$reply")" = 200 ] && field "$(body "$reply")" 'd.get("state")')
  case "$applied_state" in
    updated|up_to_date|restart_needed) ;;
    *) fail "the previous release refused to apply the candidate ($(code "$reply")): $(body "$reply")" ;;
  esac
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
# The fixture chat survives exactly, however this release stores it.
reply=$(api GET "/api/chats/upgrade-path-fixture?limit=50")
[ "$(code "$reply")" = 200 ] || fail "the fixture chat is not readable after the update: $(body "$reply")"
python3 - "$fixture" "$(body "$reply")" <<'PY' || fail "the fixture chat changed in the update"
import json, sys
expected, chat = json.loads(sys.argv[1]), json.loads(sys.argv[2])
actual = chat.get("messages") or []
canon = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)
picked = [{k: m.get(k) for k in e} for m, e in zip(actual, expected)]
assert len(actual) == len(expected), (len(actual), len(expected))
assert [canon(m) for m in picked] == [canon(m) for m in expected], picked
PY
if [ "$host_migration" = true ]; then
  docker exec "$name" python3 - <<'PY' || fail "the candidate did not convert storage exactly"
import json, sqlite3
conn = sqlite3.connect("file:/data/db/upgrade-path.db?mode=ro", uri=True)
assert conn.execute("SELECT floor FROM platform_compat").fetchone()[0] >= 1
rows = conn.execute("SELECT body FROM chat_messages WHERE chat_id = 'upgrade-path-fixture' ORDER BY seq").fetchall()
assert len(rows) == 3, rows
PY
  echo "   storage converted with the floor-aware helper ACTIVE"
fi
echo "upgrade path: ${previous:0:12} installs ${candidate:0:12}"
