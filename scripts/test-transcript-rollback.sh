#!/usr/bin/env bash
# Real-image rollback round trip for per-message transcript storage.
#
#   scripts/test-transcript-rollback.sh <previous-image> <candidate-image>
#
# The candidate keeps the previous release's chats.messages as an exact mirror
# of its message rows, so the previous image may run on the same database at
# any time. Each container gets a fresh /data (its own baked code) and shares
# only /data/db, so every step reads and writes with that release's own code
# (scripts/transcript_rollback_probe.py runs inside it).
#
# 1. previous: seed typed, bulk, damaged and tombstoned chats.
# 2. candidate: ready at once; converts in the background; every transcript is
#    exact, the damaged bytes are kept; then writes through writer commands.
# 3. previous: reads every chat exactly as the candidate left it; writes.
# 4. before the candidate returns, exactly the chats the previous release
#    changed or created lack a conversion marker and its purge left nothing;
#    the candidate re-converts them and nothing is lost.
# 5. one more candidate -> previous -> candidate cycle (steady state).
#
# Transcripts are compared as canonical JSON text, and every converted chat's
# legacy bytes must equal '[' + ', '.join(row bodies) + ']' exactly.
# Waits poll observable state; the CI job's timeout is the only bound.

set -euo pipefail

PREVIOUS="${1:?previous image}"
CANDIDATE="${2:?candidate image}"
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
name="mobius-transcript-rollback-$$"
db_volume="${name}-db"
work=$(mktemp -d)

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$db_volume" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "transcript rollback: $*" >&2
  docker logs "$name" --tail 80 >&2 2>&1 || true
  exit 1
}

start() {  # <image>
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --init --restart no \
    -v "$db_volume:/data/db" \
    -e "SECRET_KEY=transcript-rollback-regression-key-0123456789" \
    -e "DATABASE_URL=sqlite:////data/db/rollback.db" \
    -e "DATA_DIR=/data" -e "DOMAIN=localhost" -e "FRONTEND_ORIGIN=http://localhost" \
    -e "MOEBIUS_SKIP_BOOTSTRAP=1" \
    "$1" >/dev/null
  until docker exec "$name" curl -fsS -o /dev/null http://127.0.0.1:8000/api/ready 2>/dev/null; do
    [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ] \
      || fail "the $1 container stopped before it was ready"
    sleep 1
  done
  # A fresh /data seeds its checkout from the image, so the served code must
  # be exactly the image's own release. /api/version's "sha" names only the
  # image; the entrypoint's record names the tree uvicorn serves.
  local expected served_sha
  expected=$(docker exec "$name" git -c safe.directory='*' -C /app/platform-baked rev-parse HEAD)
  served_sha=$(docker exec "$name" cat /tmp/serving-sha)
  [ "$served_sha" = "$expected" ] || fail "the $1 container serves $served_sha, not its own $expected"
  docker cp "$ROOT/scripts/transcript_rollback_probe.py" "$name:/tmp/probe.py"
}

probe() {  # <command>: runs in the tree the server runs; any error fails at once
  local backend output
  # As the server's own user: the container has no CAP_SYS_PTRACE, so root
  # cannot read another user's /proc/<pid>/cwd.
  backend=$(docker exec -u mobius "$name" sh -c 'readlink "/proc/$(pgrep -n -u mobius -f "/bin/uvicorn app\.main:app")/cwd"') \
    && [ -n "$backend" ] || fail "cannot locate the serving uvicorn process"
  if ! output=$(docker exec -u mobius -e "PROBE_ROUND=${round:-1}" -w "$backend" -e PYTHONPATH="$backend" \
      "$name" python3 /tmp/probe.py "$1" 2>"$work/probe.err"); then
    cat "$work/probe.err" >&2
    fail "probe '$1' failed in $backend"
  fi
  printf '%s\n' "$output"
}

offline_probe() {  # <image> <command>: no server running; the image's own code reads raw SQLite
  docker run --rm --entrypoint python3 -u mobius -e "PROBE_ROUND=${round:-1}" -w /app/platform-baked/backend \
    -e PYTHONPATH=/app/platform-baked/backend -e SECRET_KEY=offline-probe-key-0123456789abcdef \
    -e DATABASE_URL=sqlite:////data/db/rollback.db -e DATA_DIR=/tmp \
    -v "$db_volume:/data/db" -v "$ROOT/scripts/transcript_rollback_probe.py:/tmp/probe.py:ro" \
    "$1" /tmp/probe.py "$2"
}

converge() {  # waits only on a valid "still pending" answer
  local answer
  while :; do
    answer=$(probe pending)
    case $answer in
      '{"pending": 0}') return 0 ;;
      '{"pending": '[1-9]*'}') sleep 1 ;;
      *) fail "unexpected conversion answer: $answer" ;;
    esac
  done
}

same() {  # <label> <a.json> <b.json>: canonical text, so value types count
  python3 - "$2" "$3" "$1" <<'PY' || exit 1
import json, sys
a, b = ({k: json.dumps(v, sort_keys=True) for k, v in json.load(open(path)).items()}
        for path in sys.argv[1:3])
if a != b:
  missing = sorted(set(a) ^ set(b)); differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
  sys.exit(f"{sys.argv[3]}: mismatch; only on one side: {missing[:5]}; differing: {differ[:5]}")
PY
}

docker volume create "$db_volume" >/dev/null

# "previous" is a release without message rows: the one an owner rolls back to
# across the conversion. Once the newest published ancestor stores rows itself,
# use the conversion's parent, which the candidate must still be able to run on.
if [ "$(offline_probe "$PREVIOUS" stores-rows)" = '{"rows": true}' ]; then
  conversion=$(git -C "$ROOT" log --first-parent --diff-filter=A --format=%H -- backend/app/transcript_rows.py | tail -1)
  [ -n "$conversion" ] || fail "cannot find the release that introduced message rows"
  PREVIOUS="${PREVIOUS%%:*}:sha-$(git -C "$ROOT" rev-parse "$conversion^")"
  echo "previous release already stores message rows; rolling back to $PREVIOUS"
  docker pull -q "$PREVIOUS" >/dev/null || fail "cannot pull $PREVIOUS"
fi

echo "1. previous release seeds history"
start "$PREVIOUS"
probe seed >/dev/null
probe dump > "$work/seeded.json"
docker stop "$name" >/dev/null

for round in 1 2; do
  echo "2.$round candidate converts and writes"
  start "$CANDIDATE"
  converge
  probe dump > "$work/converted.json"
  python3 - "$work/seeded.json" "$work/converted.json" <<'PY' || fail "conversion changed a transcript"
import json, sys
seeded, converted = (json.load(open(p)) for p in sys.argv[1:])
placeholder = converted.pop("damaged")["messages"]
seeded.pop("damaged")
assert placeholder and placeholder[0].get("transcript_damage") is True, placeholder
text = lambda d: {k: json.dumps(v, sort_keys=True) for k, v in d.items()}
seeded, converted = text(seeded), text(converted)
assert seeded == converted, sorted(k for k in seeded if seeded[k] != converted.get(k))[:5]
PY
  [ "$(probe mirror-exact)" = '{"differ": []}' ] || fail "a converted chat's legacy bytes are not its rows"
  probe write-new >/dev/null
  [ "$(probe mirror-exact)" = '{"differ": []}' ] || fail "a mirror after candidate writes is not byte-exact"
  probe dump > "$work/candidate.json"
  docker stop "$name" >/dev/null

  echo "3.$round previous release reads everything and writes"
  start "$PREVIOUS"
  probe dump > "$work/previous-view.json"
  same "previous release sees the candidate's transcripts" "$work/candidate.json" "$work/previous-view.json"
  wrote=$(probe write-old)
  probe dump > "$work/seeded.json"
  docker stop "$name" >/dev/null

  echo "4.$round only the previous release's changes are marked"
  marks=$(offline_probe "$CANDIDATE" unconverted)
  python3 - "$marks" "$wrote" <<'PY' || fail "the previous release's changes were not detected exactly: $marks (expected per $wrote)"
import json, sys
state = json.loads(sys.argv[1])
expected = json.loads(sys.argv[2])["expect_unconverted"]
assert state["unconverted"] == expected, state["unconverted"]
assert set(state["purged_leftovers"].values()) == {0}, state["purged_leftovers"]
PY
done

echo "5. the candidate returns and nothing is lost"
start "$CANDIDATE"
converge
probe dump > "$work/final.json"
[ "$(probe mirror-exact)" = '{"differ": []}' ] || fail "a re-converted chat's legacy bytes are not its rows"
same "the candidate re-converted the previous release's writes" "$work/seeded.json" "$work/final.json"
echo "transcript rollback: ok"
