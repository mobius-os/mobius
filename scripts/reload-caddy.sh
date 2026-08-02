#!/bin/sh
set -eu

# Reload the bundled self-hosted edge from the host checkout's current
# Caddyfile. Updating a bind-mounted file does not guarantee that the already
# running Caddy process has loaded it, so validate and reload explicitly.
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_ROOT"

if ! docker compose version >/dev/null 2>&1; then
  echo "reload-caddy: Docker Compose is required." >&2
  exit 1
fi
if ! docker compose ps --status running --services | grep -qx caddy; then
  echo "reload-caddy: the bundled caddy service is not running." >&2
  echo "Run 'docker compose up -d --build' first." >&2
  exit 1
fi

docker compose exec -T caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T caddy \
  caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

echo "reload-caddy: active edge policy reloaded."
