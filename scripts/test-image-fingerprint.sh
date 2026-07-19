#!/usr/bin/env bash
# Fingerprint inputs whose output is baked into the Docker test image.

set -euo pipefail

ROOT="${MOBIUS_TEST_IMAGE_INPUT_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}"
files=(
  Dockerfile
  backend/requirements.txt
  frontend/package.json
  frontend/package-lock.json
)

for file in "${files[@]}"; do
  sha256sum "$ROOT/$file" | cut -d' ' -f1
done | sha256sum | cut -d' ' -f1
