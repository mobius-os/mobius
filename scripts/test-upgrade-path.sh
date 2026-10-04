#!/usr/bin/env bash
# Real-Docker upgrade-path check: can the PREVIOUS published release install
# this candidate through the in-product update, exactly as its owner would?
#
#   scripts/test-upgrade-path.sh <previous-image> <candidate-image> [repo]
#
# Both images must be provenance-bound builds whose /app/build-info.json names
# their baked commit; <repo> (default .) must contain both commits so the
# candidate can be handed to the old instance as a git bundle. Normal CI uses
# official images; the dependency fixture uses two disposable local-overlay
# images, never pretending that their SHAs were published releases.
#
# 1. Boot the previous image on a fresh volume (production path, like an owner).
# 2. Offer it the candidate release and create its owner through the setup API.
#    Like an agent customizing its instance, commit local edits to image-owned
#    files (Dockerfile, Python package list). They must never block the update,
#    and must still be in the source afterwards.
# 3. Stand in for a current self-hosted host helper. This checks the old
#    updater and the new image's boot against the helper's contract; the
#    helper's own code (scripts/mobius-rebuild-host.py) is not exercised.
# 4. Press Update through the same HTTP calls Settings makes. Any refusal
#    fails the check: that is how a release strands existing instances.
# 5. Image updates: take the queued request as the helper would, recreate the
#    container on the candidate image with the same volume, report success,
#    and require the candidate to serve its own tree and settle the update.
#    Source-only updates: Apply, restart, and require the candidate tree.

set -euo pipefail

PREVIOUS="${1:?previous image}"
CANDIDATE="${2:?candidate image}"
REPO="${3:-.}"
# Optional dependency fixture assertions. The fixture must be a real image
# built from a committed source pair, not an overlay installed into a container.
# All three values are required together so an accidental partial fixture
# cannot pass as a Python/lock upgrade.
before_python=${UPGRADE_PYTHON_BEFORE:-}
after_python=${UPGRADE_PYTHON_AFTER:-}
lock_package=${UPGRADE_LOCK_PACKAGE:-}
force_rollback=${UPGRADE_FORCE_ROLLBACK:-0}
[[ $force_rollback == 0 || $force_rollback == 1 ]] \
  || { echo "upgrade path: UPGRADE_FORCE_ROLLBACK must be 0 or 1" >&2; exit 2; }
[[ $force_rollback == 0 || -n $lock_package ]] \
  || { echo "upgrade path: forced rollback requires the dependency fixture" >&2; exit 2; }
if [[ -n $before_python || -n $after_python || -n $lock_package ]]; then
  [[ $before_python =~ ^3\.12\.[0-9]+$ && $after_python =~ ^3\.12\.[0-9]+$ \
    && $before_python != "$after_python" \
    && $lock_package =~ ^[a-z][a-z0-9-]*==[0-9][a-zA-Z0-9.!+_-]*$ ]] \
    || { echo "upgrade path: supply distinct Python 3.12 versions and a pinned lock package" >&2; exit 2; }
fi
name="mobius-upgrade-path-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
volume="${name}-data"
work=$(mktemp -d)
record=/data/.platform-prepared-update.json
container_owned=false
volume_owned=false
bundle_ref=""
bundle_owned=false

cleanup() {
  [ "$bundle_owned" = true ] && git -C "$REPO" update-ref -d "$bundle_ref" >/dev/null 2>&1 || true
  [ "$container_owned" = true ] && docker rm -f "$name" >/dev/null 2>&1 || true
  [ "$volume_owned" = true ] && docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  echo "upgrade path: $*" >&2
  docker logs "$name" --tail 80 >&2 2>&1 || true
  exit 1
}

as_mobius() { docker exec -u mobius "$name" "$@"; }

python_version() {
  docker exec "$name" python3 -c 'import platform; print(platform.python_version())'
}

package_version() {  # <distribution name>: actual installed metadata
  docker exec "$name" python3 -c \
    'import importlib.metadata as m, sys; print(m.version(sys.argv[1]))' "$1"
}

verify_fixture_service() {  # <expected image SHA> <expected served checkout SHA>
  local ready version
  ready=$(api GET /api/ready)
  [[ $(code "$ready") == 200 && $(field "$(body "$ready")" 'd.get("ready")') == True ]] \
    || fail "dependency fixture is reachable but not ready: $(body "$ready")"
  version=$(api GET /api/version)
  [[ $(code "$version") == 200 \
     && $(field "$(body "$version")" 'd.get("sha")') == "$1" \
     && $(field "$(body "$version")" 'd.get("serving_source")') == platform \
     && $(field "$(body "$version")" 'd.get("served_sha")') == "$2" ]] \
    || fail "dependency fixture does not serve the expected source/image pair: $(body "$version")"
}

image_sha() {
  docker run --rm --entrypoint cat "$1" /app/build-info.json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["sha"])'
}

verify_image_source() {  # <image> <sha>
  [[ $(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1") == "$2" ]] \
    || fail "image label does not pair $1 with $2"
  [[ $(docker run --rm --entrypoint git "$1" -C /app/platform-baked rev-parse HEAD) == "$2" ]] \
    || fail "baked source does not pair $1 with $2"
}

launch() {  # <image>: fixed disposable environment, no health expectation
  # This name was checked absent before the first launch; later launches reuse
  # only our own removed container. Clean up even if Docker creates then fails.
  container_owned=true
  docker run -d --name "$name" --init --restart no \
    -v "$volume:/data" \
    -e "SECRET_KEY=upgrade-path-regression-key-0123456789" \
    -e "DATABASE_URL=sqlite:////data/db/upgrade-path.db" \
    -e "DATA_DIR=/data" \
    -e "DOMAIN=localhost" \
    -e "FRONTEND_ORIGIN=http://localhost" \
    -e "MOEBIUS_SKIP_BOOTSTRAP=1" \
    "$1" >/dev/null
}

start() {  # <image>
  local probe=/api/health
  [[ -z $before_python ]] || probe=/api/ready
  launch "$1"
  for _ in $(seq 1 240); do
    if docker exec "$name" curl -fsS -o /dev/null "http://127.0.0.1:8000$probe" 2>/dev/null; then
      return 0
    fi
    [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ] \
      || fail "the $1 container stopped while booting"
    sleep 1
  done
  fail "the $1 container did not serve $probe within 240s"
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
[[ "$previous" =~ ^[0-9a-f]{40}$ && "$candidate" =~ ^[0-9a-f]{40}$ ]] \
  || fail "both images must record their commit in /app/build-info.json"
[ "$previous" != "$candidate" ] || fail "the previous and candidate images are the same release"
verify_image_source "$PREVIOUS" "$previous"
verify_image_source "$CANDIDATE" "$candidate"
echo "upgrade path: ${previous:0:12} -> ${candidate:0:12}"

# The image seeds a shallow checkout, so the candidate travels with its whole
# history: a bundle that excluded the previous release would need every commit
# the candidate branched from (a merge of an older branch does), which the seed
# lacks. A bundle carries refs, not bare commits, so name the candidate.
bundle_ref=refs/upgrade-path/candidate-${name##mobius-upgrade-path-}
git -C "$REPO" update-ref "$bundle_ref" "$candidate" "$(printf '0%.0s' {1..40})" \
  || fail "the repository does not contain the candidate ${candidate:0:12}"
bundle_owned=true
git -C "$REPO" merge-base --is-ancestor "$previous" "$candidate" \
  || fail "the candidate ${candidate:0:12} does not descend from ${previous:0:12}"
bundled=0
git -C "$REPO" bundle create "$work/candidate.bundle" "$bundle_ref" \
  >/dev/null 2>"$work/bundle.err" && bundled=1
git -C "$REPO" update-ref -d "$bundle_ref"
bundle_owned=false
[ "$bundled" = 1 ] || fail "could not bundle ${candidate:0:12} on ${previous:0:12}: $(cat "$work/bundle.err")"

echo "1. the previous release boots like an owner's instance"
if docker container inspect "$name" >/dev/null 2>&1 \
   || docker volume inspect "$volume" >/dev/null 2>&1; then
  fail "disposable container or volume name already exists; refusing to touch it"
fi
volume_owned=true
docker volume create "$volume" >/dev/null
start "$PREVIOUS"
if [[ -n $before_python ]]; then
  [[ $(python_version) == "$before_python" ]] \
    || fail "previous image does not run Python $before_python"
  package=${lock_package%%==*}
  package_target=${lock_package#*==}
  if package_version "$package" >/dev/null 2>&1; then
    fail "previous image already installs fixture package $package"
  fi
  if as_mobius grep -Fq "$lock_package" /data/platform/backend/requirements.lock; then
    fail "previous source already declares fixture package $lock_package"
  fi
fi

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
old_head=$(as_mobius git -C /data/platform rev-parse HEAD)

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

if [ "$needs_image" = True ]; then
  if [[ -n $before_python ]]; then
    as_mobius git -C /data/platform show "$candidate:backend/requirements.lock" \
      > "$work/candidate-requirements.lock"
    grep -Fq "$lock_package" "$work/candidate-requirements.lock" \
      || fail "candidate source does not declare $lock_package in requirements.lock"
  fi
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
  if [ "$force_rollback" = 1 ]; then
    echo "5. boot the candidate, then roll back to the wrong image before verification"
    # Images with a boot transaction leave image-requiring source prepared at
    # cutover. Only the matching new image may swap it in. Exercise that real
    # activation before testing the old image's boot-time snapshot reversion.
    [ "$(as_mobius python3 -c "import json; print(json.load(open('$record'))['state'])")" = prepared ] \
      || fail "cutover did not leave the image-requiring update prepared"
    [[ $(as_mobius git -C /data/platform rev-parse HEAD) == "$old_head" ]] \
      || fail "cutover moved source before the target image booted"
    docker stop -t 60 "$name" >/dev/null
    docker rm "$name" >/dev/null
    container_owned=false
    start "$CANDIDATE"
    [ "$(as_mobius python3 -c "import json; print(json.load(open('$record'))['state'])")" = swapped ] \
      || fail "target image boot did not swap the prepared source"
    as_mobius git -C /data/platform merge-base --is-ancestor "$candidate" HEAD \
      || fail "target image boot did not activate the candidate source"
    [[ $(python_version) == "$after_python" ]] \
      || fail "target image boot does not run Python $after_python"
    [[ $(package_version "$package") == "$package_target" ]] \
      || fail "target image boot did not install $lock_package"
    as_mobius grep -Fq "$lock_package" /data/platform/backend/requirements.lock \
      || fail "target image boot did not activate the candidate lock"
    verify_fixture_service "$candidate" "$(as_mobius git -C /data/platform rev-parse HEAD)"
    docker stop -t 60 "$name" >/dev/null
    docker rm "$name" >/dev/null
    container_owned=false
    # The stand-in presents the previous image as an incorrect replacement.
    # The old image's real boot transaction must revert the swapped checkout.
    docker run --rm --entrypoint python3 -v "$volume:/data" "$PREVIOUS" \
      -P /app/runtime/restart_ledger.py rearm-cutover "$operation" >/dev/null \
      || fail "the rollback handoff could not be re-armed"
    start "$PREVIOUS"
    [[ $(docker inspect -f '{{.Image}}' "$name") == \
       $(docker image inspect -f '{{.Id}}' "$PREVIOUS") ]] \
      || fail "rollback does not run the previous image"
    [[ $(as_mobius git -C /data/platform rev-parse HEAD) == "$old_head" ]] \
      || fail "the old image did not restore the pre-update source snapshot"
    verify_fixture_service "$previous" "$old_head"
    [[ $(python_version) == "$before_python" ]] \
      || fail "rollback does not run Python $before_python"
    if package_version "$package" >/dev/null 2>&1; then
      fail "rollback image unexpectedly installs $package"
    fi
    [ "$(as_mobius python3 -c "import json; print(json.load(open('$record'))['state'])")" = prepared ] \
      || fail "the rolled-back update is not kept prepared for retry"
    if [ "$local_edits" = true ]; then
      as_mobius grep -q 'upgrade-path: local image customization' /data/platform/Dockerfile \
        && as_mobius grep -q 'upgrade-path: local package note' /data/platform/backend/requirements.txt \
        || fail "rollback lost pre-update local customizations"
    fi
    ledger finalize-cutover || fail "the rollback could not finalize its handoff"
    write_status rolled_back "Wrong-image replacement reverted to the saved source and image."
    echo "upgrade path: wrong-image cutover restored ${previous:0:12} and its local work"
    exit 0
  fi
  write_status replacing "Rebuilding the container."
  docker stop -t 60 "$name" >/dev/null
  docker rm "$name" >/dev/null
  container_owned=false
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
  [[ -z $before_python ]] \
    || fail "Python/lock fixture was offered as source-only Apply, not an image replacement"
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
  [[ $(docker inspect -f '{{.Image}}' "$name") == \
     $(docker image inspect -f '{{.Id}}' "$CANDIDATE") ]] \
    || fail "the replacement runs another image than the paired candidate"
  if [[ -n $before_python ]]; then
    [[ $(python_version) == "$after_python" ]] \
      || fail "replacement does not run Python $after_python"
    [[ $(package_version "$package") == "$package_target" ]] \
      || fail "replacement did not install $lock_package from its lock"
    # This fixture uses a distribution with a matching import name (colorama).
    # Installed metadata alone must not pass a broken import.
    docker exec "$name" python3 -c \
      'import importlib, sys; importlib.import_module(sys.argv[1].replace("-", "_"))' "$package" \
      || fail "replacement cannot import fixture package $package"
    as_mobius grep -Fq "$lock_package" /data/platform/backend/requirements.lock \
      || fail "served source lost $lock_package from requirements.lock"
    verify_fixture_service "$candidate" "$(as_mobius git -C /data/platform rev-parse HEAD)"
  fi
  for _ in $(seq 1 60); do  # reading the status confirms the exact replacement
    api GET /api/admin/rebuild >/dev/null
    as_mobius test -e "$record" || break
    sleep 2
  done
  as_mobius test ! -e "$record" || fail "the update never settled: $(as_mobius cat "$record")"
fi
echo "upgrade path: ${previous:0:12} installs ${candidate:0:12}"
