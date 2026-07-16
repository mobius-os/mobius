#!/usr/bin/env bash
# Fingerprint inputs whose output is baked into the Docker test image.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
files=(
  Dockerfile
  backend/requirements.txt
  frontend/package.json
  frontend/package-lock.json
  backend/scripts/build-react-vendor.mjs
  backend/scripts/build-codemirror-vendor.mjs
  backend/scripts/build-recharts-vendor.mjs
  backend/scripts/build-date-fns-vendor.mjs
  backend/scripts/build-d3-geo-vendor.mjs
  backend/scripts/build-marked-vendor.mjs
  backend/scripts/build-dompurify-vendor.mjs
)

for file in "${files[@]}"; do
  sha256sum "$ROOT/$file" | cut -d' ' -f1
done | sha256sum | cut -d' ' -f1
