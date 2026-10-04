#!/usr/bin/env bash
set -euo pipefail

# Install the root-owned self-host replacement controller from the trusted
# checkout that owns the live Compose app. No executable code is downloaded.
# This installs the frozen launcher and seeds it with this checkout's worker;
# later worker changes arrive with verified official images, so this runs once.
#
# Helper protocol revision: 2 (request version 2 echoes the app's nonce).
# Keep in step with deployment/self-hosted-helper.required; never decrement.

# A release that advances the helper requirement cannot be installed from the
# older release's Settings, which reports host work it can never retire. Once
# this installation verifies the floor-aware worker as ACTIVE, the installer
# starts that waiting update through the running app's own reviewed updater
# (scripts/finish-helper-update.py). --no-update installs the helper only.
FINISH_UPDATE=1
for arg in "$@"; do
  case "$arg" in
    --no-update) FINISH_UPDATE=0 ;;
    *) echo "usage: sudo scripts/install-rebuild-helper.sh [--no-update]" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo from the trusted Möbius checkout." >&2
  exit 1
fi
for command in docker git python3 systemctl; do command -v "$command" >/dev/null; done
docker compose version >/dev/null
[[ -d /run/systemd/system ]] \
  || { echo "This helper requires systemd as the host service manager." >&2; exit 1; }
ARCH=$(docker info --format '{{.Architecture}}')
[[ $ARCH == x86_64 || $ARCH == amd64 ]] \
  || { echo "Official Möbius images currently support amd64 hosts only." >&2; exit 1; }

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "Run from a committed Möbius checkout, not copied app data." >&2; exit 1; }
git -C "$ROOT" ls-files --error-unmatch \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  scripts/mobius-rebuild-launcher.py \
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py scripts/finish-helper-update.py \
  docker-compose.yml >/dev/null \
  || { echo "The replacement helper must be tracked in the trusted checkout." >&2; exit 1; }

# The running container's Compose labels are the topology owner. This preserves
# the shared-edge overlay when present instead of guessing from an installer
# flag that can drift from the deployment which created the container.
PROJECT=${COMPOSE_PROJECT_NAME:-$(basename "$ROOT")}
mapfile -t CANDIDATES < <(docker ps -q \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter "label=com.docker.compose.service=app")
[[ ${#CANDIDATES[@]} -eq 1 ]] \
  || { echo "Expected exactly one running app for Compose project '$PROJECT'." >&2; exit 1; }
CID=${CANDIDATES[0]}
CURRENT_IMAGE=$(docker inspect "$CID" --format '{{.Config.Image}}')
[[ -n $CURRENT_IMAGE && $CURRENT_IMAGE != *$'\n'* ]] \
  || { echo "Could not resolve the running app image." >&2; exit 1; }
readarray -t LABELS < <(docker inspect "$CID" --format \
  '{{index .Config.Labels "com.docker.compose.project"}}{{println}}{{index .Config.Labels "com.docker.compose.project.working_dir"}}{{println}}{{index .Config.Labels "com.docker.compose.project.config_files"}}{{println}}{{index .Config.Labels "com.docker.compose.project.environment_file"}}')
[[ ${LABELS[0]:-} == "$PROJECT" ]] || { echo "Compose project label mismatch." >&2; exit 1; }
WORKING_DIR=${LABELS[1]:-}
FROZEN_SOURCE=
FILES=()
ENV_FILES=()
if [[ $WORKING_DIR == /etc/mobius-rebuild \
      && ${LABELS[2]:-} == "/etc/mobius-rebuild/compose.yml,/etc/mobius-rebuild/image.override.yml" ]]; then
  # A container already recreated by this helper records the frozen root-owned
  # files in its Compose labels rather than the original checkout. Preserve the
  # proven resolved topology while upgrading only the reviewed controller and
  # its narrow image/runtime override.
  for frozen in /etc/mobius-rebuild/config.json \
                /etc/mobius-rebuild/compose.yml \
                /etc/mobius-rebuild/image.override.yml; do
    [[ -f $frozen && ! -L $frozen && $(stat -c '%u' "$frozen") == 0 \
       && $((8#$(stat -c '%a' "$frozen") & 8#022)) == 0 ]] \
      || { echo "Existing replacement topology is not root-controlled." >&2; exit 1; }
  done
  FROZEN_SOURCE=/etc/mobius-rebuild/compose.yml
else
  FILES_OUTPUT=$(python3 "$ROOT/scripts/rebuild-topology.py" compose-files \
    "$ROOT" "$WORKING_DIR" "${LABELS[2]:-}")
  mapfile -t FILES <<<"$FILES_OUTPUT"
  ENV_FILES_OUTPUT=$(python3 "$ROOT/scripts/rebuild-topology.py" \
    environment-files "${LABELS[3]:-}")
  if [[ -n $ENV_FILES_OUTPUT ]]; then
    mapfile -t ENV_FILES <<<"$ENV_FILES_OUTPUT"
  fi
  for file in "${FILES[@]}"; do
    relative=${file#"$ROOT/"}
    git -C "$ROOT" ls-files --error-unmatch "$relative" >/dev/null \
      || { echo "Compose input is not tracked: $relative" >&2; exit 1; }
  done
fi
git -C "$ROOT" diff --quiet HEAD -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  scripts/mobius-rebuild-launcher.py \
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py scripts/finish-helper-update.py \
  "${FILES[@]#"$ROOT/"}" \
  || { echo "Commit and review every helper and Compose input first." >&2; exit 1; }
[[ -z $(git -C "$ROOT" status --porcelain=v1 --untracked-files=all -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  scripts/mobius-rebuild-launcher.py \
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py scripts/finish-helper-update.py \
  "${FILES[@]#"$ROOT/"}") ]] \
  || { echo "The selected helper and Compose inputs must be clean." >&2; exit 1; }

ARGS=()
for file in "${ENV_FILES[@]}"; do ARGS+=(--env-file "$file"); done
ARGS+=(-p "$PROJECT")
for file in "${FILES[@]}"; do ARGS+=(-f "$file"); done
DATA_SOURCE=$(docker inspect "$CID" --format \
  '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')
[[ $DATA_SOURCE =~ ^/[A-Za-z0-9_./-]+$ && -d $DATA_SOURCE ]] \
  || { echo "Could not resolve the persistent /data mount on the host." >&2; exit 1; }
APP_UID=$(docker exec "$CID" id -u mobius)
APP_GID=$(docker exec "$CID" id -g mobius)
[[ $APP_UID =~ ^[0-9]+$ && $APP_GID =~ ^[0-9]+$ ]] \
  || { echo "Could not resolve the app user's numeric ownership." >&2; exit 1; }

# Resolve and compare the exact network set before any host state is written.
RUNNING_NETWORKS=$(docker inspect "$CID" --format \
  '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
  | sed '/^$/d' | sort)
RESOLVED=$(mktemp)
trap 'rm -f "$RESOLVED"' EXIT
if [[ -n $FROZEN_SOURCE ]]; then
  MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose \
    -p "$PROJECT" -f "$FROZEN_SOURCE" config --format json >"$RESOLVED"
else
  MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose \
    "${ARGS[@]}" config --format json >"$RESOLVED"
fi
EXPECTED_NETWORKS=$(python3 "$ROOT/scripts/rebuild-topology.py" \
  expected-networks "$RESOLVED")
[[ $RUNNING_NETWORKS == "$EXPECTED_NETWORKS" ]] \
  || { printf 'Refusing to freeze a topology mismatch.\nRunning:\n%s\nResolved:\n%s\n' \
       "$RUNNING_NETWORKS" "$EXPECTED_NETWORKS" >&2; exit 1; }

umask 077
install -d -m 0700 /etc/mobius-rebuild /var/lib/mobius-rebuild
# Nothing may replace the app while the controller changes: stop new requests
# from starting a run, wait for a running one, and hold its lock until the
# installation is complete. Requests stay queued meanwhile.
systemctl stop mobius-rebuild.path 2>/dev/null || true
# A failed installation leaves the previous controller in charge, not paused.
trap 'rm -f "$RESOLVED"; systemctl start mobius-rebuild.path 2>/dev/null || true' EXIT
exec 9>>/var/lib/mobius-rebuild/replace.lock
flock 9
# Seed the launcher's worker from this checkout. It never lowers a revision
# already adopted from a newer official image.
MOBIUS_REBUILD_LOCK_HELD=1 \
  /usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" adopt-self
# A queued or previously rejected candidate is not installed recovery authority.
# Refuse rather than silently weaken the launcher's revision high-water guard.
/usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" verify-active
install -D -m 0755 "$ROOT/scripts/mobius-rebuild-launcher.py" \
  /usr/local/libexec/.mobius-rebuild-host.new
mv -f /usr/local/libexec/.mobius-rebuild-host.new \
  /usr/local/libexec/mobius-rebuild-host
SNAPSHOT=$(mktemp /etc/mobius-rebuild/compose.XXXXXX)
if [[ -n $FROZEN_SOURCE ]]; then
  cp "$FROZEN_SOURCE" "$SNAPSHOT"
else
  MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose \
    "${ARGS[@]}" config >"$SNAPSHOT"
fi
chmod 0600 "$SNAPSHOT"
mv -f "$SNAPSHOT" /etc/mobius-rebuild/compose.yml
OVERRIDE_NEW=$(mktemp /etc/mobius-rebuild/image.override.XXXXXX)
cat >"$OVERRIDE_NEW" <<'EOF'
services:
  app:
    image: ${MOBIUS_IMAGE:?MOBIUS_IMAGE is required}
    environment:
      MOBIUS_HOST_RECOVERY_REQUIRED: "1"
    volumes:
      - type: bind
        source: /var/lib/mobius-rebuild
        target: /run/mobius-rebuild-host
        read_only: true
        bind:
          create_host_path: false
EOF
chmod 0600 "$OVERRIDE_NEW"
mv -f "$OVERRIDE_NEW" /etc/mobius-rebuild/image.override.yml
python3 - "$PROJECT" "$DATA_SOURCE" <<'PY'
import json, os, sys, tempfile
value = {"version": 3, "project": sys.argv[1], "data_dir": sys.argv[2]}
fd, name = tempfile.mkstemp(dir="/etc/mobius-rebuild", text=True)
with os.fdopen(fd, "w") as handle: json.dump(value, handle, separators=(",", ":"))
os.chmod(name, 0o600)
os.replace(name, "/etc/mobius-rebuild/config.json")
PY

# Root owns status and topology; the app user can create only the fixed inbox
# request/ready files consumed by the validated worker.
install -d -o root -g root -m 0755 "$DATA_SOURCE/mobius-rebuild"
# GNU install accepts a numeric owner absent from the host passwd; uutils
# (the default coreutils on Ubuntu 25.10+) does not. The app uid exists only
# inside the container, so set the ownership portably instead.
mkdir -p "$DATA_SOURCE/mobius-rebuild/inbox"
chown "$APP_UID:$APP_GID" "$DATA_SOURCE/mobius-rebuild/inbox"
chmod 0700 "$DATA_SOURCE/mobius-rebuild/inbox"

cat >/etc/systemd/system/mobius-rebuild.service <<'EOF'
[Unit]
Description=Replace the Möbius app container with an official image
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/mobius-rebuild-host run
ExecStopPost=/usr/local/libexec/mobius-rebuild-host reconcile
# Recovery after an interrupted run may restore or restart a container and
# wait for it; the default 90 s stop limit would cut it off mid-recovery.
TimeoutStopSec=15min
EOF
cat >/etc/systemd/system/mobius-rebuild.path <<EOF
[Unit]
Description=Watch for a Möbius container rebuild request

[Path]
PathExists=$DATA_SOURCE/mobius-rebuild/inbox/request.json
Unit=mobius-rebuild.service

[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/mobius-rebuild-reconcile.service <<'EOF'
[Unit]
Description=Reconcile interrupted Möbius container rebuild state
After=docker.service
Requires=docker.service
Before=mobius-rebuild.path

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/mobius-rebuild-host reconcile

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/mobius-rebuild.service \
  /etc/systemd/system/mobius-rebuild.path \
  /etc/systemd/system/mobius-rebuild-reconcile.service
systemctl daemon-reload
# Release the lock so reconcile can settle interrupted work and refresh the
# capability status, then let queued requests run.
flock -u 9
exec 9>&-
/usr/local/libexec/mobius-rebuild-host reconcile
systemctl enable mobius-rebuild-reconcile.service
systemctl enable --now mobius-rebuild.path

echo "Container rebuild support installed for Compose project '$PROJECT'."
echo "Topology: ${FILES[*]:-$FROZEN_SOURCE}"
echo "Rerun this installer after an intentional Compose topology change."

[[ $FINISH_UPDATE == 1 ]] || exit 0
TARGET_SHA=$(git -C "$ROOT" rev-parse HEAD)
PROTOCOL=$(tr -d '[:space:]' < "$ROOT/deployment/self-hosted-helper.required")
# The app user runs its own updater; this checkout supplies only the bridge.
if ! FINISH=$(docker exec -i -u mobius -w /data/platform/backend "$CID" \
    python3 -I - "$TARGET_SHA" "$PROTOCOL" < "$ROOT/scripts/finish-helper-update.py"); then
  echo "The helper is installed, but the waiting update was not started:" >&2
  printf '%s\n' "$FINISH" >&2
  exit 1
fi
STATE=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1].splitlines()[-1])["state"])' "$FINISH")
if [[ $STATE != queued ]]; then
  echo "No update is waiting for this helper; update from Settings as usual."
  exit 0
fi
NONCE=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1].splitlines()[-1])["request_nonce"])' "$FINISH")
echo "Installing Möbius ${TARGET_SHA:0:12}, which was waiting for this helper."
echo "Chats pause briefly while the container is replaced. Large chat histories"
echo "are converted on first start; this can take a few minutes."
for _ in $(seq 1 360); do
  OUTCOME=$(python3 - "$NONCE" <<'PY' 2>/dev/null || true
import json, sys
try:
    status = json.load(open("/var/lib/mobius-rebuild/status.json"))
except (OSError, ValueError):
    raise SystemExit
if status.get("request_nonce") == sys.argv[1] and status.get("state") in {
        "succeeded", "no_change", "failed", "rolled_back", "needs_recovery"}:
    print(status["state"], status.get("message") or "")
PY
)
  case "${OUTCOME%% *}" in
    succeeded|no_change) echo "Update installed: ${OUTCOME#* }"; exit 0 ;;
    failed|rolled_back|needs_recovery)
      echo "The update did not complete (${OUTCOME%% *}): ${OUTCOME#* }" >&2
      exit 1 ;;
  esac
  sleep 5
done
echo "The update is still running; follow it in Settings." >&2
exit 1
