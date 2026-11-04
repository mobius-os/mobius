#!/usr/bin/env bash
# Real mixed self-hosted upgrade: <target> needs BOTH a new image and a newer
# installed host helper than <previous>. scripts/test-upgrade-path.sh
# deliberately fails such a span, because Settings alone cannot finish it.
# This proves the rest of that contract on a disposable host:
#
#   sudo scripts/test-mixed-host-upgrade.sh <previous-sha> <target-sha>
#
# 0. Rebuild a minimal environment (no proxies, Docker/Compose selectors, Git
#    or curl configuration, or deploy overrides) and refuse anything but a
#    fresh local disposable host, before the first host mutation.
# 1. Deploy <previous> from an operator clone as an owner does, prove the
#    running image, served source and installed helper are <previous>, set up
#    the owner, seed chats and a data sentinel.
# 2. Offer <target>. The previous release's Settings review must report
#    exactly this mixed span, and Update (rebuild) and Apply must each refuse
#    it without changing source, inbox, helper, image or container.
# 3. The operator pulls <target>. Its deploy-prod.sh must refuse while the
#    installed helper predates its root-pinned manual adapter, before a build.
# 4. sudo scripts/install-rebuild-helper.sh installs the current helper beside
#    the healthy previous app without restarting it.
# 5. sudo scripts/deploy-prod.sh builds <target>, installs its source through
#    the previous release's updater and cuts the app over through that helper.
#    Its fixed public probe names a host this proof maps to loopback, so the
#    deploy stops there: the cutover and every earlier deploy check are
#    observed, but its shell reload broadcast, build-cache cleanup and final
#    completion are skipped. This is a partial deploy, never reported whole.
# 6. Through the local Caddy (HTTPS with its internal CA) and inside the app:
#    the admitted app serves <target>'s image and source with the current
#    helper active; the volume, owner session, chats and sentinel survived;
#    only the helper migration stays owed, because the container cannot
#    observe the host helper.
#
# <previous> must have a published official image; deploy-prod.sh builds
# <target> from the public origin. Run only on a fresh disposable amd64 systemd
# host with a local Docker daemon, Compose v2, free ports 80/443, at least
# 15 GiB free for Docker, and a root-owned full-history checkout of this
# script. It uses production container, volume and unit names, installs
# root-owned helper files, publishes ports 80/443 and edits /etc/hosts.

set -euo pipefail
shopt -s inherit_errexit

PREVIOUS="${1:?previous sha}"
TARGET="${2:?target sha}"
# These values become Git refs and JSON request values below.
[[ $PREVIOUS =~ ^[0-9a-f]{40}$ && $TARGET =~ ^[0-9a-f]{40}$ && $PREVIOUS != "$TARGET" ]] \
  || { echo "two distinct full release SHAs are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

# BEGIN FRESH HOST PREFLIGHT
CLEAN_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Bash itself exports PWD, SHLVL, OLDPWD and _; everything else is ours.
ENV_ALLOWED=" PATH HOME LANG PWD SHLVL OLDPWD _ GIT_CONFIG_NOSYSTEM MOBIUS_MIXED_PROOF_HOME COMPOSE_PROJECT_NAME "
APP_VOLUMES=(mobius_app_data mobius_caddy_data mobius_caddy_config)
HELPER_PATHS=(/etc/mobius-rebuild /var/lib/mobius-rebuild
  /usr/local/libexec/mobius-rebuild-host /usr/local/libexec/mobius-boot-admission.py
  /usr/local/libexec/mobius-manual-cutover.py /etc/systemd/system/mobius-rebuild.service
  /etc/systemd/system/mobius-rebuild.path /etc/systemd/system/mobius-rebuild-reconcile.service
  /etc/systemd/system/mobius-rebuild-reconcile.timer)

refuse() { echo "mixed upgrade: $*" >&2; exit 2; }

# Observation only: every check either proves this is a fresh local disposable
# host or refuses before the first host mutation.
fresh_host_preflight() {  # <checkout root>
  local name out project
  for name in $(compgen -e); do
    [[ $ENV_ALLOWED == *" $name "* ]] || refuse "unexpected environment variable $name"
  done
  [[ $PATH == "$CLEAN_PATH" && $HOME == "${MOBIUS_MIXED_PROOF_HOME:-}" && $HOME == /tmp/mobius-mixed-home.* ]] \
    || refuse "the environment was not rebuilt for this proof"
  out=$(stat -c '%u %a' "$HOME") || refuse "cannot inspect $HOME"
  [[ $out == "0 700" ]] || refuse "the isolated HOME is not root-private"
  for name in "$1" "$1/.git"; do
    out=$(stat -c '%u %A' "$name") || refuse "cannot inspect $name"
    [[ $out =~ ^0\ d....-..-.$ ]] || refuse "$name must be a root-owned checkout no one else can write"
  done
  out=$(uname -m) || refuse "cannot read the architecture"
  [[ $out == x86_64 ]] || refuse "official images are amd64 only"
  out=$(systemctl is-system-running) || [[ $out == degraded ]] || refuse "systemd is not running (${out:-unknown})"
  out=$(docker context show) || refuse "cannot read the Docker context"
  [[ $out == default ]] || refuse "use the local default Docker context, not $out"
  out=$(docker context inspect default -f '{{.Endpoints.docker.Host}}') || refuse "cannot read the Docker endpoint"
  [[ $out == unix:///var/run/docker.sock ]] || refuse "use the local Docker daemon, not $out"
  out=$(docker info -f '{{.OSType}} {{.Architecture}}') || refuse "Docker is unavailable"
  [[ $out == "linux x86_64" ]] || refuse "the Docker daemon is $out"
  docker compose version >/dev/null || refuse "Docker Compose v2 is unavailable"
  out=$(docker ps -a --format '{{.Names}} {{.Label "com.docker.compose.project"}} {{.Label "io.mobius.admission.project"}}') \
    || refuse "cannot list containers"
  while read -r name project; do
    [[ -n $name ]] || continue
    [[ $name != mobius && $name != edge-caddy && " $project " != *" mobius "* ]] \
      || refuse "existing container $name; use a fresh disposable host"
  done <<<"$out"
  out=$(docker volume ls -q) || refuse "cannot list volumes"
  for name in "${APP_VOLUMES[@]}"; do
    ! grep -qx -- "$name" <<<"$out" || refuse "existing volume $name; use a fresh disposable host"
  done
  out=$(docker network ls --format '{{.Name}}') || refuse "cannot list networks"
  for name in mobius_default edge-mobius; do
    ! grep -qx -- "$name" <<<"$out" || refuse "existing network $name; use a fresh disposable host"
  done
  out=$(docker image ls -q mobius) || refuse "cannot list images"
  [[ -z $out ]] || refuse "an image named mobius already exists"
  out=$(ss -Hltnu '( sport = :80 or sport = :443 )') || refuse "cannot list listening ports"
  [[ -z $out ]] || refuse "ports 80/443 are already in use"
  for name in "${HELPER_PATHS[@]}"; do
    [[ ! -e $name && ! -L $name ]] || refuse "existing helper path $name; use a fresh disposable host"
  done
  out=$(docker info --format '{{.DockerRootDir}}') && out=$(df -PB1 "$out" | awk 'NR == 2 {print $4}') \
    || refuse "cannot read Docker free space"
  [[ $out =~ ^[0-9]+$ ]] && (( out >= 15 * 1024 * 1024 * 1024 )) \
    || refuse "Docker needs 15 GiB free for deploy-prod.sh's build"
}
# END FRESH HOST PREFLIGHT

# Never inherit proxies, Docker/Compose selectors, Git or curl configuration,
# or deploy overrides (ALLOW_*, FORCE_NOW, MOBIUS_IMAGE, ...): rerun this
# script in a constructed environment with a fresh root-private HOME. The
# preflight then rejects anything else that is still exported.
if [[ -z ${MOBIUS_MIXED_PROOF_HOME:-} ]]; then
  home=$(mktemp -d /tmp/mobius-mixed-home.XXXXXX)
  exec env -i PATH="$CLEAN_PATH" HOME="$home" LANG=C.UTF-8 GIT_CONFIG_NOSYSTEM=1 \
    MOBIUS_MIXED_PROOF_HOME="$home" /bin/bash "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
fi

IMAGE=ghcr.io/mobius-os/mobius
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
STATUS=/var/lib/mobius-rebuild/status.json
DOMAIN=localhost
export COMPOSE_PROJECT_NAME=mobius

fail() {
  echo "mixed upgrade: $*" >&2
  journalctl --no-pager -u mobius-rebuild.service -n 60 >&2 || true
  cat "$STATUS" >&2 2>/dev/null || true
  local cid
  if cid=$(app_container 2>/dev/null); then docker logs "$cid" --tail 60 >&2 2>&1 || true; fi
  exit 1
}

# Exactly one running app, whether Compose or the admission wrapper owns it.
app_container() {
  local legacy admitted ids
  legacy=$(docker ps --no-trunc -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter label=com.docker.compose.service=app) || return 1
  admitted=$(docker ps --no-trunc -q \
    --filter "label=io.mobius.admission.project=$COMPOSE_PROJECT_NAME") || return 1
  ids=$(printf '%s\n%s\n' "$legacy" "$admitted" | sed '/^$/d' | sort -u)
  [[ $ids =~ ^[0-9a-f]{64}$ ]] || { echo "expected exactly one running app, found: $ids" >&2; return 1; }
  printf '%s\n' "$ids"
}

# api <method> <path> [json]: prints the body, then the HTTP status on its own line.
api() {
  local cid args=(-sS -X "$1" -w '\n%{http_code}' -H 'Content-Type: application/json')
  [[ -n ${token:-} ]] && args+=(-H "Authorization: Bearer $token")
  [[ -n ${3:-} ]] && args+=(-d "$3")
  cid=$(app_container) || return 1
  docker exec "$cid" curl "${args[@]}" "http://127.0.0.1:8000$2"
}

# BEGIN MIXED PREDICATES
body() { sed '$d' <<<"$1"; }
code() { tail -n1 <<<"$1"; }
field() {  # <json> <python expression over d>; JSON on stdin (a preview can exceed ARG_MAX)
  python3 -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1]))' "$2" <<<"$1"
}

# Reasons this proof may admit: routine work an image replacement carries plus
# the exact helper migration. Topology, proxy and Python-package work (which
# the operator install refuses on an image without those packages) need their
# own proof; an unknown code fails closed.
MIXED_REASON_CODES="host_helper_migration host_operator_tooling container_image_definition
  container_build_context frontend_dependencies sqlite_runtime legacy_python_runtime
  served_runtime_module baked_runtime baked_platform_boot platform_skill_templates
  self_hosted_topology_source railway_topology_source commit_launcher live_maintenance_tooling
  backend_development_source server_runtime live_source"

mixed_review() {  # <preview json> <previous> <target>: exactly the pinned image plus helper span
  python3 -c '
import json, sys
d = json.load(sys.stdin)
previous, target, allowed = sys.argv[1], sys.argv[2], set(sys.argv[3].split())
a = d.get("activation") or {}
actions, reasons = a.get("required_actions") or [], a.get("reasons") or []
helper = [r.get("paths") for r in reasons if r.get("code") == "host_helper_migration"]
ok = (d.get("current_sha") == previous and d.get("target_sha") == target
      and len(actions) == len(set(actions))
      and set(actions) in ({"image_rebuild", "host_maintenance"},
                           {"server_restart", "image_rebuild", "host_maintenance"})
      and helper == [["deployment/self-hosted-helper.required"]]
      and all(r.get("code") in allowed for r in reasons))
raise SystemExit(0 if ok else 1)' "$2" "$3" "$MIXED_REASON_CODES" <<<"$1"
}

refused_with() {  # <reply> <code>: exactly this refusal, never an accepted request
  [[ $(code "$1") == 409 ]] && [[ $(field "$(body "$1")" '(d.get("detail") or {}).get("code")') == "$2" ]]
}

# The residual scripts/test-upgrade-path.sh also requires after a helper
# handoff: restart and image work are complete, and the helper migration stays
# owed. If a reviewed mechanism ever verifies the installed helper from the
# container, update this together with that mechanism.
helper_handoff_only() {  # <status json>
  [[ $(field "$1" '(lambda a: a.get("required_actions") == ["host_maintenance"] and [(r.get("code"), r.get("paths")) for r in a.get("reasons") or []] == [("host_helper_migration", ["deployment/self-hosted-helper.required"])])(d.get("activation") or {})') == True ]]
}

# deploy-prod.sh's final public probe names one fixed host, which this proof
# resolves to loopback where nothing serves that name: it must fail there,
# after every earlier deploy check, and nothing after it may have run. Exit 0
# (and its completion line) is required only if that probe names DOMAIN.
deploy_public_host() {  # <deploy-prod.sh>
  sed -n 's#^  PUBLIC_URL="https://\([A-Za-z0-9.-]*\)/api/health"$#\1#p' "$1"
}
deploy_cutover_observed() {  # <exit status> <log> <public host>
  local marker
  for marker in "controller verified the exact admitted container" \
      "(matches the commit just built)" "protected runtime: current" \
      "served platform contains reviewed source" "internal /api/health: 200" \
      "internal /api/ready:  200"; do
    grep -Fq -- "$marker" "$2" || return 1
  done
  if [[ $3 == "$DOMAIN" ]]; then
    [[ $1 == 0 ]] && grep -Fq "deploy complete" "$2"
  else
    [[ $1 == 1 ]] && ! grep -Fq "public  /api/health: 200" "$2" \
      && ! grep -Fq "deploy complete" "$2" && ! grep -Fq "[4b/4]" "$2" \
      && grep -Fq "(https://$3/api/health)" "$2" \
      && [[ $(sed '/^[[:space:]]*$/d' "$2" | tail -n1) == *"public reverse-proxy returned non-200; check Caddy." ]]
  fi
}
# END MIXED PREDICATES

# BEGIN INGRESS CHECKS
# Only through this host's own bundled Caddy: loopback, no proxy, no curl
# configuration file, and Caddy's own internal CA for DOMAIN.
ingress() {  # <ca file> <path> [token]: prints the body, then the HTTP status
  local args=(-q -sS --noproxy '*' --max-time 10 --cacert "$1"
              --resolve "$DOMAIN:443:127.0.0.1" -w '\n%{http_code}')
  [[ -n ${3:-} ]] && args+=(-H "Authorization: Bearer $3")
  curl "${args[@]}" "https://$DOMAIN$2"
}
ingress_serves_target() {  # <ca file> <owner token> <target sha>
  local reply
  reply=$(ingress "$1" /api/health) && [[ $(code "$reply") == 200 ]] || return 1
  reply=$(ingress "$1" /api/ready) && [[ $(code "$reply") == 200 ]] || return 1
  reply=$(ingress "$1" /api/version) && [[ $(code "$reply") == 200 ]] || return 1
  [[ $(field "$(body "$reply")" 'd.get("sha")') == "$3" ]] || return 1
  # The pre-upgrade owner session: same owner and secret, through the proxy.
  reply=$(ingress "$1" /api/platform/status "$2") && [[ $(code "$reply") == 200 ]] || return 1
  helper_handoff_only "$(body "$reply")"
}
# END INGRESS CHECKS

# BEGIN REFUSAL SNAPSHOT
# Everything a refused action must leave as it was. Each observation fails the
# snapshot; expected absence is printed as a fact. A refused Settings action
# may only re-mint the service token and record failed update progress (checked
# separately), so neither is part of this snapshot. Root status timestamps
# (keys ending in _at) are dropped; every other status field is compared.
untouched_state() {
  local cid path
  cid=$(app_container) || return 1
  docker inspect -f '{{.Id}} {{.Image}} {{.State.StartedAt}} {{.RestartCount}}' "$cid" || return 1
  # The container shell needs its own pipefail; outer Bash cannot see a
  # failed find/hash hidden by the inner sort process.
  docker exec -u mobius "$cid" bash -euo pipefail -c '
    git -C /data/platform rev-parse HEAD
    git -C /data/platform for-each-ref --format="%(refname) %(objectname)"
    git -C /data/platform status --porcelain=v1 --untracked-files=all
    git -C /data/platform worktree list --porcelain
    find /data/mobius-rebuild/inbox -mindepth 1 -printf "%y %p %s\n" | sort
    find /data/mobius-rebuild/inbox -mindepth 1 -type f -exec sha256sum {} + | sort
    if [ -L /data/.platform-prepared-update.json ]; then
      echo "prepared record must not be a symlink" >&2
      exit 1
    elif [ -e /data/.platform-prepared-update.json ]; then
      test -f /data/.platform-prepared-update.json && test -r /data/.platform-prepared-update.json || exit 1
      sha256sum /data/.platform-prepared-update.json
    else
      echo "prepared: absent"
    fi' || return 1
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({k: v for k, v in d.items() if not k.endswith("_at")}, sort_keys=True))' "$STATUS" || return 1
  for path in /var/lib/mobius-rebuild/transaction.json /var/lib/mobius-rebuild/workers.json \
      /usr/local/libexec/mobius-manual-cutover.py /usr/local/libexec/mobius-rebuild-host; do
    if [[ -e $path || -L $path ]]; then sha256sum "$path" || return 1; else echo "$path: absent"; fi
  done
  find /etc/mobius-rebuild /etc/systemd/system -maxdepth 1 -type f \
      \( -path '/etc/mobius-rebuild/*' -o -name 'mobius-rebuild*' \) \
    | sort | while read -r path; do sha256sum "$path" || exit 1; done || return 1
  docker image ls --no-trunc -q mobius || return 1
}
# END REFUSAL SNAPSHOT

# BEGIN REVISION READER
revision_of() {  # <sha> <file> <name>: a revision constant, read as text
  git -C "$ROOT" show "$1:$2" | sed -n "s/^$3 = \([0-9][0-9]*\)\$/\1/p"
}
# END REVISION READER

single_data_writer() {  # <owner CID>: no other container mounts its /data writable
  local ids
  ids=$(docker ps --no-trunc -q) || return 1
  # Intentionally split: one argument per running container ID.
  # shellcheck disable=SC2086
  docker inspect $ids | python3 -c '
import json, sys
items = json.load(sys.stdin)
owner = next(c for c in items if c["Id"] == sys.argv[1])
data = next(m["Source"] for m in owner["Mounts"] if m["Destination"] == "/data")
writers = [c["Id"] for c in items if any(m["Source"] == data and m.get("RW") for m in c["Mounts"])]
assert writers == [owner["Id"]], writers
' "$1"
}

data_source() {  # <CID>
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$1"
}

served() {  # <CID>: "<source> <sha>" the entrypoint selected for uvicorn
  docker exec "$1" sh -euc 'printf "%s %s\n" "$(cat /tmp/serving-source)" "$(cat /tmp/serving-sha)"'
}

# BEGIN HELPER IDENTITY
helper_identity() {  # <sha>: the launcher and worker installed from that release
  local launcher worker digest
  launcher=$(revision_of "$1" scripts/mobius-rebuild-launcher.py LAUNCHER_REVISION) || return 1
  worker=$(revision_of "$1" scripts/mobius-rebuild-host.py WORKER_REVISION) || return 1
  digest=$(git -C "$ROOT" show "$1:scripts/mobius-rebuild-host.py" | sha256sum | cut -d' ' -f1) || return 1
  python3 - "$STATUS" "$launcher" "$worker" "$digest" <<'HELPER'
import json, sys
from pathlib import Path
status_path, launcher, worker, digest = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
status = json.loads(Path(status_path).read_text())
assert status.get("launcher_revision") == launcher, status
active = json.loads(Path("/var/lib/mobius-rebuild/workers.json").read_text())["active"]
assert active["revision"] == worker and active["sha256"] == digest, active
assert not Path("/var/lib/mobius-rebuild/transaction.json").exists(), "a replacement journal is open"
HELPER
}
# END HELPER IDENTITY

# The transcript fixture, run by the code the server runs.
probe() {  # <command>
  local cid backend
  cid=$(app_container) || fail "cannot identify the app"
  backend=$(docker exec -u mobius "$cid" sh -c 'readlink "/proc/$(pgrep -n -u mobius -f "/bin/uvicorn app\.main:app")/cwd"') \
    && [[ -n $backend ]] || fail "cannot locate the serving uvicorn process"
  docker cp "$ROOT/scripts/transcript_rollback_probe.py" "$cid:/tmp/probe.py" || fail "cannot copy the transcript probe"
  docker exec -u mobius -w "$backend" -e PYTHONPATH="$backend" "$cid" python3 /tmp/probe.py "$1" \
    || fail "transcript probe '$1' failed in $backend"
}

wait_healthy() {
  local cid state
  for _ in $(seq 1 60); do
    if cid=$(app_container 2>/dev/null) && state=$(docker inspect -f '{{.State.Health.Status}}' "$cid") \
        && [[ $state == healthy ]]; then
      return 0
    fi
    sleep 5
  done
  fail "the app did not become healthy"
}

checkout_clean() {  # the operator checkout carries only its own .env
  local out
  out=$(git -C "$CHECKOUT" status --porcelain=v1 --ignored --untracked-files=all) || return 1
  [[ $out == '!! .env' ]]
}

fresh_host_preflight "$ROOT"

# Fail on the pair before touching the host.
git -C "$ROOT" merge-base --is-ancestor "$PREVIOUS" "$TARGET" \
  || refuse "$TARGET does not descend from $PREVIOUS"
previous_helper=$(git -C "$ROOT" show "$PREVIOUS:deployment/self-hosted-helper.required") || refuse "no previous helper marker"
target_helper=$(git -C "$ROOT" show "$TARGET:deployment/self-hosted-helper.required") || refuse "no target helper marker"
[[ $previous_helper =~ ^[0-9]+$ && $target_helper =~ ^[0-9]+$ ]] && (( target_helper > previous_helper )) \
  || refuse "the pair does not advance the helper requirement"
git -C "$ROOT" cat-file -e "$TARGET:scripts/mobius-manual-cutover.py" || refuse "the target has no root-pinned manual adapter"
! git -C "$ROOT" cat-file -e "$PREVIOUS:scripts/mobius-manual-cutover.py" 2>/dev/null \
  || refuse "the previous release already ships the manual adapter"
target_worker=$(revision_of "$TARGET" scripts/mobius-rebuild-host.py WORKER_REVISION)
[[ $target_worker =~ ^[0-9]+$ ]] || refuse "missing target worker revision"
public_host=$(git -C "$ROOT" show "$TARGET:scripts/deploy-prod.sh" | deploy_public_host /dev/stdin)
[[ $public_host =~ ^[A-Za-z0-9.-]+$ ]] || refuse "cannot read deploy-prod.sh's public probe host"

echo "mixed upgrade: ${PREVIOUS:0:12} -> ${TARGET:0:12} (helper $previous_helper -> $target_helper)"
WORK=$(mktemp -d /tmp/mobius-mixed-upgrade.XXXXXX)
RELEASES=$WORK/releases.git   # the operator's release remote
CHECKOUT=$WORK/mobius         # the trusted host checkout that owns the app
if [[ $public_host != "$DOMAIN" ]]; then
  printf '127.0.0.1 %s # mobius mixed-upgrade proof: never reach a real host\n' "$public_host" >>/etc/hosts
  resolved=$(getent hosts "$public_host" | awk 'NR == 1 {print $1}') || fail "cannot resolve $public_host"
  [[ $resolved == 127.0.0.1 ]] || fail "$public_host does not resolve to loopback; refusing to probe a real host"
fi
git init -q --bare --initial-branch=main "$RELEASES"
git -C "$ROOT" push -q "$RELEASES" "$PREVIOUS:refs/heads/main" "$TARGET:refs/mixed-upgrade/target" \
  || fail "cannot publish the releases locally"
git clone -q "$RELEASES" "$CHECKOUT" || fail "cannot clone the operator checkout"
printf 'SECRET_KEY=mixed-upgrade-regression-key-0123456789abcdef\nDOMAIN=%s\n' "$DOMAIN" >"$CHECKOUT/.env"
chmod 0600 "$CHECKOUT/.env"
checkout_clean || fail "the operator checkout is not clean"

echo "1. the previous release runs as an owner deploys it"
(cd "$CHECKOUT" && MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" docker compose --env-file "$CHECKOUT/.env" \
  up -d --no-build --no-deps app) || fail "could not deploy the previous release"
wait_healthy
app=$(app_container) || fail "no single previous app"
# BEGIN PREVIOUS IDENTITY
previous_image=$(docker image inspect -f '{{.Id}}' "$IMAGE:sha-$PREVIOUS") || fail "cannot inspect the previous image"
labels=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.source"}}' "$previous_image") \
  || fail "cannot read the previous image labels"
[[ $labels == "$PREVIOUS https://github.com/mobius-os/mobius" ]] || fail "the previous image is not the official $PREVIOUS: $labels"
running=$(docker inspect -f '{{.Image}}' "$app") || fail "cannot inspect the previous app"
[[ $running == "$previous_image" ]] || fail "the app does not run the previous image"
built=$(docker exec "$app" python3 -c 'import json; print(json.load(open("/app/build-info.json"))["sha"])') \
  || fail "cannot read the previous build record"
[[ $built == "$PREVIOUS" ]] || fail "the previous image records build $built"
served_now=$(served "$app") || fail "cannot read the served source"
head=$(docker exec -u mobius "$app" git -C /data/platform rev-parse HEAD) || fail "cannot read the previous checkout"
[[ $served_now == "platform $PREVIOUS" && $head == "$PREVIOUS" ]] \
  || fail "the previous app serves '$served_now' from HEAD $head, not $PREVIOUS"
reply=$(api GET /api/version) || fail "cannot read the previous version"
version=$(field "$(body "$reply")" 'd.get("sha")') || fail "unreadable previous version: $reply"
[[ $version == "$PREVIOUS" ]] || fail "the previous app reports $version"
# END PREVIOUS IDENTITY
reply=$(api POST /api/auth/setup '{"username":"owner","password":"mixed-upgrade-owner-password"}') || fail "owner setup failed"
[[ $(code "$reply") == 200 ]] || fail "owner setup failed: $(body "$reply")"
token=$(field "$(body "$reply")" 'd["access_token"]') || fail "owner setup returned no token"
# The worker's chat drain authenticates with the owner's service token.
for _ in $(seq 1 30); do
  docker exec "$app" test -s /data/service-token.txt && break
  sleep 2
done
docker exec "$app" test -s /data/service-token.txt || fail "the instance has no service token"
probe seed >/dev/null
probe dump >"$WORK/seeded.json"
docker exec -u mobius "$app" sh -euc \
  'mkdir -p /data/mixed-upgrade-proof && od -An -tx1 -N32 /dev/urandom | tr -d " \n" >/data/mixed-upgrade-proof/sentinel' \
  || fail "cannot write the data sentinel"
sentinel=$(docker exec "$app" sha256sum /data/mixed-upgrade-proof/sentinel) || fail "cannot read the data sentinel"
volume=$(data_source "$app") || fail "cannot read the data volume"
[[ -n $volume ]] || fail "the app has no /data volume"
"$CHECKOUT/scripts/install-rebuild-helper.sh" || fail "the previous helper did not install"
helper_identity "$PREVIOUS" || fail "the installed helper is not the previous release's"
[[ ! -e /usr/local/libexec/mobius-manual-cutover.py ]] || fail "the previous helper installed a manual adapter"

echo "2. the previous release's Settings refuses the mixed update"
git -C "$RELEASES" bundle create "$WORK/target.bundle" refs/mixed-upgrade/target >/dev/null 2>&1 \
  || fail "could not bundle the target"
docker cp "$WORK/target.bundle" "$app:/tmp/target.bundle" || fail "cannot hand the target to the app"
docker exec "$app" chmod 0644 /tmp/target.bundle || fail "cannot share the target bundle"
docker exec -u mobius "$app" git -C /data/platform fetch -q /tmp/target.bundle \
  "refs/mixed-upgrade/target:refs/remotes/origin/main" || fail "the previous release could not fetch the target"
reply=$(api GET /api/platform/update-preview) || fail "cannot request the update preview"
[[ $(code "$reply") == 200 ]] || fail "the update preview failed: $(body "$reply")"
preview=$(body "$reply")
conflicts=$(field "$preview" 'len(d.get("conflict_paths") or []) + len(d.get("blocking_paths") or [])') \
  || fail "unreadable update preview: $preview"
[[ $conflicts == 0 ]] || fail "a fresh instance reports update conflicts: $preview"
mixed_review "$preview" "$PREVIOUS" "$TARGET" || fail "the review is not exactly the pinned image plus helper span: $preview"
plan=$(field "$preview" 'json.dumps({k: d.get(k) for k in ("plan_id", "current_sha", "target_sha", "image_digest")})') \
  || fail "the preview has no plan"
before=$(untouched_state) || fail "cannot record the pre-refusal state"
reply=$(api POST /api/platform/rebuild "$plan") || fail "cannot press Update"
refused_with "$reply" external_activation_required \
  || fail "Update did not refuse the mixed span ($(code "$reply")): $(body "$reply")"
after=$(untouched_state) || fail "cannot record the state after Update"
[[ $after == "$before" ]] || fail "the refused Update changed the installation"
reply=$(api POST /api/platform/apply "$plan") || fail "cannot press Apply"
refused_with "$reply" image_rebuild_required \
  || fail "Apply did not refuse the mixed span ($(code "$reply")): $(body "$reply")"
after=$(untouched_state) || fail "cannot record the state after Apply"
[[ $after == "$before" ]] || fail "the refused Apply changed the installation"
reply=$(api GET /api/platform/update-progress) || fail "cannot read update progress"
progress=$(field "$(body "$reply")" '(d.get("phase"), d.get("error"), d.get("target_sha"))') \
  || fail "unreadable update progress: $reply"
[[ $progress == "('failed', 'image_rebuild_required', '$TARGET')" ]] || fail "unexpected update progress: $progress"

echo "3. the operator pulls the target; its deploy refuses the old helper"
git -C "$ROOT" push -q "$RELEASES" "$TARGET:refs/heads/main" || fail "cannot publish the target locally"
git -C "$CHECKOUT" pull -q --ff-only || fail "the operator checkout cannot advance"
head=$(git -C "$CHECKOUT" rev-parse HEAD) || fail "cannot read the operator checkout"
[[ $head == "$TARGET" ]] || fail "the operator checkout is $head, not the target"
checkout_clean || fail "the operator checkout is not clean"
status=0
"$CHECKOUT/scripts/deploy-prod.sh" --yes >"$WORK/deploy-early.log" 2>&1 || status=$?
[[ $status != 0 ]] || fail "deploy-prod.sh ran beside a helper without the manual adapter"
grep -Fq "the installed controller requires a privileged deploy and a root-pinned manual-cutover adapter" \
  "$WORK/deploy-early.log" || { cat "$WORK/deploy-early.log" >&2; fail "deploy-prod.sh failed for another reason"; }
after=$(untouched_state) || fail "cannot record the state after the refused deploy"
[[ $after == "$before" ]] || fail "the refused deploy changed the installation"

echo "4. the operator installs the target's helper migration"
app_state=$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$app") || fail "cannot inspect the app"
"$CHECKOUT/scripts/install-rebuild-helper.sh" || fail "the target helper migration failed"
cid=$(app_container) || fail "no single app after the helper migration"
now=$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$cid") || fail "cannot inspect the app"
[[ $now == "$app_state" ]] || fail "installing the helper replaced or restarted the healthy app"
helper_identity "$TARGET" || fail "the installed helper is not the target's"
python3 - "$CHECKOUT" <<'ADAPTER' || fail "the manual adapter is not the reviewed one"
import stat, sys
from pathlib import Path
adapter = Path("/usr/local/libexec/mobius-manual-cutover.py")
st = adapter.lstat()
assert stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022, adapter
assert adapter.read_bytes() == (Path(sys.argv[1]) / "scripts/mobius-manual-cutover.py").read_bytes()
ADAPTER

echo "5. the operator deploys the target with deploy-prod.sh"
checkout_clean || fail "the operator checkout is not clean"
status=0
"$CHECKOUT/scripts/deploy-prod.sh" --yes >"$WORK/deploy.log" 2>&1 || status=$?
deploy_cutover_observed "$status" "$WORK/deploy.log" "$public_host" \
  || { tail -n 80 "$WORK/deploy.log" >&2; fail "deploy-prod.sh did not cut over as expected (exit $status)"; }

echo "6. the target serves through the local proxy with the current helper and the owner's data"
app=$(app_container) || fail "no single app after the deploy"
built_image=$(docker image inspect -f '{{.Id}}' mobius) || fail "deploy-prod.sh built no image"
running=$(docker inspect -f '{{.Image}}' "$app") || fail "cannot inspect the app"
[[ $running == "$built_image" ]] || fail "the app does not run the image deploy-prod.sh built"
labels=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' mobius) \
  || fail "cannot read the built image label"
[[ $labels == "$TARGET" ]] || fail "the built image is $labels, not the target"
admission=$(docker inspect -f '{{index .Config.Labels "io.mobius.admission.token"}}' "$app") || fail "cannot read admission"
[[ $admission =~ ^[0-9a-f]{32}$ ]] || fail "the installed helper did not admit the app"
single_data_writer "$app" || fail "another container writes the app data"
now=$(data_source "$app") || fail "cannot read the data volume"
[[ $now == "$volume" ]] || fail "the app no longer uses the original data volume"
built=$(docker exec "$app" python3 -c 'import json; print(json.load(open("/app/build-info.json"))["sha"])') \
  || fail "cannot read the target build record"
[[ $built == "$TARGET" ]] || fail "the running image records build $built"
served_now=$(served "$app") || fail "cannot read the served source"
[[ $served_now == "platform "* ]] || fail "the app serves '$served_now', not the platform checkout"
docker exec -u mobius "$app" git -C /data/platform merge-base --is-ancestor "$TARGET" "${served_now#platform }" \
  || fail "the served checkout does not contain the target"
docker exec "$app" sh -euc 'test ! -e /data/.platform-prepared-update.json' || fail "the update never settled"
python3 - "$TARGET" "$target_worker" <<'CUTOVER' || fail "the current worker did not perform the replacement"
import json, sys
from pathlib import Path
root = Path("/var/lib/mobius-rebuild")
status = json.loads((root / "status.json").read_text())
assert status.get("state") == "succeeded" and status.get("expected_sha") == sys.argv[1], status
assert status.get("worker_revision") == int(sys.argv[2]), status
assert not (root / "transaction.json").exists(), "the replacement left its journal"
CUTOVER
# The bundled proxy routes "app" to exactly this admitted container.
caddy=$(docker ps --no-trunc -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
  --filter label=com.docker.compose.service=caddy) || fail "cannot find the bundled proxy"
[[ $caddy =~ ^[0-9a-f]{64}$ ]] || fail "expected exactly one bundled proxy, found: $caddy"
ids=$(docker ps --no-trunc -q) || fail "cannot list containers"
# Intentionally split: one argument per running container ID.
# shellcheck disable=SC2086
docker inspect $ids | python3 -c '
import json, sys
items = {c["Id"]: c for c in json.load(sys.stdin)}
proxy_networks = set(items[sys.argv[1]]["NetworkSettings"]["Networks"])
assert proxy_networks, "the proxy has no network"
for name in proxy_networks:
    routed = [cid for cid, c in items.items()
              if "app" in ((c["NetworkSettings"]["Networks"].get(name) or {}).get("Aliases") or [])]
    assert routed == [sys.argv[2]], (name, routed)
' "$caddy" "$app" || fail "the bundled proxy does not route to exactly the admitted app"
docker exec "$caddy" cat /data/caddy/pki/authorities/local/root.crt >"$WORK/caddy-root.crt" \
  || fail "cannot read the bundled proxy's local CA"
for _ in $(seq 1 30); do
  ingress_serves_target "$WORK/caddy-root.crt" "$token" "$TARGET" && break
  sleep 2
done
ingress_serves_target "$WORK/caddy-root.crt" "$token" "$TARGET" \
  || fail "the local proxy does not serve the target, the owner's session and exactly the helper handoff"
sha=$(docker exec "$app" sha256sum /data/mixed-upgrade-proof/sentinel) || fail "cannot read the data sentinel"
[[ $sha == "$sentinel" ]] || fail "the data sentinel changed"
for _ in $(seq 1 150); do  # waits only on a valid "still pending" answer
  answer=$(probe pending) || fail "cannot read transcript conversion"
  case $answer in
    '{"pending": 0}') break ;;
    '{"pending": '[1-9]*'}') sleep 2 ;;
    *) fail "unexpected transcript conversion answer: $answer" ;;
  esac
done
[[ $answer == '{"pending": 0}' ]] || fail "transcript conversion did not finish"
probe dump >"$WORK/final.json"
python3 - "$WORK/seeded.json" "$WORK/final.json" <<'SAME' || fail "chat transcripts changed across the upgrade"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
# The fixture's deliberately damaged chat may become its damage placeholder.
a.pop("damaged", None); damaged = b.pop("damaged", None)
assert damaged is None or damaged["messages"][0].get("transcript_damage") is True, damaged
a, b = ({k: json.dumps(v, sort_keys=True) for k, v in d.items()} for d in (a, b))
assert a == b, sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))[:5]
SAME
echo "mixed upgrade: Settings refused ${PREVIOUS:0:12} -> ${TARGET:0:12}; the operator path cut the app over with helper revision $target_helper and the owner's data"
echo "mixed upgrade: partial deploy: deploy-prod.sh stopped at its fixed public probe, skipping its shell reload broadcast, build-cache cleanup and final completion"
