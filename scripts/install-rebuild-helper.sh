#!/usr/bin/env bash
set -euo pipefail

# Install the root-owned self-host replacement controller from the trusted
# checkout that owns the live Compose app. No executable code is downloaded.
# This installs the frozen launcher and seeds it with this checkout's worker;
# later worker changes arrive with verified official images, so this runs once.
#
# Helper protocol revision: 5 (explicit root-pinned boot admission migration).
# Keep in step with deployment/self-hosted-helper.required; never decrement.

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
  scripts/mobius-rebuild-launcher.py scripts/mobius-boot-admission.py \
  scripts/mobius-manual-cutover.py \
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py \
  docker-compose.yml >/dev/null \
  || { echo "The replacement helper must be tracked in the trusted checkout." >&2; exit 1; }

# The running container's Compose labels are the topology owner. This preserves
# the shared-edge overlay when present instead of guessing from an installer
# flag that can drift from the deployment which created the container.
PROJECT=${COMPOSE_PROJECT_NAME:-$(basename "$ROOT")}
# Each admission attempt has its own Compose project. Its fixed owner label
# and legacy Compose labels are one union, not competing discovery fallbacks.
discover_app() {
  local legacy admitted
  local -a candidates
  legacy=$(docker ps --no-trunc -q \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=app") || return 1
  admitted=$(docker ps --no-trunc -q \
    --filter "label=io.mobius.admission.project=$PROJECT") || return 1
  mapfile -t candidates < <(printf '%s\n%s\n' "$legacy" "$admitted" | sed '/^$/d' | sort -u)
  [[ ${#candidates[@]} -eq 1 && ${candidates[0]} =~ ^[0-9a-f]{64}$ ]] \
    || { echo "Expected exactly one running app across legacy and admission projects for '$PROJECT'." >&2; return 1; }
  printf '%s\n' "${candidates[0]}"
}
CID=$(discover_app)
CURRENT_IMAGE=$(docker inspect "$CID" --format '{{.Config.Image}}')
[[ -n $CURRENT_IMAGE && $CURRENT_IMAGE != *$'\n'* ]] \
  || { echo "Could not resolve the running app image." >&2; exit 1; }
readarray -t LABELS < <(docker inspect "$CID" --format \
  '{{index .Config.Labels "com.docker.compose.project"}}{{println}}{{index .Config.Labels "com.docker.compose.project.working_dir"}}{{println}}{{index .Config.Labels "com.docker.compose.project.config_files"}}{{println}}{{index .Config.Labels "com.docker.compose.project.environment_file"}}{{println}}{{index .Config.Labels "io.mobius.admission.project"}}')
ADMISSION_MANAGED=0
if [[ -n ${LABELS[4]:-} && ${LABELS[4]} != '<no value>' ]]; then
  [[ ${LABELS[4]} == "$PROJECT" ]] || { echo "Admission owner project mismatch." >&2; exit 1; }
  ADMISSION_MANAGED=1
else
  [[ ${LABELS[0]:-} == "$PROJECT" ]] || { echo "Compose project label mismatch." >&2; exit 1; }
fi
WORKING_DIR=${LABELS[1]:-}
FROZEN_SOURCE=
FILES=()
ENV_FILES=()
if [[ $ADMISSION_MANAGED == 1 ]] || [[ $WORKING_DIR == /etc/mobius-rebuild \
      && ${LABELS[2]:-} == "/etc/mobius-rebuild/compose.yml,/etc/mobius-rebuild/image.override.yml" ]]; then
  # A container already recreated by this helper records the frozen root-owned
  # files in its Compose labels rather than the original checkout. Preserve the
  # proven resolved topology while upgrading only the reviewed controller and
  # its narrow image/runtime override.
  [[ -d /etc/mobius-rebuild && ! -L /etc/mobius-rebuild \
     && $(stat -c '%u' /etc/mobius-rebuild) == 0 \
     && $((8#$(stat -c '%a' /etc/mobius-rebuild) & 8#022)) == 0 ]] \
    || { echo "Existing replacement topology directory is not root-controlled." >&2; exit 1; }
  for frozen in /etc/mobius-rebuild/config.json \
                /etc/mobius-rebuild/compose.yml \
                /etc/mobius-rebuild/image.override.yml; do
    [[ -f $frozen && ! -L $frozen && $(stat -c '%u' "$frozen") == 0 \
       && $((8#$(stat -c '%a' "$frozen") & 8#022)) == 0 ]] \
      || { echo "Existing replacement topology is not root-controlled." >&2; exit 1; }
  done
  python3 - "$PROJECT" <<'PY_OWNER'
import json, sys
with open("/etc/mobius-rebuild/config.json") as handle:
    configuration = json.load(handle)
if configuration.get("version") != 3 or configuration.get("project") != sys.argv[1]:
    raise SystemExit("Frozen helper configuration belongs to a different project")
PY_OWNER
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
# Inspect only startup identity and mounts, never environment/secret values.
verify_admission_identity() {
  local inspected
  inspected=$(docker inspect "$CID" --format \
    '{"Id":{{json .Id}},"Image":{{json .Image}},"Name":{{json .Name}},"Entrypoint":{{json .Config.Entrypoint}},"Cmd":{{json .Config.Cmd}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}}}') || return 1
  python3 - "$PROJECT" "$CID" "$inspected" <<'PY_ADMISSION'
import json, re, stat, sys
from pathlib import Path

project, cid, raw = sys.argv[1:]
configuration = json.loads(Path("/etc/mobius-rebuild/config.json").read_text())
expected = json.loads(Path("/etc/mobius-rebuild/image.override.yml").read_text())
actual = json.loads(raw)
app = expected["services"]["app"]
labels = app.get("labels", {})
token = labels.get("io.mobius.admission.token", "")
operation = labels.get("io.mobius.admission.operation", "")
role = labels.get("io.mobius.admission.role")
name = "mobius-admission-" + token
prefix = ["python3", "-I", "-S", "/run/mobius-boot-admission.py", "enter",
          "--state-dir", "/run/mobius-admission", "--data-dir", "/data",
          "--token", token, "--"]

def require(condition):
    if not condition:
        raise SystemExit("Running admission container does not match the root-frozen owning identity")

require(configuration.get("version") == 3 and configuration.get("project") == project)
require(re.fullmatch(r"[0-9a-f]{64}", cid) and actual.get("Id") == cid)
require(re.fullmatch(r"[0-9a-f]{32}", token) and re.fullmatch(r"[0-9a-f]{32}", operation))
require(role in {"target", "rollback"} and labels.get("io.mobius.admission.project") == project)
require(expected.get("name") == name and app.get("container_name") == name and actual.get("Name") == "/" + name)
require(isinstance(app.get("entrypoint"), list) and app["entrypoint"][:len(prefix)] == prefix)
require(isinstance(app.get("command"), list) and bool(app["entrypoint"][len(prefix):] + app["command"]))
require(actual.get("Entrypoint") == app["entrypoint"] and actual.get("Cmd") == app["command"])
require(isinstance(app.get("image"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", app["image"]))
require(actual.get("Image") == app["image"])
observed_labels = actual.get("Labels") or {}
for key in ("project", "operation", "role", "token"):
    require(observed_labels.get("io.mobius.admission." + key) == labels["io.mobius.admission." + key])
require(observed_labels.get("com.docker.compose.project") == name)
require(observed_labels.get("com.docker.compose.service") == "app")
require(observed_labels.get("com.docker.compose.project.working_dir") == "/etc/mobius-rebuild")
attempt_path = Path("/etc/mobius-rebuild/attempts") / (token + ".json")
require(observed_labels.get("com.docker.compose.project.config_files") == str(attempt_path))
# Derive the path from the validated token, never open an arbitrary label path.
for path, directory in ((attempt_path.parent, True), (attempt_path, False)):
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid == 0 and not info.st_mode & 0o022)
require(json.loads(attempt_path.read_text()) == expected)
for target, source, writable, kind in (
    ("/run/mobius-boot-admission.py", "/usr/local/libexec/mobius-boot-admission.py", False, "bind"),
    ("/run/mobius-admission", "/var/lib/mobius-rebuild/admission/" + operation, True, "bind"),
    ("/data", configuration.get("data_dir"), True, None),
):
    require(isinstance(source, str) and source.startswith("/"))
    matches = [mount for mount in actual.get("Mounts", []) if mount.get("Destination") == target]
    require(len(matches) == 1)
    mount = matches[0]
    require(mount.get("Source") == source and mount.get("RW") is writable)
    require(kind is None or mount.get("Type") == kind)
PY_ADMISSION
}
if [[ $ADMISSION_MANAGED == 1 ]]; then
  verify_admission_identity
fi

git -C "$ROOT" diff --quiet HEAD -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  scripts/mobius-rebuild-launcher.py scripts/mobius-boot-admission.py \
  scripts/mobius-manual-cutover.py \
  scripts/rebuild-topology.py backend/scripts/prepare-container-replacement.py \
  backend/scripts/prepare-container-cutover.py \
  "${FILES[@]#"$ROOT/"}" \
  || { echo "Commit and review every helper and Compose input first." >&2; exit 1; }
[[ -z $(git -C "$ROOT" status --porcelain=v1 --untracked-files=all -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  scripts/mobius-rebuild-launcher.py scripts/mobius-boot-admission.py \
  scripts/mobius-manual-cutover.py \
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
if [[ -n $FROZEN_SOURCE ]]; then
  python3 - "$DATA_SOURCE" <<'PY_DATA'
import json, sys
with open("/etc/mobius-rebuild/config.json") as handle:
    configuration = json.load(handle)
if configuration.get("data_dir") != sys.argv[1]:
    raise SystemExit("Running data mount differs from the frozen helper configuration")
PY_DATA
fi
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

# Refuse before touching controller files or units. Recheck under replace.lock:
# a transaction can appear after this preflight. Even a legacy issued-start
# container cannot be retroactively enrolled in the new admission protocol.
refuse_unresolved_transaction() {
  if [[ -e /var/lib/mobius-rebuild/transaction.json \
        || -L /var/lib/mobius-rebuild/transaction.json ]]; then
    echo "Unresolved replacement transaction: preserve it and finish recovery before helper migration." >&2
    exit 1
  fi
}
refuse_unresolved_transaction

# Never freeze a per-attempt wrapper/token into the reusable base topology.
# The worker alone creates a fresh admission override for each exact container.
python3 - "$RESOLVED" <<'PY_BASE'
import json, sys
app = json.load(open(sys.argv[1]))["services"]["app"]
serialized = json.dumps(app)
if any(value in serialized for value in (
    "/run/mobius-boot-admission.py", "/run/mobius-admission",
    "io.mobius.admission.",
)):
    raise SystemExit("Base Compose topology contains boot-admission state; restore the unwrapped base before migration.")
PY_BASE

umask 077
# BEGIN MIGRATION DISPATCH QUIESCENCE
# Launcher 1 does not hold candidate.lock while selecting a worker. A selected
# child paused before replace.lock could otherwise outlive this installation.
# Stop only dispatch sources; never stop/kill a replacement or its recovery.
DISPATCH_UNITS=(mobius-rebuild.path mobius-rebuild-reconcile.timer)
DISPATCH_RESTORE=()
MIGRATION_COMPLETE=0
unit_state() {
  local info key value
  UNIT_LOAD= UNIT_ACTIVE= UNIT_JOB= UNIT_JOB_SEEN=0
  info=$(systemctl show "$1" --all --property=LoadState --property=ActiveState --property=Job) || return 1
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState) UNIT_LOAD=$value ;;
      ActiveState) UNIT_ACTIVE=$value ;;
      Job) UNIT_JOB=$value; UNIT_JOB_SEEN=1 ;;
    esac
  done <<<"$info"
  [[ $UNIT_LOAD == loaded || $UNIT_LOAD == not-found ]] \
    && [[ -n $UNIT_ACTIVE && $UNIT_JOB_SEEN == 1 ]]
}
migration_cleanup() {
  local rc=$? i
  trap - EXIT
  rm -f "$RESOLVED"
  # Release our fences before restoring dispatch. No active worker was stopped.
  exec 9>&- 8>&-
  if [[ $MIGRATION_COMPLETE == 0 ]]; then
    for i in "${!DISPATCH_RESTORE[@]}"; do
      systemctl "${DISPATCH_RESTORE[$i]}" "${DISPATCH_UNITS[$i]}" 2>/dev/null \
        || echo "Could not restore scheduling for ${DISPATCH_UNITS[$i]}; inspect systemd before retrying." >&2
    done
  fi
  exit "$rc"
}
# Snapshot BOTH sources before stopping either. Refuse transitional/queued
# states rather than guessing which scheduling state should be restored.
for unit in "${DISPATCH_UNITS[@]}"; do
  unit_state "$unit" || { echo "Cannot inspect dispatch unit $unit." >&2; exit 1; }
  [[ -z $UNIT_JOB || $UNIT_JOB == 0 ]] \
    || { echo "Dispatch unit $unit has a queued job; retry migration after it settles." >&2; exit 1; }
  case "$UNIT_ACTIVE" in
    active) DISPATCH_RESTORE+=(start) ;;
    inactive|failed) DISPATCH_RESTORE+=(stop) ;;
    *) echo "Dispatch unit $unit is transitioning; retry migration after it settles." >&2; exit 1 ;;
  esac
done
trap migration_cleanup EXIT
for unit in "${DISPATCH_UNITS[@]}"; do
  unit_state "$unit" || { echo "Cannot inspect dispatch unit $unit." >&2; exit 1; }
  if [[ $UNIT_LOAD != not-found ]]; then
    systemctl stop "$unit" || { echo "Cannot pause dispatch unit $unit." >&2; exit 1; }
  fi
done
# Inactive includes no delayed launcher/worker child in these oneshot units.
# Job must also be empty: an inactive service with a queued start is not fenced.
for unit in "${DISPATCH_UNITS[@]}" mobius-rebuild.service mobius-rebuild-reconcile.service; do
  unit_state "$unit" || { echo "Cannot inspect controller unit $unit." >&2; exit 1; }
  if [[ $UNIT_ACTIVE != inactive && $UNIT_ACTIVE != failed ]] \
      || [[ -n $UNIT_JOB && $UNIT_JOB != 0 ]]; then
    echo "Controller unit $unit is active or queued; leave recovery running and retry migration after it settles." >&2
    exit 1
  fi
done
# END MIGRATION DISPATCH QUIESCENCE
install -d -m 0700 /etc/mobius-rebuild /var/lib/mobius-rebuild
# Launcher 2 also cooperates with this lock order. Keep both locks through
# publication; unit quiescence above supplies the missing launcher-1 fence.
exec 8>>/var/lib/mobius-rebuild/candidate.lock
flock -w 30 8 \
  || { echo "A controller dispatch is active; retry helper migration after it settles." >&2; exit 1; }
exec 9>>/var/lib/mobius-rebuild/replace.lock
flock -w 30 9 \
  || { echo "A replacement is active; retry helper migration after it settles." >&2; exit 1; }
refuse_unresolved_transaction
[[ $(discover_app) == "$CID" ]] \
  || { echo "The running app changed during preflight; retry helper migration." >&2; exit 1; }
if [[ $ADMISSION_MANAGED == 1 ]]; then
  verify_admission_identity
fi

# BEGIN PINNED BOOT ADMISSION INSTALL
# Publish the prerequisite before adopting a worker that can use it. Existing
# bind mounts keep their original inode; no running container is changed.
python3 - "$ROOT/scripts/mobius-boot-admission.py" \
  "$ROOT/scripts/mobius-manual-cutover.py" <<'PY_GATE'
import os, stat, sys, tempfile
from pathlib import Path

sources = [Path(value) for value in sys.argv[1:]]
state = Path("/var/lib/mobius-rebuild/admission")
destinations = [Path("/usr/local/libexec/mobius-boot-admission.py"),
                Path("/usr/local/libexec/mobius-manual-cutover.py")]
if len(sources) != len(destinations):
    raise SystemExit("Both reviewed admission helpers are required")
destination = destinations[0]

def trusted_directory(path, *, private=False):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_gid != 0 or info.st_mode & 0o022
            or (private and stat.S_IMODE(info.st_mode) != 0o700)):
        raise SystemExit(f"Boot admission directory is not root-controlled: {path}")

for parent in (Path("/var"), Path("/var/lib"), state.parent,
               Path("/usr"), Path("/usr/local")):
    trusted_directory(parent)
try:
    destination.parent.mkdir(mode=0o755)
except FileExistsError:
    pass
trusted_directory(destination.parent)
try:
    state.mkdir(mode=0o700)
    os.chown(state, 0, 0)
except FileExistsError:
    pass
trusted_directory(state, private=True)
# Validate both sources and existing destinations before publishing either.
payloads = []
for source, destination in zip(sources, destinations):
    try:
        info = destination.lstat()
    except FileNotFoundError:
        pass
    else:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_gid != 0 or info.st_mode & 0o022):
            raise SystemExit("Installed admission helper is not root-controlled")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SystemExit("Admission helper source must be a regular file")
        payload = handle.read()
    compile(payload, str(source), "exec")
    payloads.append(payload)
for destination, payload in zip(destinations, payloads):
    fd, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            os.fchown(handle.fileno(), 0, 0)
            os.fchmod(handle.fileno(), 0o755)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        for directory in (destination.parent, state, state.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
PY_GATE
# END PINNED BOOT ADMISSION INSTALL

# Seed the active worker, including an exact still-pending image candidate.
# Refusal exits before publishing new units/capabilities; high-water alone
# does not mean the required worker is active. A genuinely newer active worker
# and any newer offered candidate are preserved.
MOBIUS_REBUILD_LOCK_HELD=1 \
  /usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" adopt-self
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
# A wrapped live app is discovered through this exact current attempt record.
# Preserve it on reinstall; never merge it into the reusable base snapshot.
if [[ $ADMISSION_MANAGED == 0 ]]; then
OVERRIDE_NEW=$(mktemp /etc/mobius-rebuild/image.override.XXXXXX)
cat >"$OVERRIDE_NEW" <<'EOF'
services:
  app:
    image: ${MOBIUS_IMAGE:?MOBIUS_IMAGE is required}
EOF
chmod 0600 "$OVERRIDE_NEW"
mv -f "$OVERRIDE_NEW" /etc/mobius-rebuild/image.override.yml
fi
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
# Run: pull 3600 + recovery allowance 3600 + preflight/provenance/cleanup 1800.
TimeoutStartSec=9000
# Recovery: six remove/create/start mutations * 300 + two readiness waits * 600
# + 600 for bounded observations and settlement. Interrupted work stays journaled.
TimeoutStopSec=3600
EOF
cat >/etc/systemd/system/mobius-rebuild.path <<EOF
[Unit]
Description=Watch for a Möbius container rebuild request

[Path]
# Edge-trigger requests; a queued request behind recovery must not spin this
# unit until systemd rate-limits the watcher. The timer supplies catch-up.
PathChanged=$DATA_SOURCE/mobius-rebuild/inbox/request.json
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
# Same recovery allowance: 6 * 300 mutations + 2 * 600 readiness + 600 overhead.
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/mobius-rebuild-reconcile.timer <<'EOF'
[Unit]
Description=Retry interrupted Möbius container recovery

[Timer]
OnBootSec=30
OnUnitInactiveSec=30
AccuracySec=1
# The worker dispatches queued requests and always reconciles in ExecStopPost.
Unit=mobius-rebuild.service

[Install]
WantedBy=timers.target
EOF
chmod 0644 /etc/systemd/system/mobius-rebuild.service \
  /etc/systemd/system/mobius-rebuild.path \
  /etc/systemd/system/mobius-rebuild-reconcile.service \
  /etc/systemd/system/mobius-rebuild-reconcile.timer
systemctl daemon-reload
# Installation never runs recovery or replaces the healthy app. Release the
# lock, then restore normal request/timer processing; those workers own recovery.
flock -u 9
exec 9>&-
MOBIUS_REBUILD_LAUNCHER=2 \
  /usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" publish-capabilities
flock -u 8
exec 8>&-
systemctl enable mobius-rebuild-reconcile.service
systemctl enable --now mobius-rebuild-reconcile.timer
systemctl enable --now mobius-rebuild.path
MIGRATION_COMPLETE=1

echo "Container rebuild support installed for Compose project '$PROJECT'."
echo "Topology: ${FILES[*]:-$FROZEN_SOURCE}"
echo "Rerun this installer after an intentional Compose topology change."
