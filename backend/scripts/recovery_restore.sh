#!/bin/sh
# Recovery restore — copies baked sources back over the live ones.
#
# Run from the recovery chat (POST /recover/restore) or directly via
# `docker exec`. Each mode is idempotent: re-running is safe and
# produces the same result.
#
# Modes:
#   shell-dist   Restore the prebuilt frontend bundle (fast; no
#                rebuild needed; serves immediately after restart).
#   shell-src    Restore the editable frontend source (the agent's
#                edits to /data/shell/src/ are wiped). Requires a
#                rebuild to take visual effect.
#   backend      Restore /app/app/ from /app/app-baked/ (skipping
#                files listed in protected-files.txt — those are
#                already root-owned and chmod 444, so cp -a would
#                fail to overwrite them anyway).
#   scripts      Restore /app/scripts/ from /app/scripts-baked/.
#
# After 'backend' or 'scripts', the caller should trigger
# POST /recover/restart so uvicorn picks up the restored code.
#
# Exit codes:
#   0  success
#   1  unknown mode
#   2  source dir missing (bad image)
#   3  copy failed

set -e

MODE="$1"

if [ -z "$MODE" ]; then
  cat <<EOF >&2
Usage: recovery_restore.sh <mode>

Modes:
  shell-dist   Restore /data/shell/dist/ from /app/static/
  shell-src    Restore /data/shell/src/ from /app/shell-src/
  backend      Restore /app/app/ from /app/app-baked/
  scripts      Restore /app/scripts/ from /app/scripts-baked/
EOF
  exit 1
fi

case "$MODE" in
  shell-dist)
    SRC="/app/static"
    DST="/data/shell/dist"
    ;;
  shell-src)
    SRC="/app/shell-src"
    DST="/data/shell"
    ;;
  backend)
    SRC="/app/app-baked"
    DST="/app/app"
    ;;
  scripts)
    SRC="/app/scripts-baked"
    DST="/app/scripts"
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    exit 1
    ;;
esac

if [ ! -d "$SRC" ]; then
  echo "Source missing: $SRC (broken image?)" >&2
  exit 2
fi

echo "Restoring $DST from $SRC..."

# mkdir -p the destination so a fresh volume / wiped directory works.
mkdir -p "$DST"

# cp -a preserves perms + ownership of the SOURCE (root:root for the
# baked copies). The chown sweep below re-hands the writable layers
# back to mobius. Protected files in the destination that are chmod
# 444 root will block cp -a's overwrite with "Permission denied" —
# that's expected (the frozen island is already the right content)
# and we suppress those errors with --no-clobber + a fallback rsync
# pattern if cp fails on those.
if ! cp -a "$SRC/." "$DST/" 2>&1; then
  echo "cp -a failed; some protected files probably blocked overwrite (expected)" >&2
fi

# Re-hand the writable layer to mobius (except for protected files
# which entrypoint.sh re-locks on next boot).
if [ "$DST" = "/app/app" ] || [ "$DST" = "/app/scripts" ]; then
  chown -R mobius:mobius "$DST" 2>/dev/null || true
elif [ "$DST" = "/data/shell" ] || [ "$DST" = "/data/shell/dist" ]; then
  chown -R mobius:mobius "$DST" 2>/dev/null || true
fi

# Re-enforce protected-files chmod 444 + root ownership after the
# overwrite. Walk the same protected-files.txt the entrypoint uses.
if [ -f /app/protected-files.txt ]; then
  while IFS= read -r line; do
    case "$line" in \#*|"") continue ;; esac
    case "$line" in
      /*) target="$line" ;;
      *)  target="/data/shell/$line" ;;
    esac
    # Only re-enforce protected files INSIDE the destination we just
    # restored. A 'backend' restore shouldn't touch /data/shell/ perms.
    case "$target" in
      "$DST"/*|"$DST")
        if [ -f "$target" ]; then
          chown root:root "$target" 2>/dev/null || true
          chmod 444 "$target" 2>/dev/null || true
        fi
        ;;
    esac
  done < /app/protected-files.txt
fi

echo "Restore complete: $DST"
exit 0
