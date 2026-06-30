#!/bin/sh
# Recovery restore — copies baked sources back over the live ones.
#
# Invoked by the recovery chat agent via Bash (it has filesystem write
# access), or directly via `docker exec`. There is no HTTP route that
# runs this — the agent runs the script itself. Each mode is
# idempotent: re-running is safe and produces the same result.
#
# Modes:
#   shell-dist      Restore the prebuilt frontend bundle (fast; no
#                   rebuild needed; serves immediately after restart).
#   shell-src       Restore the editable frontend source (the agent's
#                   edits to /data/shell/src/ are wiped). Requires a
#                   rebuild to take visual effect.
#   backend         Restore /app/app/ from /app/app-baked/ (skipping
#                   files listed in protected-files.txt — those are
#                   already root-owned and chmod 444, so cp -a would
#                   fail to overwrite them anyway).
#   scripts         Restore /app/scripts/ from /app/scripts-baked/.
#   platform        Git restore: `git -C /data/platform reset --hard HEAD`
#                   (reverts uncommitted agent edits; commits are preserved).
#                   Use when the agent made edits that broke the platform
#                   but hasn't committed them yet. Fast; no image needed.
#   platform-baked  Full restore: wipe /data/platform/app and
#                   /data/platform/scripts, recopy from baked floor, then
#                   commit the restore to /data/platform git history.
#                   Use when the agent broke something and committed it, or
#                   when git reset --hard is not enough.
#
# After 'backend', 'scripts', 'platform', or 'platform-baked', the
# caller should trigger POST /recover/restart so uvicorn picks up the
# restored code.
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
  shell-dist      Restore /data/shell/dist/ from /app/static/
  shell-src       Restore /data/shell/src/ from /app/shell-src/
  backend         Restore /app/app/ from /app/app-baked/
  scripts         Restore /app/scripts/ from /app/scripts-baked/
  platform        git reset --hard HEAD in /data/platform
  platform-baked  Wipe + recopy /data/platform/{app,scripts} from baked floor
EOF
  exit 1
fi

# --- platform mode: git reset --hard HEAD (reverts uncommitted edits) ---
if [ "$MODE" = "platform" ]; then
  if [ ! -d /data/platform/.git ]; then
    echo "platform restore: /data/platform/.git not found — run 'platform-baked' instead." >&2
    exit 2
  fi
  echo "Restoring /data/platform via git reset --hard HEAD..."
  # Clear pycache first so stale bytecache doesn't survive the reset.
  find /data/platform -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  find /data/platform -name '*.pyc' -delete 2>/dev/null || true
  if su -s /bin/sh mobius -c 'git -C /data/platform reset --hard HEAD && git -C /data/platform clean -fd'; then
    # clean -fd removes UNTRACKED files a bad change may have added — reset
    # --hard alone leaves them, so a stray module could survive (Codex). We use
    # -fd, NOT -fdx: -x would also delete GITIGNORED files, which now include
    # the recovery/core island (app/main.py, auth, recover_*, scripts/...). The
    # parent dirs are mobius-owned, so a mobius-run `clean -x` would unlink even
    # the root-owned 444 recovery files, leaving the overlay unbootable with no
    # recovery surface. -fd preserves everything gitignored.
    echo "platform restore: git reset --hard + clean -fd succeeded."
    # Clear the stale upgrade-available marker so a post-restore status read
    # does not fall back to its build sha (availability itself is the ancestry
    # check now, but the flag still seeds current_build_sha).
    rm -f /data/.platform-upgrade-available
    exit 0
  else
    echo "FATAL: git reset --hard HEAD failed in /data/platform" >&2
    exit 3
  fi
fi

# --- platform-baked mode: full wipe + recopy from baked floor -----------
if [ "$MODE" = "platform-baked" ]; then
  SRC_APP="/app/app-baked"
  SRC_SCR="/app/scripts-baked"
  DST_APP="/data/platform/app"
  DST_SCR="/data/platform/scripts"
  if [ ! -d "$SRC_APP" ]; then
    echo "Source missing: $SRC_APP (broken image?)" >&2
    exit 2
  fi
  echo "Restoring /data/platform from baked floor..."
  # Clear pycache before the overwrite to avoid stale bytecache.
  find "$DST_APP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  find "$DST_APP" -name '*.pyc' -delete 2>/dev/null || true
  find "$DST_SCR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  # Wipe first so files NOT in the baked floor (an agent-added module) do
  # not survive the restore — cp -a alone only overwrites (Codex).
  rm -rf "$DST_APP" "$DST_SCR"
  mkdir -p "$DST_APP" "$DST_SCR"
  cp_out=$(cp -a "$SRC_APP/." "$DST_APP/" 2>&1) || {
    echo "FATAL: cp -a $SRC_APP -> $DST_APP failed: $cp_out" >&2; exit 3
  }
  cp_out=$(cp -a "$SRC_SCR/." "$DST_SCR/" 2>&1) || {
    echo "FATAL: cp -a $SRC_SCR -> $DST_SCR failed: $cp_out" >&2; exit 3
  }
  # Re-open write access (baked copies are chmod a-w; cp -a preserves that).
  chmod -R u+w "$DST_APP" "$DST_SCR" 2>/dev/null || true
  chown -R mobius:mobius /data/platform 2>/dev/null || true
  # Re-enforce protected-file perms in the new platform tree.
  # The protected-files.txt entries use /app/app/ paths; those symlink to
  # /data/platform/app/ so chmod on /app/app/X acts on /data/platform/app/X.
  # We also walk with the real /data/platform/app/ prefix so the protection
  # holds whether the symlink exists or not.
  if [ -f /app/protected-files.txt ]; then
    while IFS= read -r line; do
      case "$line" in \#*|"") continue ;; esac
      case "$line" in
        /*) target="$line" ;;
        *)  target="/data/shell/$line" ;;
      esac
      if [ -f "$target" ]; then
        chown root:root "$target" 2>/dev/null || true
        case "$target" in
          *.sh) chmod 555 "$target" 2>/dev/null || true ;;
          *)    chmod 444 "$target" 2>/dev/null || true ;;
        esac
      fi
    done < /app/protected-files.txt
  fi
  # Commit the restore to /data/platform git history, and STAMP the baked floor
  # (.baked-sha + a baked-<sha> tag) in the SAME commit so /api/version + the
  # store read accurate right after a deploy instead of "update available" until
  # a manual tag (138). Kept under the "restore: platform-baked" subject so the
  # deploy's step-3b baseline diff still classifies it a system commit. Done as
  # mobius so the .git refs stay mobius-owned; BUILD_SHA is expanded by THIS
  # (root) shell because `su` resets the environment.
  if [ -d /data/platform/.git ]; then
    _bsha="${BUILD_SHA:-unknown}"
    su -s /bin/sh mobius -c "
      if [ '$_bsha' != 'unknown' ]; then printf '%s' '$_bsha' > /data/platform/.baked-sha; fi
      git -C /data/platform add -A
      git -C /data/platform commit -m 'restore: platform-baked restore from baked floor' 2>/dev/null || true
      if [ '$_bsha' != 'unknown' ]; then git -C /data/platform tag -f 'baked-$_bsha' HEAD 2>/dev/null || true; fi
    "
  fi
  # Clear the stale upgrade-available marker so a post-restore status read does
  # not fall back to its build sha (availability is the ancestry check now).
  rm -f /data/.platform-upgrade-available
  echo "Restore complete: /data/platform (platform-baked)"
  exit 0
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
          # .sh files need 555 (executable) so the next entrypoint
          # boot can run them. Other files (Python, configs) → 444.
          # Without the case-split, a direct `docker exec
          # recovery_restore.sh scripts` strips +x from entrypoint.sh
          # and recovery_restore.sh themselves, and the next
          # container restart fails with `permission denied`.
          case "$target" in
            *.sh) chmod 555 "$target" 2>/dev/null || true ;;
            *)    chmod 444 "$target" 2>/dev/null || true ;;
          esac
        fi
        ;;
    esac
  done < /app/protected-files.txt
fi

echo "Restore complete: $DST"
exit 0
