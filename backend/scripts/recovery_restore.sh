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
# baked copies). When this script runs from entrypoint AS ROOT (the
# normal post-flag-file path), root bypasses the dest chmod-444 so
# all files restore cleanly. When run AS MOBIUS (e.g. manual debug),
# cp -a's open-for-write on chmod-444 dest files fails with EACCES
# on protected files — which is fine because those files are already
# the right content (frozen + root-owned). We distinguish the two
# cases by checking euid: if we're root, ANY cp failure is real and
# fails the script.
cp_output=$(cp -a "$SRC/." "$DST/" 2>&1)
cp_status=$?
if [ $cp_status -ne 0 ]; then
  if [ "$(id -u)" -eq 0 ]; then
    echo "FATAL: cp -a failed while running as root:" >&2
    echo "$cp_output" >&2
    exit 3
  else
    # Mobius run: protected-file EACCES is expected. Surface other
    # errors but don't fail (operator can re-run as root if needed).
    echo "cp -a partial: protected files blocked overwrite (expected when run as mobius)" >&2
    echo "$cp_output" | grep -v "Permission denied" >&2 || true
  fi
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
