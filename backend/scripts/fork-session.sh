#!/bin/bash
# fork-session.sh [--json] [<provider>] <session_id> <cwd> "<coaching prompt>"
#
# Forks one preserved provider session and interviews that exact fork. There is
# deliberately no transcript reseed or evidence-only fallback: if the provider
# cannot fork the source session, Agent Coaching is unavailable for that run.
# The three-argument form remains Claude for existing app-owned callers; new
# callers may name either provider explicitly.
set -euo pipefail

exec python3 "$(dirname "$0")/fork_session.py" "$@"
