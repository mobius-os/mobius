#!/usr/bin/env bash
# Real-Docker regression for the timeout path. Requires mobius-test:ci.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
name="mobius-probe-regression-$$"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
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

echo "docker probe cleanup regression: PASS"
