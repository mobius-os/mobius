#!/usr/bin/env bash
# Fingerprint inputs whose output is baked into the Docker test image.

set -euo pipefail

ROOT="${MOBIUS_TEST_IMAGE_INPUT_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}"
CLASSIFIER="$ROOT/backend/app/platform_activation.py"
mapfile -t files < <(
  python3 "$CLASSIFIER" dependency-fingerprint-paths "$ROOT"
)

[ "${#files[@]}" -gt 0 ] || {
  echo "No image dependency inputs were classified." >&2
  exit 1
}

for file in "${files[@]}"; do
  sha256sum "$ROOT/$file" | cut -d' ' -f1
done | sha256sum | cut -d' ' -f1
