#!/usr/bin/env bash
set -euo pipefail

# Install the root-owned self-host replacement controller from the trusted
# checkout that owns the live Compose app. No executable code is downloaded.

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
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py \
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
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py \
  "${FILES[@]#"$ROOT/"}" \
  || { echo "Commit and review every helper and Compose input first." >&2; exit 1; }
[[ -z $(git -C "$ROOT" status --porcelain=v1 --untracked-files=all -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py \
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
install -D -m 0755 "$ROOT/scripts/mobius-rebuild-host.py" \
  /usr/local/libexec/mobius-rebuild-host
/usr/local/libexec/mobius-rebuild-host bootstrap-runtime "$CID"
SNAPSHOT=$(mktemp /etc/mobius-rebuild/compose.XXXXXX)
if [[ -n $FROZEN_SOURCE ]]; then
  cp "$FROZEN_SOURCE" "$SNAPSHOT"
else
  MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose \
    "${ARGS[@]}" config >"$SNAPSHOT"
fi
chmod 0600 "$SNAPSHOT"
mv -f "$SNAPSHOT" /etc/mobius-rebuild/compose.yml
cat >/etc/mobius-rebuild/image.override.yml <<'EOF'
services:
  app:
    image: ${MOBIUS_IMAGE:?MOBIUS_IMAGE is required}
    volumes:
      - type: bind
        source: ${MOBIUS_RUNTIME_OVERLAY:?MOBIUS_RUNTIME_OVERLAY is required}
        target: /app/runtime
        read_only: true
EOF
chmod 0600 /etc/mobius-rebuild/image.override.yml
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
install -d -o "$APP_UID" -g "$APP_GID" -m 0700 "$DATA_SOURCE/mobius-rebuild/inbox"

cat >/etc/systemd/system/mobius-rebuild.service <<'EOF'
[Unit]
Description=Replace the Möbius app container with an official image
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/mobius-rebuild-host run
ExecStopPost=/usr/local/libexec/mobius-rebuild-host reconcile
EOF
cat >/etc/systemd/system/mobius-rebuild.path <<EOF
[Unit]
Description=Watch for a Möbius container replacement request

[Path]
PathExists=$DATA_SOURCE/mobius-rebuild/inbox/request.json
Unit=mobius-rebuild.service

[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/mobius-rebuild-reconcile.service <<'EOF'
[Unit]
Description=Reconcile interrupted Möbius container replacement state
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
/usr/local/libexec/mobius-rebuild-host reconcile
systemctl enable mobius-rebuild-reconcile.service
systemctl enable --now mobius-rebuild.path

echo "Container replacement installed for Compose project '$PROJECT'."
echo "Topology: ${FILES[*]:-$FROZEN_SOURCE}"
echo "Rerun this installer after an intentional Compose topology change."
