#!/usr/bin/env bash
# Real-Docker regression for the production boot transaction. Requires the
# image under test (default mobius-test:ci) built with BUILD_SHA pinned, so its
# baked checkout and /app/build-info.json name the same commit.
#
# The disposable e2e runtime skips the transaction, so this boots the ordinary
# production path on a persistent volume and checks, on real restarts:
#   0. the first boot runs the transaction on the fresh seed;
#   1. a plain restart runs the image's transaction and serves /data/platform;
#   2. an update prepared for exactly this image is swapped in by its boot,
#      with an in-progress edit carried across;
#   3. an update swapped in for another image is returned to its saved state;
#   4. a record from a newer boot protocol stops the boot (fail closed), and
#      the instance boots again once that record is gone.

set -euo pipefail

IMAGE="${1:-mobius-test:ci}"
name="mobius-boot-transaction-$$"
volume="${name}-data"
record=/data/.platform-prepared-update.json

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "boot transaction regression: $*" >&2
  docker logs "$name" --tail 80 >&2 2>&1 || true
  exit 1
}

as_mobius() { docker exec -u mobius "$name" "$@"; }

wait_healthy() {
  for _ in $(seq 1 180); do
    if docker exec "$name" curl -fsS -o /dev/null http://127.0.0.1:8000/api/health 2>/dev/null; then
      return 0
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" != "true" ]; then
      fail "the container stopped while booting"
    fi
    sleep 1
  done
  fail "the container did not serve /api/health within 180s"
}

restart() {
  local since
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker restart "$name" >/dev/null
  wait_healthy
  boot_log=$(docker logs --since "$since" "$name" 2>&1)
}

expect_log() {
  grep -qF -- "$1" <<<"$boot_log" || fail "expected boot log line: $1"
}

serving_platform() {
  [ "$(docker exec "$name" cat /tmp/serving-source)" = "platform" ] \
    || fail "the platform checkout is not served"
  [ "$(docker exec "$name" cat /tmp/platform-boot-transaction)" = "$protocol" ] \
    || fail "the boot transaction did not publish its protocol"
}

write_record() {
  as_mobius python3 -c 'import json, sys
fields = json.loads(sys.argv[2])
fields.setdefault("image_digest", None)
fields.setdefault("requires_image", True)
fields.setdefault("protocol", int(sys.argv[3]))
with open(sys.argv[1], "w") as handle:
  json.dump(fields, handle)' "$record" "$1" "$protocol"
}

commit_on_head() {  # <path> <content>: a commit adding one file, branch unmoved
  as_mobius sh -c '
    set -e
    cd /data/platform
    blob=$(printf "%s\n" "$2" | git hash-object -w --stdin)
    # A private index file: --index-output cannot write outside the
    # repository filesystem, and the shared index must stay untouched.
    index=$(mktemp -u /tmp/smoke-index.XXXXXX)
    GIT_INDEX_FILE=$index git read-tree HEAD
    GIT_INDEX_FILE=$index git update-index --add --cacheinfo "100644,$blob,$1"
    tree=$(GIT_INDEX_FILE=$index git write-tree)
    rm -f "$index"
    git -c user.name=smoke -c user.email=smoke@example.invalid \
      commit-tree "$tree" -p HEAD -m "boot transaction smoke: $1"
  ' sh "$1" "$2"
}

docker volume create "$volume" >/dev/null
docker run -d --name "$name" --init --restart no \
  -v "$volume:/data" \
  -e "SECRET_KEY=boot-transaction-regression-key-0123456789" \
  -e "DATABASE_URL=sqlite:////data/db/boot-transaction.db" \
  -e "DATA_DIR=/data" \
  -e "DOMAIN=localhost" \
  -e "FRONTEND_ORIGIN=http://localhost" \
  -e "MOEBIUS_SKIP_BOOTSTRAP=1" \
  "$IMAGE" >/dev/null
wait_healthy  # first boot seeds /data/platform from the baked checkout
# The protocol this image's own boot transaction publishes.
protocol=$(docker exec "$name" cat /app/platform-baked/backend/runtime/boot-protocol)
[[ "$protocol" =~ ^[0-9]+$ ]] || fail "the image records no boot protocol"
echo "0. the first boot runs the same transaction on the fresh seed"
serving_platform

image=$(docker exec "$name" python3 -c \
  'import json; print(json.load(open("/app/build-info.json"))["sha"])')
[[ "$image" =~ ^[0-9a-f]{40}$ ]] || fail "the image records no revision"

echo "1. a plain restart runs the image's boot transaction"
restart
expect_log "platform boot activate: none"
serving_platform

echo "2. an update prepared for this image is swapped in by its boot"
head=$(as_mobius git -C /data/platform rev-parse HEAD)
prepared=$(commit_on_head boot-smoke-update.txt "from the update")
as_mobius git -C /data/platform update-ref refs/mobius/update-prepared "$prepared"
write_record "{\"state\":\"prepared\",\"snapshot\":\"$head\",\"prepared\":\"$prepared\",\"target\":\"$image\"}"
as_mobius sh -c 'printf "in progress\n" > /data/platform/boot-smoke-late.txt'
restart
expect_log "platform boot activate: replayed"
serving_platform
as_mobius git -C /data/platform merge-base --is-ancestor "$prepared" HEAD \
  || fail "the checkout does not contain the prepared update"
[ "$(as_mobius cat /data/platform/boot-smoke-update.txt)" = "from the update" ] \
  || fail "the update's file is missing"
[ "$(as_mobius cat /data/platform/boot-smoke-late.txt)" = "in progress" ] \
  || fail "the in-progress edit was lost"
for _ in $(seq 1 60); do  # the started server confirms an unbound swap
  as_mobius test -e "$record" || break
  sleep 1
done
as_mobius test ! -e "$record" || fail "the started server did not confirm the swap"

echo "3. an update swapped in for another image is returned to its saved state"
as_mobius rm -f /data/platform/boot-smoke-late.txt
late=$(as_mobius git -C /data/platform rev-parse HEAD)
upstream=$(as_mobius git -C /data/platform rev-parse upstream)
other=$(commit_on_head boot-smoke-other-image.txt "needs another image")
as_mobius git -C /data/platform reset -q --hard "$other"
write_record "{\"state\":\"swapped\",\"snapshot\":\"$late\",\"prepared\":\"$other\",\"target\":\"$(printf 'f%.0s' $(seq 1 40))\",\"late\":\"$late\",\"late_committed\":\"$late\",\"restore\":{\"upstream\":\"$upstream\",\"activation\":null}}"
restart
expect_log "platform boot activate: reverted"
serving_platform
[ "$(as_mobius git -C /data/platform rev-parse HEAD)" = "$late" ] \
  || fail "the checkout did not return to the saved state"
as_mobius test ! -e /data/platform/boot-smoke-other-image.txt \
  || fail "the other image's source is still served"
[ "$(as_mobius python3 -c "import json; print(json.load(open('$record'))['state'])")" = "prepared" ] \
  || fail "the reverted update is not prepared again"
as_mobius rm -f "$record"

echo "4. a record from a newer boot protocol falls back instead of crash-looping"
write_record "{\"state\":\"prepared\",\"snapshot\":\"$late\",\"prepared\":\"$late\",\"target\":\"$image\",\"protocol\":$((protocol + 1))}"
held=$(as_mobius git -C /data/platform rev-parse HEAD)
restart  # the protected built-in version serves; the container never exits
expect_log "boot activate could not settle /data/platform"
[ "$(docker exec "$name" cat /tmp/serving-source)" = "baked" ] \
  || fail "the unsettled boot did not fall back to the baked platform"
[ "$(docker exec "$name" cat /tmp/platform-boot-unsettled)" = "activate" ] \
  || fail "the fallback did not pause update work"
docker exec "$name" test ! -e /tmp/platform-boot-transaction \
  || fail "a failed transaction published its protocol"
as_mobius grep -qF "boot protocol $((protocol + 1))" /data/logs/platform-boot.jsonl \
  || fail "the boot log does not say why the transaction failed"
[ "$(as_mobius git -C /data/platform rev-parse HEAD)" = "$held" ] \
  || fail "the fallback changed the checkout it left for the next boot"
as_mobius test -e "$record" || fail "the fallback dropped the update record"
as_mobius rm -f "$record"
restart
docker exec "$name" test ! -e /tmp/platform-boot-unsettled \
  || fail "a settled boot kept update work paused"
serving_platform

echo "boot transaction regression: ok"
