#!/usr/bin/env bash
# The Project app owns the renderer; the selected project's scene is its input.
set -euo pipefail
: "${PROJECT_ROOT:?}" "${PROJECT_SOURCE:?}" "${PROJECT_OUTPUT_DIR:?}"
python3 "$(dirname "$0")/build.py"
