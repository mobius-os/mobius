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
#      release): every transcript stays exact; beside that admitted app, raw
#      Compose conflicts and the target's own helper-managed deploy reconciles
#      the bundled proxy without touching the app, its gate or its ledger;
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

# BEGIN CONTROLLER QUIESCENCE
# With dispatch stopped, a oneshot controller started earlier can still be
# activating (its worker runs), deactivating (ExecStopPost reconciles), or hold
# a queued start job while inactive. `systemctl is-active` calls none of those
# active, so positively require each controller service to be inactive or
# failed with no job. This only observes: running recovery is never stopped.
CONTROLLER_UNITS=(mobius-rebuild.service mobius-rebuild-reconcile.service)
controller_unit_state() {  # <unit>: sets UNIT_ACTIVE/UNIT_JOB; fails on unreadable evidence
  local info line key value load= seen=
  UNIT_ACTIVE= UNIT_JOB=
  info=$(systemctl show "$1" --all --property=LoadState --property=ActiveState --property=Job) \
    || { echo "systemctl show $1 failed" >&2; return 1; }
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    [[ $line == *=* ]] || { echo "$1: unparseable systemd line: $line" >&2; return 1; }
    key=${line%%=*} value=${line#*=}
    [[ " $seen " != *" $key "* ]] || { echo "$1: duplicate systemd property $key" >&2; return 1; }
    seen+=" $key"
    case $key in
      LoadState) load=$value ;;
      ActiveState) UNIT_ACTIVE=$value ;;
      Job) UNIT_JOB=$value ;;
      *) echo "$1: unexpected systemd property $key" >&2; return 1 ;;
    esac
  done <<<"$info"
  # An older seed may not install the reconcile unit: not-found is inactive.
  [[ $load == loaded || $load == not-found ]] \
    || { echo "$1: unusable LoadState '${load}'" >&2; return 1; }
  [[ $UNIT_ACTIVE =~ ^[a-z-]+$ ]] || { echo "$1: unusable ActiveState '${UNIT_ACTIVE}'" >&2; return 1; }
  [[ $seen == *" Job"* && $UNIT_JOB =~ ^[0-9]*$ ]] \
    || { echo "$1: unusable Job '${UNIT_JOB}'" >&2; return 1; }
}
wait_controllers_quiescent() {  # <attempts> <interval-seconds>
  local attempts=$1 interval=$2 attempt unit busy
  for (( attempt = 1; attempt <= attempts; attempt++ )); do
    busy=
    for unit in "${CONTROLLER_UNITS[@]}"; do
      controller_unit_state "$unit" || fail "cannot prove $unit is quiescent"
      if [[ $UNIT_ACTIVE != inactive && $UNIT_ACTIVE != failed ]] \
          || [[ -n $UNIT_JOB && $UNIT_JOB != 0 ]]; then
        busy+=" $unit=$UNIT_ACTIVE/job:${UNIT_JOB:-none}"
      fi
    done
    if [[ -z $busy ]]; then return 0; fi
    if (( attempt < attempts )); then sleep "$interval"; fi
  done
  fail "previous replacement still running or queued after $attempts checks:$busy"
}
# END CONTROLLER QUIESCENCE

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

single_data_writer() {  # <owner CID>: no other container mounts its /data writable
  # Inspect mounts too: label-only discovery could miss an unwrapped writer.
  docker inspect $(docker ps --no-trunc -q) | python3 -c '
import json, sys
items = json.load(sys.stdin)
owner = next(c for c in items if c["Id"] == sys.argv[1])
data = next(m["Source"] for m in owner["Mounts"] if m["Destination"] == "/data")
writers = [c["Id"] for c in items if any(m["Source"] == data and m.get("RW") for m in c["Mounts"])]
assert writers == [owner["Id"]], writers
' "$1"
}

prove_compose_name_fence() {
  local cid started current_name output status mode
  cid=$(app_container) || fail "cannot identify managed app for Compose proof"
  started=$(docker inspect -f '{{.State.StartedAt}}' "$cid")
  current_name=$(docker inspect -f '{{.Name}}' "$cid")
  [[ $current_name == "$original_app_name" ]] || fail "managed replacement changed deployment name"
  [[ $(docker inspect -f '{{index .Config.Labels "io.mobius.admission.token"}}' "$cid") =~ ^[0-9a-f]{32}$ ]] \
    || fail "Compose fence proof requires an admitted app"
  # Fetch dependencies before the negative assertions: registry failure must
  # never masquerade as the intended daemon name conflict.
  MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" docker compose --env-file "$ENV_FILE" pull caddy \
    || fail "could not prepare Caddy for the raw Compose proof"
  output="$TPROOF/compose-name-conflict.log"
  for mode in all caddy force; do
    local -a selection=()
    case $mode in caddy) selection=(caddy);; force) selection=(--force-recreate app);; esac
    if MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" timeout 90 docker compose --env-file "$ENV_FILE" \
        up -d --no-build --pull never "${selection[@]}" >"$output" 2>&1; then
      fail "raw Compose $mode unexpectedly succeeded beside a managed app"
    else
      status=$?
    fi
    [[ $status != 124 && $status != 137 ]] || fail "raw Compose $mode timed out instead of reporting a name conflict"
    grep -Eiq 'container name.*already in use|name.*is already in use' "$output" \
      || { cat "$output" >&2; fail "raw Compose $mode failed for a reason other than the name fence"; }
    [[ $(app_container) == "$cid" ]] || fail "raw Compose $mode changed app ownership"
    [[ $(docker inspect -f '{{.State.StartedAt}}' "$cid") == "$started" ]] \
      || fail "raw Compose $mode restarted the managed app"
    single_data_writer "$cid" || fail "raw Compose $mode created a second writer on the app data"
  done
  echo "   unchanged raw Compose commands conflict without a second app or restart"
}

# A helper-managed deploy replaces only the admitted app, then reconciles the
# bundled Caddy by itself. Run the target's own deploy-prod.sh functions,
# extracted verbatim from a checkout of <target> (never a copy in this file or
# the previous release's script), against this real daemon. Caddy must be
# created, then recreated for a changed routing environment and a changed image
# reference, while the admitted app's identity, start, gate and ledger stay
# exactly as they were. Synthetic *.localhost names keep Caddy on its internal
# CA (no ACME or DNS); the disposable owner override drops host port bindings,
# which are not under test and would collide with this runner's registry.
PROXY_CANDIDATE=""
PROXY_FUNCTIONS=""
proxy_env() {  # <domain> <gateway> <command...>: the deploy's exported prod environment
  local domain=$1 gateway=$2; shift 2
  (cd "$PROXY_CANDIDATE" && env -u COMPOSE_FILE -u COMPOSE_PROFILES \
    SECRET_KEY=host-helper-regression-key-0123456789abcdef MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" \
    DOMAIN="$domain" MOBIUS_SERVICE_GATEWAY_ORIGIN="$gateway" "$@")
}
proxy_override() {  # <active|disabled> [image]: the owner override for this step
  local extra=""
  if [[ $1 == disabled ]]; then extra+=$'    profiles: ["disabled"]\n'; fi
  if [[ -n ${2:-} ]]; then extra+="    image: $2"$'\n'; fi
  printf 'services:\n  caddy:\n    ports: !reset []\n%s' "$extra" \
    >"$PROXY_CANDIDATE/docker-compose.override.yml"
}
proxy_containers() {  # every bundled proxy container of the project, running or not
  docker ps -a --no-trunc -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter label=com.docker.compose.service=caddy
}
run_proxy_reconcile() {  # <domain> <gateway> <log>: the target's function, as its prod self-hosted helper path calls it
  proxy_env "$1" "$2" timeout 180 bash -c '
    set -euo pipefail
    C_RED="" C_GREEN="" C_DIM="" C_YELLOW="" C_RESET=""
    source "$1"
    TARGET=prod HELPER_MANAGED=1 EDGE_TOPOLOGY=self-hosted
    COMPOSE_ARGS=(-p mobius)
    reconcile_bundled_proxy' reconcile "$PROXY_FUNCTIONS" >"$3" 2>&1 \
    || { cat "$3" >&2; fail "the target's reconcile_bundled_proxy failed"; }
}
proxy_authority() {  # <app CID>: identity, gate and ledger state the proxy must never change
  local data
  data=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$1")
  docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}} {{.Image}} {{index .Config.Labels "io.mobius.admission.token"}}' "$1"
  python3 - "$data" <<'PROXY_AUTHORITY'
import hashlib, sys
from pathlib import Path
gate = Path("/var/lib/mobius-rebuild/admission")
assert any(p.is_file() for p in gate.rglob("*")), "the admitted app has no gate state"
for root in (gate, Path(sys.argv[1]) / ".restart-ledger"):
    if not root.is_dir():
        print(root, "absent")
        continue
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        regular = path.is_file() and not path.is_symlink()
        print(path, oct(st.st_mode), st.st_uid, st.st_size,
              hashlib.sha256(path.read_bytes()).hexdigest() if regular else "-")
print("transaction", Path("/var/lib/mobius-rebuild/transaction.json").exists())
PROXY_AUTHORITY
}
proxy_serves() {  # <proxy CID> <domain> <gateway> <stale domain> <stale gateway> <image>
  local cid=$1 config=""
  for _ in $(seq 1 30); do
    if [[ $(docker inspect -f '{{.State.Running}}' "$cid") == true ]] &&
       config=$(docker exec "$cid" wget -qO- http://127.0.0.1:2019/config/ 2>/dev/null) &&
       [[ -n $config ]]; then
      break
    fi
    config=""
    sleep 2
  done
  [[ -n $config ]] || { docker logs --tail 40 "$cid" >&2 || true; fail "the bundled proxy never loaded its configuration"; }
  python3 - "$(docker inspect "$cid")" "$config" "${@:2}" <<'PROXY_CONFIG' \
    || fail "the bundled proxy does not run the reconciled environment"
import json, sys
item = json.loads(sys.argv[1])[0]
config = json.loads(sys.argv[2])
domain, gateway, stale_domain, stale_gateway, image = sys.argv[3:]
env = item["Config"]["Env"]
for value in (f"DOMAIN={domain}", f"FRONTEND_ORIGIN=https://{domain}",
              f"MOBIUS_SERVICE_GATEWAY_ORIGIN={gateway}"):
    assert value in env, (value, env)
assert item["Config"]["Image"] == image, item["Config"]["Image"]
assert item["State"]["Restarting"] is False and item["RestartCount"] == 0, item["State"]
assert not item["HostConfig"].get("PortBindings"), item["HostConfig"]["PortBindings"]
# What Caddy actually adapted from the Caddyfile, not only the container env.
hosts = {host for server in config["apps"]["http"]["servers"].values()
         for route in server.get("routes", []) for match in route.get("match", [])
         for host in match.get("host", [])}
gateway_host = gateway.split("://", 1)[1]
assert {domain, gateway_host} <= hosts, hosts
assert not {stale_domain, stale_gateway.split("://", 1)[-1]} & hosts, hosts
PROXY_CONFIG
}

prove_bundled_proxy_reconcile() {
  local app app_state authority log proxy previous_proxy started rendered tag volumes
  local old_domain=proxy-old.localhost old_gateway=https://services-old.localhost
  local new_domain=proxy-new.localhost new_gateway=https://services-new.localhost
  app=$(app_container) || fail "cannot identify managed app for the proxy proof"
  [[ $(docker inspect -f '{{index .Config.Labels "io.mobius.admission.token"}}' "$app") =~ ^[0-9a-f]{32}$ ]] \
    || fail "the proxy proof requires an admitted app"
  app_state=$(docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' "$app")
  authority=$(proxy_authority "$app") || fail "cannot record the app's gate and ledger"
  # The raw Compose refusals above must not have left a proxy behind: this
  # proof starts without one, so its first reconcile really creates it.
  [[ -z $(proxy_containers) ]] || fail "raw Compose left a bundled proxy container"

  PROXY_CANDIDATE=$(mktemp -d /tmp/mobius-proxy-candidate.XXXXXX)/checkout
  git -C "$ROOT" worktree add --detach -q "$PROXY_CANDIDATE" "$TARGET" \
    || fail "cannot check out the target for the proxy proof"
  [[ $(git -C "$PROXY_CANDIDATE" rev-parse HEAD) == "$TARGET" ]] || fail "the proxy candidate is not the target"
  git -C "$ROOT" show "$TARGET:scripts/deploy-prod.sh" | cmp -s - "$PROXY_CANDIDATE/scripts/deploy-prod.sh" \
    || fail "the proxy candidate's deploy script is not the target's"
  PROXY_FUNCTIONS="$TPROOF/target-deploy-proxy.sh"
  python3 - "$PROXY_CANDIDATE/scripts/deploy-prod.sh" >"$PROXY_FUNCTIONS" <<'PROXY_EXTRACT' \
    || fail "the target's deploy-prod.sh has no single bundled proxy reconcile"
import re, sys
text = open(sys.argv[1]).read()
for name in ("info", "ok", "fail", "intent", "external_prod_caddy_running", "reconcile_bundled_proxy"):
    # One-line logger, or a block that ends at its first column-0 brace.
    found = [m.group(0) for m in re.finditer(
        r"^%s\(\) +\{(?: [^\n]*\}|\n.*?\n\})$" % re.escape(name), text, re.M | re.S)]
    assert len(found) == 1, (name, len(found))
    print(found[0])
PROXY_EXTRACT
  echo "   target deploy functions: $(sha256sum <"$PROXY_FUNCTIONS" | cut -c1-12)"

  # Never bind host ports, even for a moment: check the rendered model first.
  proxy_override active
  rendered=$(proxy_env "$old_domain" "$old_gateway" docker compose -p mobius config --format json) \
    || fail "cannot render the target's Compose model"
  python3 -c '
import json, sys
caddy = json.loads(sys.argv[1])["services"]["caddy"]
assert not caddy.get("ports"), caddy.get("ports")
print(caddy["image"])' "$rendered" >"$TPROOF/proxy-image" || fail "the proxy override still publishes ports"
  # Fetch the image first: a registry failure must not look like a reconcile failure.
  proxy_env "$old_domain" "$old_gateway" docker compose -p mobius pull caddy \
    || fail "could not prepare Caddy for the proxy proof"

  echo "   a profile-disabled bundled proxy stays absent"
  log="$TPROOF/proxy-disabled.log"
  proxy_override disabled
  run_proxy_reconcile "$old_domain" "$old_gateway" "$log"
  grep -Fq "no active bundled proxy service; leaving the proxy unchanged" "$log" \
    || { cat "$log" >&2; fail "a disabled proxy was not left alone"; }
  ! grep -Fq -- "up -d" "$log" || fail "a disabled proxy was named to Compose"
  [[ -z $(proxy_containers) ]] || fail "the reconcile activated a disabled proxy"

  local step domain gateway stale_domain stale_gateway image
  tag=mobius-proxy-proof/caddy:changed
  docker tag "$(cat "$TPROOF/proxy-image")" "$tag" || fail "cannot tag the changed proxy image"
  previous_proxy=""
  for step in create environment image unchanged; do
    domain=$new_domain gateway=$new_gateway stale_domain=$old_domain stale_gateway=$old_gateway
    image=$tag
    case $step in
      create) domain=$old_domain gateway=$old_gateway stale_domain=$new_domain
              stale_gateway=$new_gateway image=$(cat "$TPROOF/proxy-image") ;;
      environment) image=$(cat "$TPROOF/proxy-image") ;;
    esac
    if [[ $image == "$tag" ]]; then proxy_override active "$tag"; else proxy_override active; fi
    log="$TPROOF/proxy-$step.log"
    run_proxy_reconcile "$domain" "$gateway" "$log"
    grep -Fq "docker compose -p mobius up -d --no-deps caddy" "$log" \
      && grep -Fq "bundled proxy reconciled without touching the app" "$log" \
      || { cat "$log" >&2; fail "the $step reconcile did not run the bundled proxy path"; }
    proxy=$(proxy_containers)
    [[ $proxy =~ ^[0-9a-f]{64}$ ]] || fail "the $step reconcile left proxies: $proxy"
    proxy_serves "$proxy" "$domain" "$gateway" "$stale_domain" "$stale_gateway" "$image"
    case $step in
      create) ;;
      unchanged)
        [[ $proxy == "$previous_proxy" &&
           $(docker inspect -f '{{.State.StartedAt}}' "$proxy") == "$started" ]] \
          || fail "an unchanged reconcile recreated or restarted the proxy" ;;
      *) [[ $proxy != "$previous_proxy" ]] || fail "the changed $step was not applied to the proxy" ;;
    esac
    [[ $(app_container) == "$app" ]] || fail "the $step reconcile changed app ownership"
    [[ $(docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' "$app") == "$app_state" ]] \
      || fail "the $step reconcile restarted the app"
    single_data_writer "$app" || fail "the $step reconcile created a second writer on the app data"
    previous_proxy=$proxy
    started=$(docker inspect -f '{{.State.StartedAt}}' "$proxy")
  done
  [[ $(docker inspect -f '{{.Image}}' "$proxy") == $(docker image inspect -f '{{.Id}}' "$(cat "$TPROOF/proxy-image")") ]] \
    || fail "the changed image reference is not the pulled proxy image"
  [[ $(proxy_authority "$app") == "$authority" ]] || fail "the proxy reconcile changed app identity, gate or ledger"

  # Leave later phases the host they had: no proxy, image tag or checkout.
  volumes=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$proxy")
  docker rm -f "$proxy" >/dev/null || fail "cannot remove the proof proxy"
  # Intentionally split: these are the proxy's own named volumes.
  # shellcheck disable=SC2086
  [[ -z $volumes ]] || docker volume rm $volumes >/dev/null || fail "cannot remove the proof proxy volumes"
  docker image rm "$tag" >/dev/null || fail "cannot remove the changed proxy tag"
  git -C "$ROOT" worktree remove --force "$PROXY_CANDIDATE" || fail "cannot remove the proxy candidate"
  [[ -z $(proxy_containers) ]] || fail "a bundled proxy outlived its proof"
  [[ $(app_container) == "$app" ]] && single_data_writer "$app" \
    || fail "the proxy cleanup disturbed the app"
  echo "   the target's deploy reconciled the bundled proxy; the admitted app, gate and ledger never changed"
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
  # cannot read another user's /proc/<pid>/cwd. -o, not -n: the server is the
  # oldest match; a child it is starting (say, a build in the frontend folder)
  # carries the server's command line in its own cwd until it execs.
  docker exec -u mobius "$1" sh -c 'readlink "/proc/$(pgrep -o -u mobius -f "/bin/uvicorn app\.main:app")/cwd"'
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
original_app_name=$(docker inspect -f '{{.Name}}' "$(app_container)")
# The worker's chat drain authenticates with the owner's service token.
# A release with a one-time owner setup code issues it on the status read;
# older releases have none and ignore the field.
docker exec "$(app_container)" curl -fsS -o /dev/null http://127.0.0.1:8000/api/auth/setup/status || true
setup_code=$(docker exec -u mobius "$(app_container)" cat /data/setup-code 2>/dev/null | tr -d '[:space:]' || true)
docker exec "$(app_container)" curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"owner\",\"password\":\"host-helper-owner-password\",\"setup_code\":\"$setup_code\"}" \
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
  wait_controllers_quiescent 60 2
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
config = Path("/etc/mobius-rebuild/config.v4.json")
st = config.lstat()
assert stat.S_ISREG(st.st_mode) and st.st_uid == st.st_gid == 0, config
assert stat.S_IMODE(st.st_mode) == 0o600, config
assert json.loads(config.read_text())["version"] == 4
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
wait_controllers_quiescent 60 2
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
  if (( target_helper >= 5 )); then
    prove_compose_name_fence
    # After the raw Compose refusals, so no proxy can mask their app conflict.
    prove_bundled_proxy_reconcile
  fi
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
