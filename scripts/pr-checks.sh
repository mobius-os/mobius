#!/usr/bin/env bash
# Compatibility entry point: 0 finished (pass or fail), 1 silent pending,
# 2 diagnostic when the observation is broken or the PR head was replaced.
exec python3 "$(dirname "${BASH_SOURCE[0]}")/pr-checks.py" "$@"
