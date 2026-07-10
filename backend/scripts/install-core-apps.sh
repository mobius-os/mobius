#!/bin/bash
# install-core-apps.sh — retired compatibility hook.
#
# Installable apps are no longer seeded from platform-owned core-app snapshots.
# The App Store is bootstrapped by backend/app/bootstrap.py; Memory, Reflection,
# Beat Machine, and every other catalog app install/update into /data/apps via
# the App Store manifest flow. This script remains because the entrypoint calls
# it on existing deployments.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"
LOG="$DATA_DIR/cron-logs/install-core-apps.log"
mkdir -p "$DATA_DIR/cron-logs"
echo "[$(date -Iseconds)] install-core-apps: no platform core apps to seed" >>"$LOG"
