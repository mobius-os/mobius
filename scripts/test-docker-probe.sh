#!/usr/bin/env bash
# Real-Docker regression for the timeout path. Requires mobius-test:ci.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
name="mobius-probe-regression-$$"
collision_name="${name}-collision"
collision_id=""

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  if [ -n "$collision_id" ]; then
    docker rm -f "$collision_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

set +e
"$ROOT/scripts/docker-probe.sh" --timeout 2 --name "$name" -- \
  -i --entrypoint python mobius-test:ci -u - <<'PY'
import time

print("probe-started", flush=True)
time.sleep(60)
PY
rc="$?"
set -e

if [ "$rc" -ne 124 ]; then
  echo "expected timeout exit 124, got $rc" >&2
  exit 1
fi
if docker inspect "$name" >/dev/null 2>&1; then
  echo "timed probe left container $name behind" >&2
  exit 1
fi
if python3 - "$name" <<'PY'
import os
import sys

needle = sys.argv[1].encode()
for entry in os.scandir("/proc"):
  if not entry.name.isdigit():
    continue
  try:
    cmdline = open(f"/proc/{entry.name}/cmdline", "rb").read()
  except OSError:
    continue
  if cmdline.startswith(b"docker\0run\0") and needle in cmdline:
    raise SystemExit(0)
raise SystemExit(1)
PY
then
  echo "timed probe left its Docker client behind" >&2
  exit 1
fi

collision_id=$(
  docker create --name "$collision_name" \
    --entrypoint python mobius-test:ci \
    -c 'import time; time.sleep(60)'
)
set +e
"$ROOT/scripts/docker-probe.sh" --timeout 2 --name "$collision_name" -- \
  --entrypoint true mobius-test:ci >/dev/null 2>&1
collision_rc="$?"
set -e
if [ "$collision_rc" -ne 125 ]; then
  echo "expected Docker name-conflict exit 125, got $collision_rc" >&2
  exit 1
fi
if ! docker inspect "$collision_id" >/dev/null 2>&1; then
  echo "probe removed a pre-existing container after a name conflict" >&2
  exit 1
fi
docker rm -f "$collision_id" >/dev/null
collision_id=""

echo "docker probe cleanup regression: PASS"
