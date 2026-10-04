#!/usr/bin/env bash
# Real first upgrade of a helper-installed host to the transcript-row release.
#
#   sudo scripts/test-transcript-host-cutover.sh <previous-sha> <target-sha> [scenario...]
#
# Run on a DISPOSABLE systemd host with Docker Compose (a CI runner). It uses
# the production container, volume, unit and state names, refuses a host that
# already has any of them, and removes them between scenarios. Both images
# must be pullable as ghcr.io/mobius-os/mobius:sha-<sha> with their official
# labels; the installed worker pulls them itself. An unpublished candidate can
# be served to it by a disposable registry (see the transcript_host_proof job
# in .github/workflows/test.yml); nothing here bypasses the worker's checks.
#
# Every scenario starts like an owner: <previous> deployed from its checkout,
# owner set up, a typed fixture chat plus bulk history, and the helper
# installed once from that checkout (worker revision 2 for releases before
# the transcript rows). Then:
#
#   upgrade      the owner runs <target>'s installer, which verifies the
#                floor-aware worker as ACTIVE and finishes the waiting update;
#                then the app asks for <previous> again and must be refused
#                (rolled back onto <target>, never left on <previous>).
#   before       the new container is stopped while converting (floor 0): the
#                worker restores <previous> with the legacy chats exact, and a
#                second attempt resumes and completes.
#   after        the new container is stopped once the floor has risen: the
#                worker must never start <previous>; it settles forward.
#   interrupted  the worker itself is killed once the floor has risen: boot
#                reconciliation must settle forward the same way.
#
# Prints one timing line per scenario (installer/request to settled outcome).

set -euo pipefail

PREVIOUS="${1:?previous sha}"
TARGET="${2:?target sha}"
shift 2
SCENARIOS=("$@")
[[ ${#SCENARIOS[@]} -gt 0 ]] || SCENARIOS=(upgrade before after interrupted)
[[ $PREVIOUS =~ ^[0-9a-f]{40}$ && $TARGET =~ ^[0-9a-f]{40}$ && $PREVIOUS != "$TARGET" ]] \
  || { echo "two distinct full release SHAs are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
IMAGE=ghcr.io/mobius-os/mobius
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
STATUS=/var/lib/mobius-rebuild/status.json
WORK=$(mktemp -d /tmp/mobius-transcript-host.XXXXXX)
ENV_FILE=$WORK/env
export COMPOSE_PROJECT_NAME=mobius
PREVIOUS_ID=""
TARGET_ID=""
HELPER_PATHS=(/etc/mobius-rebuild /var/lib/mobius-rebuild
  /usr/local/libexec/mobius-rebuild-host /etc/systemd/system/mobius-rebuild.service
  /etc/systemd/system/mobius-rebuild.path /etc/systemd/system/mobius-rebuild-reconcile.service)

fail() {
  echo "transcript host: $*" >&2
  systemctl --no-pager status mobius-rebuild.service mobius-rebuild.path >&2 || true
  journalctl --no-pager -u mobius-rebuild.service -n 80 >&2 || true
  cat "$STATUS" >&2 2>/dev/null || true
  docker logs mobius --tail 80 >&2 2>&1 || true
  exit 1
}

docker info >/dev/null || fail "Docker is unavailable"
[[ -d /run/systemd/system ]] || fail "systemd is required"
if docker container inspect mobius >/dev/null 2>&1 \
   || docker volume inspect mobius_app_data >/dev/null 2>&1; then
  echo "transcript host: use a fresh disposable host, not an existing installation" >&2
  exit 2
fi
for path in "${HELPER_PATHS[@]}"; do
  if [[ -e $path || -L $path ]]; then
    echo "transcript host: existing helper path $path; use a fresh disposable host" >&2
    exit 2
  fi
done
printf 'SECRET_KEY=transcript-host-regression-key-0123456789abcdef\nDOMAIN=localhost\n' >"$ENV_FILE"
chmod 0600 "$ENV_FILE"
# One owner checkout: deployed at <previous>, later updated in place to
# <target> (the installer refuses a checkout other than the deployed one).
CHECKOUT=$WORK/checkout
git -C "$ROOT" worktree add --detach -q "$CHECKOUT" "$PREVIOUS"
cleanup() {
  reset_host || true
  git -C "$ROOT" worktree remove --force "$CHECKOUT" 2>/dev/null || true
  rm -rf "$WORK"
}
update_checkout() { git -C "$CHECKOUT" checkout -q --detach "$1"; }
trap cleanup EXIT
docker pull -q "$IMAGE:sha-$PREVIOUS" >/dev/null
docker pull -q "$IMAGE:sha-$TARGET" >/dev/null
PREVIOUS_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE:sha-$PREVIOUS")
TARGET_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE:sha-$TARGET")

reset_host() {  # remove everything a scenario created; images stay pulled
  systemctl disable --now mobius-rebuild.path mobius-rebuild-reconcile.service >/dev/null 2>&1 || true
  systemctl stop mobius-rebuild.service >/dev/null 2>&1 || true
  rm -rf "${HELPER_PATHS[@]}"
  systemctl daemon-reload
  docker rm -f mobius >/dev/null 2>&1 || true
  docker volume rm mobius_app_data >/dev/null 2>&1 || true
  # The worker re-tags images it manages; restore the official tags.
  [[ -z $PREVIOUS_ID ]] || docker tag "$PREVIOUS_ID" "$IMAGE:sha-$PREVIOUS"
  [[ -z $TARGET_ID ]] || docker tag "$TARGET_ID" "$IMAGE:sha-$TARGET"
  update_checkout "$PREVIOUS"
}

field() {  # <json-file> <python expression over d>
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}
database() { echo "$(docker volume inspect -f '{{.Mountpoint}}' mobius_app_data)/db/ultimate.db"; }
sql() {  # <query>: one read-only answer from the host, never through the app
  python3 - "$(database)" "$1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
try:
    row = conn.execute(sys.argv[2]).fetchone()
    print("" if row is None else row[0])
except sqlite3.OperationalError:
    print("")
PY
}
floor() { local value; value=$(sql "SELECT floor FROM platform_compat WHERE id = 1"); echo "${value:-0}"; }
running_image() { docker inspect -f '{{.Image}}' mobius 2>/dev/null || true; }
update_unbound() {  # the app's prepared update no longer names a replacement
  local text  # an unreachable container is "not yet", never "released"
  text=$(docker exec mobius sh -c 'f=/data/.platform-prepared-update.json; [ ! -e "$f" ] || cat "$f"') \
    || return 1
  python3 -c 'import json, sys; t = sys.argv[1]; sys.exit(0 if not t.strip() or json.loads(t).get("operation") is None else 1)' "$text"
}

wait_outcome() {  # <nonce>: print the settled root state for that request
  for _ in $(seq 1 360); do
    if [[ -f $STATUS ]] && [[ $(field "$STATUS" 'd.get("request_nonce")') == "$1" ]]; then
      local state; state=$(field "$STATUS" 'd.get("state")')
      case "$state" in
        succeeded|no_change|failed|rolled_back|needs_recovery)
          systemctl is-active --quiet mobius-rebuild.service && { sleep 2; continue; }
          echo "$state"; return 0 ;;
      esac
    fi
    sleep 5
  done
  fail "request $1 did not settle within 30 minutes"
}

FIXTURE='[{"id":"m1","role":"user","content":"héllo 😀","ts":1},{"id":"m1","role":"assistant","content":"same id","blocks":[{"type":"text","content":"x"}],"n":1.0,"flag":true},{"role":"assistant","content":"no id","values":[0,-0.0,2.5,null]}]'

deploy_previous() {  # <bulk-chats>
  update_checkout "$PREVIOUS"
  (cd "$CHECKOUT" && MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" docker compose \
    --env-file "$ENV_FILE" up -d --no-build --no-deps app) >/dev/null
  for _ in $(seq 1 60); do
    [[ $(docker inspect -f '{{.State.Health.Status}}' mobius 2>/dev/null) == healthy ]] && break
    sleep 5
  done
  [[ $(docker inspect -f '{{.State.Health.Status}}' mobius) == healthy ]] \
    || fail "the previous release did not become healthy"
  docker exec mobius curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d '{"username":"owner","password":"transcript-host-owner-password"}' \
    http://127.0.0.1:8000/api/auth/setup || fail "owner setup failed"
  docker exec -i -u mobius -w /data/platform/backend mobius python3 - "$FIXTURE" "$1" <<'PY' \
    || fail "could not store the fixture chats"
import json, sys
sys.path.insert(0, ".")
from app import models
from app.database import SessionLocal
db = SessionLocal()
db.add(models.Chat(id="fixture", title="Fixture", messages=json.loads(sys.argv[1]),
                   has_messages=True))
# Bulk history so conversion takes long enough to interrupt deliberately.
body = "x" * 4000
for n in range(int(sys.argv[2])):
    db.add(models.Chat(id=f"bulk-{n:05d}", title=f"Bulk {n}", has_messages=True,
                       messages=[{"id": f"b{n}-{i}", "role": "user" if i % 2 else "assistant",
                                  "content": f"{n}:{i}:{body}"} for i in range(25)]))
    if n % 200 == 199:
        db.commit()
db.commit()
db.close()
PY
  # The previous release reviews <target> from its own clone. A published
  # release is on origin/main; an unpublished candidate travels as a bundle.
  local ref=refs/transcript-host/target-$$
  git -C "$ROOT" update-ref "$ref" "$TARGET"
  git -C "$ROOT" bundle create "$WORK/target.bundle" "$ref" >/dev/null 2>&1 \
    || fail "could not bundle sha-$TARGET"
  git -C "$ROOT" update-ref -d "$ref"
  docker cp "$WORK/target.bundle" mobius:/tmp/target.bundle >/dev/null
  docker exec mobius chmod 0644 /tmp/target.bundle
  docker exec -u mobius mobius git -C /data/platform fetch -q /tmp/target.bundle \
    "$ref:refs/remotes/origin/main" || fail "the previous release could not fetch sha-$TARGET"
  for _ in $(seq 1 30); do docker exec mobius test -s /data/service-token.txt && break; sleep 2; done
  (cd "$CHECKOUT" && scripts/install-rebuild-helper.sh >/dev/null) \
    || fail "the previous release's installer failed"
}

assert_fixture_exact() {  # chats are exact in whichever form this database uses
  python3 - "$(database)" "$FIXTURE" <<'PY' || fail "the fixture chat is not exact"
import json, sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
expected = json.loads(sys.argv[2])
canon = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)
columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
if "messages" in columns:  # legacy authority
    actual = json.loads(conn.execute("SELECT messages FROM chats WHERE id='fixture'").fetchone()[0])
else:
    actual = [json.loads(body) for (body,) in conn.execute(
        "SELECT body FROM chat_messages WHERE chat_id='fixture' ORDER BY seq")]
    bulk = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_id LIKE 'bulk-%'").fetchone()[0]
    chats = conn.execute("SELECT COUNT(*) FROM chats WHERE id LIKE 'bulk-%'").fetchone()[0]
    assert bulk == chats * 25, (bulk, chats)
assert [canon(m) for m in actual] == [canon(m) for m in expected], actual
PY
}

assert_target_serving() {
  [[ $(running_image) == "$TARGET_ID" ]] || fail "the container is not sha-$TARGET"
  [[ $(docker exec mobius curl -fsS http://127.0.0.1:8000/api/version \
        | python3 -c 'import json, sys; print(json.load(sys.stdin).get("sha"))') == "$TARGET" ]] \
    || fail "the container does not serve sha-$TARGET"
  [[ $(floor) == 1 ]] || fail "the database floor is $(floor), not 1"
  [[ $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' mobius \
        | grep -c '^MOBIUS_HOST_RECOVERY_REQUIRED=1$') == 1 ]] \
    || fail "the container does not declare Host recovery"
  [[ $(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/run/mobius-rebuild-host"}}{{.RW}}{{end}}{{end}}' mobius) == false ]] \
    || fail "the Host state is not mounted read-only"
  [[ ! -e /var/lib/mobius-rebuild/transaction.json ]] || fail "a replacement journal was left behind"
}

assert_previous_never_started_after_floor() {  # <since>
  local started
  # Only the app container counts: the worker's own read-only probes run the
  # previous image deliberately (and never as the app).
  started=$(docker events --since "$1" --until "$(date +%s)" --filter type=container \
    --filter event=start --filter label=com.docker.compose.service=app \
    --format '{{.Actor.Attributes.image}}' | sort -u)
  while read -r image; do
    [[ -z $image ]] && continue
    [[ $(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null) != "$PREVIOUS_ID" ]] \
      || fail "the level-0 previous image was started after the floor rose"
  done <<<"$started"
}

queue_target() {  # the installer's bridge, run on its own; prints the nonce
  local out
  out=$(docker exec -i -u mobius -w /data/platform/backend mobius \
    python3 -I - "$TARGET" "$(tr -d '[:space:]' < "$CHECKOUT/deployment/self-hosted-helper.required")" \
    < "$CHECKOUT/scripts/finish-helper-update.py") || fail "the bridge refused: $out"
  [[ $(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["state"])' "$out") == queued ]] \
    || fail "the bridge did not queue the update: $out"
  python3 -c 'import json, sys; print(json.loads(sys.argv[1])["request_nonce"])' "$out"
}

wait_for() {  # <description> <shell condition>: poll quickly, bounded
  for _ in $(seq 1 3000); do eval "$2" && return 0; sleep 0.2; done
  fail "timed out waiting for $1"
}

scenario_upgrade() {
  deploy_previous 600
  local started=$SECONDS
  update_checkout "$TARGET"
  (cd "$CHECKOUT" && scripts/install-rebuild-helper.sh) || fail "the target installer did not finish the update"
  echo "timing upgrade: installer finished the update in $((SECONDS - started))s"
  assert_target_serving
  assert_fixture_exact
  local revision; revision=$(python3 -c 'import json; print(json.load(open("/var/lib/mobius-rebuild/workers.json"))["active"]["revision"])')
  (( revision >= 4 )) || fail "the ACTIVE worker is revision $revision"
  echo "   the app asks for the previous release again"
  local since; since=$(date +%s)
  local nonce; nonce=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  docker exec -u mobius mobius sh -c "
    printf '%s' '{\"version\":2,\"expected_sha\":\"$PREVIOUS\",\"nonce\":\"$nonce\"}' \
      > /data/mobius-rebuild/inbox/.request.tmp &&
    mv /data/mobius-rebuild/inbox/.request.tmp /data/mobius-rebuild/inbox/request.json"
  local before_id; before_id=$(docker inspect -f '{{.Id}}' mobius)
  local outcome; outcome=$(wait_outcome "$nonce")
  [[ $outcome == failed && $(field "$STATUS" 'd.get("code")') == newer_version_required ]] \
    || fail "asking for the level-0 release ended $outcome ($(field "$STATUS" 'd.get("code")'))"
  [[ $(docker inspect -f '{{.Id}}' mobius) == "$before_id" ]] \
    || fail "a refused downgrade still replaced the container"
  assert_previous_never_started_after_floor "$since"
  assert_target_serving
  assert_fixture_exact
}

interrupt_scenario() {  # <when: before|after> <how: container|worker>
  deploy_previous 2500
  update_checkout "$TARGET"
  (cd "$CHECKOUT" && scripts/install-rebuild-helper.sh --no-update) >/dev/null \
    || fail "the target installer failed"
  local since; since=$(date +%s)
  local started=$SECONDS
  local nonce; nonce=$(queue_target)
  if [[ $1 == before ]]; then
    wait_for "conversion progress" '[[ $(running_image) == "$TARGET_ID" && -n $(sql "SELECT 1 FROM upgrade_units LIMIT 1") && $(floor) == 0 ]]'
  else
    wait_for "the floor to rise" '[[ $(floor) == 1 ]]'
  fi
  if [[ $2 == worker ]]; then
    systemctl kill --signal=SIGKILL mobius-rebuild.service
  elif [[ $1 == before ]]; then
    # Killed as soon as conversion starts, long before it can activate; the
    # floor check proves this run really interrupted it before activation.
    docker kill mobius >/dev/null
    [[ $(floor) == 0 ]] || fail "conversion activated before the interruption; rerun with more history"
  else
    docker stop -t 1 mobius >/dev/null
  fi
  local outcome; outcome=$(wait_outcome "$nonce")
  echo "timing $1/$2: request to settled ($outcome) in $((SECONDS - started))s"
  if [[ $1 == before ]]; then
    [[ $outcome == rolled_back ]] || fail "an interruption before activation ended $outcome"
    [[ $(running_image) == "$PREVIOUS_ID" && $(floor) == 0 ]] \
      || fail "the previous release was not restored on the legacy data"
    assert_fixture_exact
    started=$SECONDS
    # The restored release keeps its prepared update but, on its own poll,
    # releases the failed replacement bound to it; Finish may then retry.
    wait_for "the restored release to release the failed replacement" update_unbound
    nonce=$(queue_target)
    outcome=$(wait_outcome "$nonce")
    echo "timing before/resume: second attempt settled ($outcome) in $((SECONDS - started))s"
    [[ $outcome == succeeded ]] || fail "the resumed conversion ended $outcome"
  else
    [[ $outcome == succeeded ]] || fail "settling forward after activation ended $outcome"
    assert_previous_never_started_after_floor "$since"
  fi
  assert_target_serving
  assert_fixture_exact
}

for scenario in "${SCENARIOS[@]}"; do
  echo "== $scenario"
  case "$scenario" in
    upgrade) scenario_upgrade ;;
    before) interrupt_scenario before container ;;
    after) interrupt_scenario after container ;;
    interrupted) interrupt_scenario after worker ;;
    *) fail "unknown scenario $scenario" ;;
  esac
  reset_host
done
echo "transcript host: ${SCENARIOS[*]} passed for sha-${PREVIOUS:0:12} -> sha-${TARGET:0:12}"
