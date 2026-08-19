#!/usr/bin/env bash
set -euo pipefail

# Install the fixed-verb self-host rebuild controller from a trusted checkout.
# Run on the Docker host, from the exact checkout/Compose configuration that
# owns the live Möbius app. This never downloads executable installer code.

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo from the trusted Möbius checkout." >&2
  exit 1
fi
command -v docker >/dev/null
docker compose version >/dev/null
command -v ssh-keygen >/dev/null
command -v systemd-run >/dev/null
command -v git >/dev/null

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "Run from a committed Möbius checkout, not copied app data." >&2; exit 1; }
git -C "$ROOT" ls-files --error-unmatch \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  docker-compose.yml >/dev/null \
  || { echo "The rebuild helper must be tracked in the trusted checkout." >&2; exit 1; }
git -C "$ROOT" diff --quiet HEAD -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  docker-compose.yml docker-compose.prod.yml \
  || { echo "Commit and review the rebuild/helper Compose changes before installing." >&2; exit 1; }
PROJECT=${COMPOSE_PROJECT_NAME:-$(basename "$ROOT")}
FILES=("$ROOT/docker-compose.yml")
[[ -f "$ROOT/docker-compose.prod.yml" && ${MOBIUS_USE_PROD_OVERLAY:-0} == 1 ]] \
  && FILES+=("$ROOT/docker-compose.prod.yml")
for file in "${FILES[@]}"; do
  git -C "$ROOT" ls-files --error-unmatch "${file#"$ROOT"/}" >/dev/null \
    || { echo "Every selected Compose file must be tracked in the trusted checkout." >&2; exit 1; }
done
[[ -z $(git -C "$ROOT" status --porcelain=v1 --untracked-files=all -- \
  scripts/install-rebuild-helper.sh scripts/mobius-rebuild-host.py \
  "${FILES[@]#"$ROOT"/}") ]] \
  || { echo "Commit and review every selected helper/Compose input before installing." >&2; exit 1; }
ARGS=(-p "$PROJECT")
for file in "${FILES[@]}"; do ARGS+=(-f "$file"); done

cd "$ROOT"
CID=$(docker compose "${ARGS[@]}" ps -q app)
[[ -n $CID ]] || { echo "The recorded Möbius app service is not running." >&2; exit 1; }

umask 077
install -d -m 0700 /etc/mobius-rebuild /var/lib/mobius-rebuild
install -D -m 0755 "$ROOT/scripts/mobius-rebuild-host.py" /usr/local/libexec/mobius-rebuild-host
cat >/usr/local/libexec/mobius-rebuild-ssh <<'EOF'
#!/bin/sh
case "${SSH_ORIGINAL_COMMAND:-}" in
  status|rebuild) exec sudo /usr/local/libexec/mobius-rebuild-host dispatch "$SSH_ORIGINAL_COMMAND" ;;
  *) echo "unsupported command" >&2; exit 2 ;;
esac
EOF
chmod 0755 /usr/local/libexec/mobius-rebuild-ssh

# Freeze the fully resolved deployment definition under root ownership. The
# worker never re-opens files in the checkout, so later writes by the app (or
# by an unprivileged checkout owner) cannot turn the fixed rebuild verb into a
# privileged Compose change. Re-run this installer after an intentional
# topology/configuration change.
SNAPSHOT_TMP=$(mktemp /etc/mobius-rebuild/compose.XXXXXX)
docker compose "${ARGS[@]}" config >"$SNAPSHOT_TMP"
chmod 0600 "$SNAPSHOT_TMP"
mv -f "$SNAPSHOT_TMP" /etc/mobius-rebuild/compose.yml
cat >/etc/mobius-rebuild/image.override.yml <<'EOF'
services:
  app:
    image: ${MOBIUS_IMAGE:?MOBIUS_IMAGE is required}
EOF
chmod 0600 /etc/mobius-rebuild/image.override.yml
FROZEN_ARGS=(-p "$PROJECT" -f /etc/mobius-rebuild/compose.yml \
  -f /etc/mobius-rebuild/image.override.yml)
FROZEN_IMAGE=$(MOBIUS_IMAGE=ghcr.io/mobius-os/mobius \
  docker compose "${FROZEN_ARGS[@]}" config --images app)
[[ $FROZEN_IMAGE == ghcr.io/mobius-os/mobius ]] \
  || { echo "The frozen Compose snapshot did not preserve the fixed app image." >&2; exit 1; }

python3 - "$PROJECT" "$(git -C "$ROOT" rev-parse HEAD)" <<'PY'
import json, os, sys, tempfile
project, source_commit = sys.argv[1:]
value = {"version": 2, "directory": "/etc/mobius-rebuild", "project": project,
         "service": "app", "files": ["/etc/mobius-rebuild/compose.yml",
         "/etc/mobius-rebuild/image.override.yml"],
         "image": "ghcr.io/mobius-os/mobius", "source_commit": source_commit}
fd, name = tempfile.mkstemp(dir="/etc/mobius-rebuild", text=True)
with os.fdopen(fd, "w") as f: json.dump(value, f, separators=(",", ":"))
os.chmod(name, 0o600); os.replace(name, "/etc/mobius-rebuild/config.json")
PY

if ! id mobius-rebuild >/dev/null 2>&1; then
  # sshd starts every forced command through the account shell. `nologin`
  # would reject the session before the root-owned forced-command wrapper can
  # run. The system account stays password-locked and its only authorized key
  # is restricted below, so /bin/sh is an execution mechanism, not a login
  # grant.
  useradd --system --create-home --shell /bin/sh mobius-rebuild
else
  usermod --shell /bin/sh mobius-rebuild
fi
HOME_DIR=$(getent passwd mobius-rebuild | cut -d: -f6)
install -d -o mobius-rebuild -g mobius-rebuild -m 0700 "$HOME_DIR/.ssh"
KEY_DIR=$(mktemp -d)
trap 'rm -rf "$KEY_DIR"' EXIT
ssh-keygen -q -t ed25519 -N '' -f "$KEY_DIR/id_ed25519"
PUB=$(cat "$KEY_DIR/id_ed25519.pub")
printf 'restrict,command="/usr/local/libexec/mobius-rebuild-ssh" %s\n' "$PUB" \
  > "$HOME_DIR/.ssh/authorized_keys"
chown mobius-rebuild:mobius-rebuild "$HOME_DIR/.ssh/authorized_keys"
chmod 0600 "$HOME_DIR/.ssh/authorized_keys"
cat >/etc/sudoers.d/mobius-rebuild <<'EOF'
mobius-rebuild ALL=(root) NOPASSWD: /usr/local/libexec/mobius-rebuild-host dispatch status, /usr/local/libexec/mobius-rebuild-host dispatch rebuild
EOF
chmod 0440 /etc/sudoers.d/mobius-rebuild
visudo -cf /etc/sudoers.d/mobius-rebuild >/dev/null

# Install the private client material directly into the persistent app volume.
docker exec -u 0 "$CID" install -d -o mobius -g mobius -m 0700 /data/cli-auth/mobius-rebuild
docker cp "$KEY_DIR/id_ed25519" "$CID:/data/cli-auth/mobius-rebuild/id_ed25519"
GATEWAY=$(docker inspect "$CID" --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{"\n"}}{{end}}' | head -n1)
[[ -n $GATEWAY ]] || { echo "Could not determine the container's host gateway." >&2; exit 1; }
ssh-keyscan -H "$GATEWAY" > "$KEY_DIR/known_hosts" 2>/dev/null
[[ -s "$KEY_DIR/known_hosts" ]] || { echo "The host SSH server is not reachable from the container network." >&2; exit 1; }
docker cp "$KEY_DIR/known_hosts" "$CID:/data/cli-auth/mobius-rebuild/known_hosts"
cat >"$KEY_DIR/connection.json" <<EOF
{"version":1,"host":"$GATEWAY","port":22,"user":"mobius-rebuild","identity_file":"/data/cli-auth/mobius-rebuild/id_ed25519","known_hosts_file":"/data/cli-auth/mobius-rebuild/known_hosts"}
EOF
docker cp "$KEY_DIR/connection.json" "$CID:/data/cli-auth/mobius-rebuild/connection.json"
docker exec -u 0 "$CID" chown -R mobius:mobius /data/cli-auth/mobius-rebuild
docker exec -u 0 "$CID" chmod 0700 /data/cli-auth/mobius-rebuild
docker exec -u 0 "$CID" chmod 0600 /data/cli-auth/mobius-rebuild/*

echo "Rebuild helper installed for Compose project '$PROJECT'."
echo "The root-owned Compose snapshot must be refreshed by rerunning this installer after topology changes."
echo "Rebuild the app image once so the container includes the SSH client, then Settings can use it."
