#!/usr/bin/env bash
# Real self-update of the self-hosted replacement helper. An owner installed
# the helper from <previous>; <target>'s image must bring its own worker to
# that host by itself, through the installed launcher and systemd units,
# driven only by requests the app writes into /data.
#
#   sudo scripts/test-host-helper.sh <previous-sha> <target-sha>
#
# Set MOBIUS_RELEASE_REPLAY=1 to also prove automatic dependency restoration
# with a strictly newer worker. Set MOBIUS_TRANSCRIPT_PROOF=1 to also prove
# that per-message transcript storage survives real container replacements.
# The image and the served source are separate: an image replacement serves
# the existing /data/platform checkout unless an update was prepared for it,
# and /api/version's "sha" names the image. So the proof prepares the target
# exactly as Settings does, and every check runs and asserts the code the
# container actually serves (/tmp/serving-source, /tmp/serving-sha, and the
# uvicorn process's working directory):
#   3. the worker swaps in the prepared target: it converts and writes;
#   4. the previous image keeps serving the target source (a restart-loadable
#      release): every transcript stays exact;
#   5. a committed served tree that fails the import probe makes the
#      entrypoint serve the previous image's baked code, which reads every
#      transcript exactly and writes;
#   6. the source is repaired and the target image returns: exactly the
#      previous code's changes were marked, and they re-convert exactly. No live host may be used for either mode.
# Both SHAs must have published official images. Run on a disposable systemd
# host with Docker Compose (a CI runner); it installs root-owned units there.
#
# 1. Deploy <previous> from its own checkout, as an owner does, and set up the
#    owner.
# 2. Install the helper once from that checkout.
# 3. The app requests <target>: the installed worker replaces the container
#    and offers <target>'s worker when its revision is higher.
# 4. When the target advances the frozen helper requirement, explicitly install
#    that migration and prove the old launcher/helper preserves the queued request.
#    Revision 5 adds root-pinned admission, including for exact legacy images.
#    The offered/installed worker then performs the real replacement to <previous>.

set -euo pipefail

PREVIOUS="${1:?previous sha}"
TARGET="${2:?target sha}"
# These values become Git refs and JSON request values below.
[[ $PREVIOUS =~ ^[0-9a-f]{40}$ && $TARGET =~ ^[0-9a-f]{40}$ && $PREVIOUS != "$TARGET" ]] \
  || { echo "two distinct full release SHAs are required" >&2; exit 2; }
REPLAY=${MOBIUS_RELEASE_REPLAY:-0}
TRANSCRIPTS=${MOBIUS_TRANSCRIPT_PROOF:-0}
[[ $TRANSCRIPTS == 0 || $TRANSCRIPTS == 1 ]] || { echo "MOBIUS_TRANSCRIPT_PROOF must be 0 or 1" >&2; exit 2; }
[[ $REPLAY == 0 || $TRANSCRIPTS == 0 ]] \
  || { echo "the release replay and the transcript proof each prepare their own update; run one" >&2; exit 2; }
[[ $REPLAY == 0 || $REPLAY == 1 ]] || { echo "MOBIUS_RELEASE_REPLAY must be 0 or 1" >&2; exit 2; }
IMAGE=ghcr.io/mobius-os/mobius
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
STATUS=/var/lib/mobius-rebuild/status.json
ENV_FILE=$(mktemp /tmp/mobius-host-helper.XXXXXX.env)
SEED=$(mktemp -d /tmp/mobius-seed.XXXXXX)/checkout
export COMPOSE_PROJECT_NAME=mobius

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

fail() {
  echo "host helper: $*" >&2
  systemctl --no-pager status mobius-rebuild.service mobius-rebuild.path >&2 || true
  journalctl --no-pager -u mobius-rebuild.service -n 80 >&2 || true
  cat "$STATUS" >&2 2>/dev/null || true
  local cid
  if cid=$(app_container); then docker logs "$cid" --tail 60 >&2 2>&1 || true; fi
  exit 1
}

# The original project is stable; admission containers have an isolated Compose
# project per attempt. Never select by the historical physical name or take the
# first match: two running owners is an error, including across both queries.
app_container() {
  local legacy admitted ids
  legacy=$(docker ps --no-trunc -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter label=com.docker.compose.service=app) || return 1
  admitted=$(docker ps --no-trunc -q \
    --filter "label=io.mobius.admission.project=$COMPOSE_PROJECT_NAME") || return 1
  ids=$(printf '%s\n%s\n' "$legacy" "$admitted" | sed '/^$/d' | sort -u)
  if [[ ! $ids =~ ^[0-9a-f]{64}$ ]]; then
    echo "host helper: expected exactly one running app identity, found: $ids" >&2
    return 1
  fi
  printf '%s\n' "$ids"
}

field() {  # <json-file> <python expression over d>
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}

revision_of() {  # <sha>: inspect the requested release, not this harness's checkout
  git -C "$ROOT" show "$1:scripts/mobius-rebuild-host.py" \
    | sed -n 's/^WORKER_REVISION = \([0-9][0-9]*\)$/\1/p'
}

active_revision() {
  python3 -c 'import json; print(json.load(open("/var/lib/mobius-rebuild/workers.json"))["active"]["revision"])'
}

wait_status() {  # <nonce> <state>: wait until the root status names this request
  for _ in $(seq 1 240); do
    if [[ -f $STATUS ]] && [[ $(field "$STATUS" 'd.get("request_nonce")') == "$1" ]]; then
      local state; state=$(field "$STATUS" 'd.get("state")')
      [[ $state == "$2" ]] && return 0
      case "$state" in failed|rolled_back|needs_recovery|no_change) fail "request ended $state";; esac
    fi
    sleep 5
  done
  fail "request $1 did not reach $2 within 20 minutes"
}

queue() {  # <sha>: write the request exactly as the app does; prints its nonce
  local nonce; nonce=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  docker exec -u mobius "$(app_container)" sh -c "
    printf '%s' '{\"version\":2,\"expected_sha\":\"$1\",\"nonce\":\"$nonce\"}' \
      > /data/mobius-rebuild/inbox/.request.tmp &&
    mv /data/mobius-rebuild/inbox/.request.tmp /data/mobius-rebuild/inbox/request.json" \
    || fail "the app could not queue a request"
  echo "$nonce"
}

replaced_with() {  # <sha>: the container runs that image (not necessarily its source)
  [[ $(docker inspect -f '{{.Image}}' "$(app_container)") == $(docker image inspect -f '{{.Id}}' "$IMAGE:sha-$1") ]] \
    || fail "the running container is not sha-$1"
  [[ $(docker exec "$(app_container)" curl -fsS http://127.0.0.1:8000/api/version \
        | python3 -c 'import json, sys; print(json.load(sys.stdin).get("sha"))') == "$1" ]] \
    || fail "the container's image does not report sha-$1"
}

served() {  # prints "<source> <sha>": what the entrypoint selected for uvicorn
  docker exec "$(app_container)" sh -c 'printf "%s %s\n" "$(cat /tmp/serving-source)" "$(cat /tmp/serving-sha)"'
}

serves_source() {  # <source> <sha>: the served tree is that source and contains that release
  local source sha
  read -r source sha < <(served)
  [[ $source == "$1" ]] || fail "the container serves its $source tree, not $1 (served $sha)"
  if [[ $source == baked ]]; then
    [[ $sha == "$2" ]] || fail "the baked floor is $sha, not $2"
  else
    # A prepared update may be a local merge commit, so ask the served clone.
    docker exec -u mobius "$(app_container)" git -C /data/platform merge-base --is-ancestor "$2" "$sha" \
      || fail "the served /data/platform at $sha does not contain $2"
  fi
}

docker info >/dev/null || fail "Docker is unavailable"
# This test uses production unit/container names; never attach it to existing data.
if docker container inspect mobius >/dev/null 2>&1 \
   || docker volume inspect mobius_app_data >/dev/null 2>&1 \
   || [[ -n $(docker ps -aq --filter "label=io.mobius.admission.project=$COMPOSE_PROJECT_NAME") ]] \
   || [[ -n $(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
       --filter label=com.docker.compose.service=app) ]]; then
  echo "host helper: use a fresh disposable host, not an existing installation" >&2
  exit 2
fi
for path in /etc/mobius-rebuild /var/lib/mobius-rebuild \
  /usr/local/libexec/mobius-rebuild-host /usr/local/libexec/mobius-boot-admission.py \
  /usr/local/libexec/mobius-manual-cutover.py /etc/systemd/system/mobius-rebuild.service \
  /etc/systemd/system/mobius-rebuild.path /etc/systemd/system/mobius-rebuild-reconcile.service \
  /etc/systemd/system/mobius-rebuild-reconcile.timer; do
  if [[ -e $path || -L $path ]]; then
    echo "host helper: existing helper path $path; use a fresh disposable host" >&2
    exit 2
  fi
done

echo "host helper: installed at sha-${PREVIOUS:0:12}, updating to sha-${TARGET:0:12}"
git -C "$ROOT" worktree add --detach -q "$SEED" "$PREVIOUS"
if [[ ! -f $SEED/scripts/mobius-rebuild-launcher.py ]]; then
  [[ $REPLAY == 0 ]] || fail "replay requires a previous release with the launcher"
  echo "host helper: sha-${PREVIOUS:0:12} predates the launcher; nothing to prove yet"
  exit 0
fi
seeded=$(revision_of "$PREVIOUS")
target_revision=$(revision_of "$TARGET")
[[ $seeded =~ ^[0-9]+$ && $target_revision =~ ^[0-9]+$ ]] || fail "missing worker revision"
if [[ $REPLAY == 1 ]]; then
  (( target_revision > seeded )) || fail "replay must exercise a newer worker"
  git -C "$ROOT" cat-file -e "$TARGET:backend/app/app_setup.py" \
    || fail "target predates dependency restoration"
  docker pull "$IMAGE:sha-$TARGET"
  # A fresh target must lack BOTH test dependencies; otherwise this is no proof.
  docker run --rm --network none --entrypoint sh "$IMAGE:sha-$TARGET" -c '
    ! command -v figlet && python3 -c "import importlib.util; assert importlib.util.find_spec(\"pyfiglet\") is None"
  ' || fail "test packages already exist in the target image"
fi
printf 'SECRET_KEY=host-helper-regression-key-0123456789abcdef\nDOMAIN=localhost\n' >"$ENV_FILE"
chmod 0600 "$ENV_FILE"


# Transcript proof helpers (MOBIUS_TRANSCRIPT_PROOF=1). The probe runs the
# serving release's own code; waits poll observable state only.
TPROOF=$(mktemp -d /tmp/mobius-transcript-proof.XXXXXX)
served_backend() {  # <exact CID>: the serving uvicorn process's working directory
  # As the server's own user: the container has no CAP_SYS_PTRACE, so root
  # cannot read another user's /proc/<pid>/cwd.
  docker exec -u mobius "$1" sh -c 'readlink "/proc/$(pgrep -n -u mobius -f "/bin/uvicorn app\.main:app")/cwd"'
}
tprobe() {  # <command>: runs in the code the server runs; any error fails at once
  local backend output cid
  cid=$(app_container) || fail "cannot identify the active app"
  backend=$(served_backend "$cid") && [[ -n $backend ]] || fail "cannot locate the serving uvicorn process"
  docker cp "$ROOT/scripts/transcript_rollback_probe.py" "$cid:/tmp/probe.py"
  if ! output=$(docker exec -u mobius -w "$backend" -e PYTHONPATH="$backend" \
      "$cid" python3 /tmp/probe.py "$1" 2>"$TPROOF/probe.err"); then
    cat "$TPROOF/probe.err" >&2
    fail "transcript probe '$1' failed in $backend"
  fi
  printf '%s\n' "$output"
}
tconverge() {  # waits only on a valid "still pending" answer
  local answer
  while :; do
    answer=$(tprobe pending)
    case $answer in
      '{"pending": 0}') return 0 ;;
      '{"pending": '[1-9]*'}') sleep 1 ;;
      *) fail "unexpected conversion answer: $answer" ;;
    esac
  done
}
tmarks() {  # <expected json list>: chats without a conversion marker, exactly
  local marks
  marks=$(tprobe unconverted)
  python3 - "$marks" "$1" <<'PY' || fail "unconverted chats differ: $marks (expected $1)"
import json, sys
state, expected = json.loads(sys.argv[1]), json.loads(sys.argv[2])
assert state["unconverted"] == expected, state["unconverted"]
assert set(state["purged_leftovers"].values()) == {0}, state["purged_leftovers"]
PY
}
tsame() {  # <label> <expected.json> <actual.json>: the damaged chat may become its placeholder
  python3 - "$2" "$3" "$1" <<'PY' || fail "transcripts differ"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
a.pop("damaged", None); damaged = b.pop("damaged", None)
assert damaged is None or damaged["messages"][0].get("transcript_damage") is True, damaged
# Canonical text, so 1 / 1.0 / True, -0.0 / 0.0 and NaN are distinguished.
a, b = ({k: json.dumps(v, sort_keys=True) for k, v in d.items()} for d in (a, b))
assert a == b, f"{sys.argv[3]}: {sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))[:5]}"
PY
}

echo "1. the previous release runs as an owner deploys it"
cd "$SEED"
MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" docker compose --env-file "$ENV_FILE" \
  up -d --no-build --no-deps app
for _ in $(seq 1 60); do
  [[ $(docker inspect -f '{{.State.Health.Status}}' "$(app_container)" 2>/dev/null) == healthy ]] && break
  sleep 5
done
[[ $(docker inspect -f '{{.State.Health.Status}}' "$(app_container)") == healthy ]] \
  || fail "the previous release did not become healthy"
# The worker's chat drain authenticates with the owner's service token.
docker exec "$(app_container)" curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d '{"username":"owner","password":"host-helper-owner-password"}' \
  http://127.0.0.1:8000/api/auth/setup || fail "owner setup failed"
for _ in $(seq 1 30); do
  docker exec "$(app_container)" test -s /data/service-token.txt && break
  sleep 2
done
docker exec "$(app_container)" test -s /data/service-token.txt || fail "the instance has no service token"
if [[ $TRANSCRIPTS == 1 ]]; then
  tprobe seed >/dev/null
  tprobe dump >"$TPROOF/seeded.json"
fi

echo "2. the owner installs the helper once, from that checkout"
scripts/install-rebuild-helper.sh || fail "the installer failed"
seed_launcher=$(git -C "$ROOT" show "$PREVIOUS:scripts/mobius-rebuild-launcher.py" \
  | sed -n 's/^LAUNCHER_REVISION = \([0-9]*\)$/\1/p')
[[ $(field "$STATUS" 'd.get("launcher_revision")') == "$seed_launcher" ]] \
  || fail "the installed helper is not the previous release's launcher"
[[ $(active_revision) == "$seeded" ]] || fail "the installed worker is not revision $seeded"

if [[ $REPLAY == 1 ]]; then
  echo "   prepare accepted declarations and the reviewed source update"
  docker exec -u mobius "$(app_container)" mkdir -p /data/customizations
  docker cp "$ROOT/scripts/fixtures/release-replay/." "$(app_container):/data/customizations/"
  docker exec -u root "$(app_container)" chown -R mobius:mobius /data/customizations
  # Install only in the disposable OLD container, never in its image. The new
  # container must restore them automatically from the persistent declaration.
  docker exec -u mobius "$(app_container)" sudo -n apt-get update --error-on=any
  docker exec -u mobius "$(app_container)" sudo -n apt-get install --yes --no-remove figlet
  docker exec -u mobius "$(app_container)" sh /data/customizations/restore-python.sh apply
  docker exec -u mobius "$(app_container)" sh /data/customizations/restore-python.sh check
  docker exec -u mobius "$(app_container)" figlet replay >/dev/null
  docker exec -u mobius "$(app_container)" git -C /data/platform fetch origin "$TARGET"
  docker exec -i -u mobius -w /data/platform/backend "$(app_container)" python3 - "$TARGET" <<'SOURCE'
import sys
from app import platform_update as pu
preview = pu.platform_update_preview(target_sha=sys.argv[1])
assert not preview["conflict_paths"], preview["conflict_paths"]
plan = {key: preview[key] for key in ("plan_id", "current_sha", "target_sha", "image_digest")}
prepared = pu.prepare_reviewed_update(**plan)
assert isinstance(prepared, dict) and prepared["state"] == "prepared", prepared
# This historical pair is restart-loadable; the real external cutover swaps it.
# Do not silently bypass the bound-operation protocol for an image-required plan.
assert not prepared["requires_image"], "choose a restart-loadable historical source pair"
SOURCE
fi
if [[ $TRANSCRIPTS == 1 ]]; then
  echo "   prepare the target source exactly as Settings does"
  bundle_ref=refs/transcript-proof/target-$$
  git -C "$ROOT" update-ref "$bundle_ref" "$TARGET"
  git -C "$ROOT" bundle create "$TPROOF/target.bundle" "$bundle_ref" >/dev/null 2>&1 \
    || fail "could not bundle the target"
  git -C "$ROOT" update-ref -d "$bundle_ref"
  docker cp "$TPROOF/target.bundle" "$(app_container):/tmp/target.bundle"
  docker exec "$(app_container)" chmod 0644 /tmp/target.bundle
  docker exec -u mobius "$(app_container)" git -C /data/platform fetch -q /tmp/target.bundle "$bundle_ref" \
    || fail "the previous release could not fetch the target"
  docker exec -i -u mobius -w /data/platform/backend "$(app_container)" python3 - "$TARGET" <<'SOURCE' \
    || fail "the previous release's updater did not prepare the target"
import sys
from app import platform_update as pu
preview = pu.platform_update_preview(target_sha=sys.argv[1])
assert not preview["conflict_paths"], preview["conflict_paths"]
plan = {key: preview[key] for key in ("plan_id", "current_sha", "target_sha", "image_digest")}
prepared = pu.prepare_reviewed_update(**plan)
assert isinstance(prepared, dict) and prepared["state"] == "prepared", prepared
SOURCE
fi
before=$(docker inspect -f '{{.Id}}' "$(app_container)")
echo "3. the app requests the target release"
nonce=$(queue "$TARGET")
wait_status "$nonce" succeeded
replaced_with "$TARGET"
[[ $(docker inspect -f '{{.Id}}' "$(app_container)") != "$before" ]] || fail "container was not replaced"
if [[ $TRANSCRIPTS == 1 ]]; then
  serves_source platform "$TARGET"
  tconverge
  tprobe dump >"$TPROOF/converted.json"
  tsame "the target converted every chat exactly" "$TPROOF/seeded.json" "$TPROOF/converted.json"
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a converted chat's legacy bytes are not its rows"
  tprobe write-new >/dev/null
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a mirror after target writes is not byte-exact"
  tprobe dump >"$TPROOF/target.json"
fi
if [[ $REPLAY == 1 ]]; then
  # Never call setup/rerun here: startup alone must restore the declarations.
  for _ in $(seq 1 120); do
    if docker exec "$(app_container)" python3 -c '
import json
from pathlib import Path
p = Path("/data/setup-status.json")
s = json.loads(p.read_text()) if p.exists() else {}
keys = ("apt", "instance:restore-python.sh")
raise SystemExit(0 if all(s.get(k, {}).get("state") == "ready" for k in keys) else 1)
'; then break; fi
    sleep 5
  done
  docker exec -u mobius "$(app_container)" sh /data/customizations/restore-python.sh check \
    || fail "Python dependency was not automatically restored"
  docker exec -u mobius "$(app_container)" figlet replay >/dev/null || fail "apt dependency was not restored"
  docker exec -i "$(app_container)" python3 - "$TARGET" <<'VERIFY'
import json, subprocess, sys, urllib.request
from pathlib import Path
with urllib.request.urlopen("http://127.0.0.1:8000/api/version") as response:
    version = json.load(response)
assert version["serving_source"] == "platform", version
subprocess.run(["git", "-c", "safe.directory=/data/platform", "-C", "/data/platform",
                "merge-base", "--is-ancestor", sys.argv[1], version["served_sha"]], check=True)
states = json.loads(Path("/data/setup-status.json").read_text())
assert all(states[k]["state"] == "ready" for k in ("apt", "instance:restore-python.sh")), states
assert not Path("/data/.platform-prepared-update.json").exists(), "source activation unfinished"
assert Path("/data/customizations/preserved.txt").read_text() == "release-replay fixture\n"
print("release replay: target source loaded; apt and Python dependencies automatically restored")
VERIFY
fi
adoption=$(field "$STATUS" 'd.get("worker_adoption") or ""')
echo "   worker adoption: $adoption"
if (( target_revision > seeded )); then
  [[ $adoption == offered* ]] || fail "revision $target_revision was not offered"
fi

# Worker bytes cannot upgrade the frozen launcher/units. Exercise the same
# explicit host-maintenance step required of an owner, without starting another
# app replacement or pretending the image installed these files by itself.
previous_helper=$(git -C "$ROOT" show "$PREVIOUS:deployment/self-hosted-helper.required")
target_helper=$(git -C "$ROOT" show "$TARGET:deployment/self-hosted-helper.required")
nonce=
if (( target_helper > previous_helper )); then
  # Stop dispatch, not the app, so the old launcher's refusal can be observed
  # without a concurrent ExecStopPost status refresh obscuring the proof.
  systemctl stop mobius-rebuild.path
  if systemctl cat mobius-rebuild-reconcile.timer >/dev/null 2>&1; then
    systemctl stop mobius-rebuild-reconcile.timer
  fi
  for _ in $(seq 1 60); do
    systemctl is-active --quiet mobius-rebuild.service || break
    sleep 2
  done
  systemctl is-active --quiet mobius-rebuild.service && fail "previous replacement still running"
  nonce=$(queue "$PREVIOUS")
  if (( (seed_launcher == 1 && target_helper >= 4) ||
        (previous_helper < 5 && target_helper >= 5) )); then
    (( target_revision > seeded )) || fail "migration guard requires an offered newer worker"
    if (( previous_helper < 5 && target_helper >= 5 )); then
      [[ ! -e /usr/local/libexec/mobius-boot-admission.py &&
         ! -e /var/lib/mobius-rebuild/admission ]] || fail "legacy seed already has admission"
    fi
    prior_status=$(sha256sum "$STATUS")
    prior_request=$(docker exec "$(app_container)" sha256sum /data/mobius-rebuild/inbox/request.json)
    prior_container=$(docker inspect -f '{{.Id}}' "$(app_container)")
    /usr/local/libexec/mobius-rebuild-host run || fail "legacy launcher admission failed"
    [[ $(sha256sum "$STATUS") == "$prior_status" ]] || fail "legacy trial changed status"
    [[ $(docker inspect -f '{{.Id}}' "$(app_container)") == "$prior_container" ]] || fail "legacy trial replaced app"
    python3 - "$target_revision" <<'PENDING'
import json, sys
from pathlib import Path
index = json.loads(Path("/var/lib/mobius-rebuild/workers.json").read_text())
assert index["candidate"]["revision"] == int(sys.argv[1]), "candidate was consumed"
assert not Path("/var/lib/mobius-rebuild/transaction.json").exists(), "legacy trial journaled"
PENDING
    docker exec "$(app_container)" test -f /data/mobius-rebuild/inbox/request.json \
      || fail "legacy trial claimed the queued request"
    [[ $(docker exec "$(app_container)" sha256sum /data/mobius-rebuild/inbox/request.json) == "$prior_request" ]] \
      || fail "legacy trial changed the queued request"
  fi
  # Keep the exact guarded request but withhold dispatch until installed bytes
  # are verified. The installer enables its watcher/timer; neither should find
  # a request and race the migration assertions below.
  migration_cid=$(app_container) || fail "cannot identify app before migration"
  migration_started=$(docker inspect -f '{{.State.StartedAt}}' "$migration_cid")
  migration_request=$(docker exec "$migration_cid" sh -c 'sha256sum < /data/mobius-rebuild/inbox/request.json')
  docker exec -u mobius "$migration_cid" mv /data/mobius-rebuild/inbox/request.json \
    /data/mobius-rebuild/inbox/.migration-held.json || fail "cannot hold migration request"
  echo "   install the target's explicitly required host-helper migration"
  git -C "$SEED" checkout -q "$TARGET"
  "$SEED/scripts/install-rebuild-helper.sh" || fail "target helper migration failed"
  [[ $(app_container) == "$migration_cid" ]] || fail "installation replaced the healthy app"
  [[ $(docker inspect -f '{{.State.StartedAt}}' "$migration_cid") == "$migration_started" ]] \
    || fail "installation restarted the healthy app"
  if (( target_helper >= 5 )); then
    (( target_revision >= 12 )) || fail "admission requires worker revision 12 or newer"
    [[ $(cat "$SEED/deployment/self-hosted-helper.required") == "$target_helper" ]] \
      || fail "installed source helper marker differs"
    [[ $(field "$STATUS" 'd.get("launcher_revision")') == 2 ]] || fail "launcher2 not published"
    [[ $(active_revision) == "$target_revision" &&
       $(field "$STATUS" 'd.get("worker_revision")') == "$target_revision" ]] \
      || fail "target admission worker not installed"
    python3 - "$SEED" "$target_revision" <<'MIGRATION_VERIFY'
import hashlib, json, os, stat, sys
from pathlib import Path
source = Path(sys.argv[1])
for src, dst in (
    ("mobius-rebuild-launcher.py", "mobius-rebuild-host"),
    ("mobius-boot-admission.py", "mobius-boot-admission.py"),
    ("mobius-manual-cutover.py", "mobius-manual-cutover.py"),
):
    installed = Path("/usr/local/libexec") / dst
    st = installed.lstat()
    assert stat.S_ISREG(st.st_mode) and st.st_uid == st.st_gid == 0, installed
    assert stat.S_IMODE(st.st_mode) == 0o755, installed
    assert installed.read_bytes() == (source / "scripts" / src).read_bytes(), installed
state = Path("/var/lib/mobius-rebuild/admission")
st = state.lstat()
assert stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == 0
assert stat.S_IMODE(st.st_mode) == 0o700
root = state.parent
assert not os.path.lexists(root / "transaction.json"), "installation began a cutover"
active = json.loads((root / "workers.json").read_text())["active"]
assert active["revision"] == int(sys.argv[2])
expected = (source / "scripts/mobius-rebuild-host.py").read_bytes()
assert active["sha256"] == hashlib.sha256(expected).hexdigest()
assert (root / "workers" / active["file"]).read_bytes() == expected
MIGRATION_VERIFY
  fi
  [[ $(docker exec "$migration_cid" sh -c 'sha256sum < /data/mobius-rebuild/inbox/.migration-held.json') == "$migration_request" ]] \
    || fail "installation changed the held request"
  docker exec -u mobius "$migration_cid" mv /data/mobius-rebuild/inbox/.migration-held.json \
    /data/mobius-rebuild/inbox/request.json || fail "cannot resume preserved request"
fi

echo "4. the app requests the previous release again"
[[ -n $nonce ]] || nonce=$(queue "$PREVIOUS")
wait_status "$nonce" succeeded
replaced_with "$PREVIOUS"
expected=$(( target_revision > seeded ? target_revision : seeded ))
# The worker reports before it exits; the launcher settles after it.
for _ in $(seq 1 60); do
  if ! systemctl is-active --quiet mobius-rebuild.service \
     && [[ $(active_revision) == "$expected" ]]; then break; fi
  sleep 2
done
[[ $(active_revision) == "$expected" ]] \
  || fail "worker revision $expected is not active (active: $(active_revision))"
[[ $(field "$STATUS" 'd.get("worker_revision")') == "$expected" ]] \
  || fail "revision $expected did not perform the replacement"
if [[ $REPLAY == 1 ]]; then
  expected_hash=$(git -C "$ROOT" show "$TARGET:scripts/mobius-rebuild-host.py" | sha256sum | cut -d ' ' -f1)
  python3 - "$expected_hash" <<'WORKER_VERIFY'
import hashlib, json, sys
from pathlib import Path
root = Path("/var/lib/mobius-rebuild")
active = json.loads((root / "workers.json").read_text())["active"]
assert active["sha256"] == sys.argv[1], "active worker is not the reviewed release's worker"
assert hashlib.sha256((root / "workers" / active["file"]).read_bytes()).hexdigest() == sys.argv[1]
print("release replay: active worker bytes match the published target source")
WORKER_VERIFY
fi
if [[ $TRANSCRIPTS == 1 ]]; then
  # What the product serves here: normally the target source, because this
  # release is restart-loadable and an image replacement never reverts it;
  # the image's baked floor if that source cannot import on it.
  read -r step4_source step4_sha < <(served)
  echo "   the previous image serves its $step4_source tree at $step4_sha"
  case $step4_source in
    platform) serves_source platform "$TARGET" ;;
    baked) serves_source baked "$PREVIOUS" ;;
    *) fail "the previous image reports an unknown served source: $step4_source" ;;
  esac
  tprobe dump >"$TPROOF/previous-image.json"
  tsame "the previous image keeps every transcript exact" "$TPROOF/target.json" "$TPROOF/previous-image.json"
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a mirror is not byte-exact on the previous image"

  echo "5. a broken served tree falls back to the previous image's baked code"
  repaired=$(docker exec -u mobius "$(app_container)" git -C /data/platform rev-parse HEAD)
  # A committed edit that fails the import probe, as a broken agent edit would.
  docker exec -u mobius "$(app_container)" sh -c '
    cd /data/platform &&
    printf "\nraise ImportError(\"transcript proof: broken served tree\")\n" >> backend/app/main.py &&
    git -c user.name=transcript-proof -c user.email=transcript-proof@localhost \
      commit -q -m "Break the served tree" -- backend/app/main.py' \
    || fail "could not commit the broken served tree"
  docker restart "$(app_container)" >/dev/null
  until docker exec "$(app_container)" curl -fsS -o /dev/null http://127.0.0.1:8000/api/ready 2>/dev/null; do
    [[ $(docker inspect -f '{{.State.Running}}' "$(app_container)") == true ]] \
      || fail "the previous image stopped instead of serving its baked floor"
    sleep 2
  done
  serves_source baked "$PREVIOUS"
  tprobe dump >"$TPROOF/baked.json"
  tsame "the previous code reads every transcript exactly" "$TPROOF/target.json" "$TPROOF/baked.json"
  wrote=$(tprobe write-old)
  tprobe dump >"$TPROOF/previous-wrote.json"
  # The schema triggers mark exactly the chats a pre-rows previous release
  # changed; a row-based previous release keeps every chat converted.
  tmarks "$(python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["expect_unconverted"]))' "$wrote")"

  echo "6. the source is repaired and the target release returns"
  docker exec -u mobius "$(app_container)" git -C /data/platform reset -q --hard "$repaired" \
    || fail "could not repair the served tree"
  nonce=$(queue "$TARGET")
  wait_status "$nonce" succeeded
  replaced_with "$TARGET"
  serves_source platform "$TARGET"
  tconverge
  tprobe dump >"$TPROOF/final.json"
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a re-converted chat's legacy bytes are not its rows"
  tsame "the target re-converted the previous code's writes" "$TPROOF/previous-wrote.json" "$TPROOF/final.json"
  echo "host helper: transcripts survived target -> previous image -> previous code -> target"
fi
echo "host helper: worker revision $seeded -> $expected arrived with the image, replaced the container, and is active"
