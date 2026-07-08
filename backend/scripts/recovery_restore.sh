#!/bin/sh
# Recovery restore — copies baked sources back over the live ones.
#
# Invoked by the recovery chat agent via Bash (it has filesystem write
# access), or directly via `docker exec`. There is no HTTP route that
# runs this — the agent runs the script itself. Each mode is
# idempotent: re-running is safe and produces the same result.
#
# Modes:
#   platform        Git restore: `git -C /data/platform reset --hard HEAD`
#                   (reverts uncommitted agent edits; commits are preserved).
#                   Use when the agent made edits that broke the platform
#                   but hasn't committed them yet. Fast; no image needed.
#   platform-baked  Full restore: quarantine /data/platform and re-seed the
#                   whole served clone tree from /app/platform-baked. Use when
#                   the agent broke something and committed it, or git reset
#                   --hard is not enough.
#
# After 'platform' or 'platform-baked', the caller should trigger
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
  platform        git reset --hard HEAD in /data/platform
  platform-baked  Quarantine + re-seed /data/platform from /app/platform-baked
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

# --- platform-baked mode: whole-tree re-seed from baked clone -----------
if [ "$MODE" = "platform-baked" ]; then
  SRC="/app/platform-baked"
  DST="/data/platform"
  if [ ! -d "$SRC/.git" ]; then
    echo "Source missing: $SRC/.git (broken image?)" >&2
    exit 2
  fi

  _ts=$(date -u +%Y%m%dT%H%M%SZ)
  _tmp="/data/platform.reseeding.$_ts.$$"
  _quar=""
  _env_scrub="env -u PYTHONPATH -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY -u GIT_COMMON_DIR -u GIT_NAMESPACE"

  _restore_quarantine() {
    if [ -n "$_quar" ] && [ ! -e "$DST" ]; then
      mv "$_quar" "$DST" 2>/dev/null || true
      chown -R mobius:mobius "$DST" 2>/dev/null || true
    fi
  }

  echo "Restoring /data/platform from baked clone..."

  if [ -e "$DST" ]; then
    if [ -n "$(ls -A "$DST" 2>/dev/null)" ]; then
      _quar="/data/platform.reseed-prev.$_ts.$$"
      if mv "$DST" "$_quar" 2>/dev/null; then
        echo "Existing /data/platform preserved at $_quar (NOT deleted)." >&2
      else
        echo "FATAL: could not quarantine existing /data/platform; refusing to overwrite it." >&2
        exit 3
      fi
    else
      rm -rf "$DST" 2>/dev/null || true
    fi
  fi

  rm -rf /data/platform.reseeding.* 2>/dev/null || true
  mkdir -p "$_tmp"
  chown mobius:mobius "$_tmp" 2>/dev/null || true
  if ! _RESEEDING="$_tmp" su -s /bin/sh mobius -c 'cp -a /app/platform-baked/. "$_RESEEDING"'; then
    echo "FATAL: cp -a $SRC -> $_tmp failed" >&2
    rm -rf "$_tmp" 2>/dev/null || true
    _restore_quarantine
    exit 3
  fi

  chown -R mobius:mobius "$_tmp" 2>/dev/null || true
  if [ ! -f "$_tmp/.baked-sha" ]; then
    echo "${BUILD_SHA:-unknown}" > "$_tmp/.baked-sha"
    chown mobius:mobius "$_tmp/.baked-sha" 2>/dev/null || true
  fi

  if ! _RESEEDING="$_tmp" su -s /bin/sh mobius -c \
    'git -C "$_RESEEDING" rev-parse --is-inside-work-tree >/dev/null &&
     git -C "$_RESEEDING" rev-parse --verify HEAD >/dev/null'; then
    echo "FATAL: reseeded platform failed git validation" >&2
    rm -rf "$_tmp" 2>/dev/null || true
    _restore_quarantine
    exit 3
  fi
  if ! su -s /bin/sh mobius -c \
    "cd '$_tmp/backend' && $_env_scrub timeout 60 python3 -c 'import app.main'"; then
    echo "FATAL: reseeded platform failed import validation" >&2
    rm -rf "$_tmp" 2>/dev/null || true
    _restore_quarantine
    exit 3
  fi

  if [ -e "$DST" ]; then
    if [ -n "$(ls -A "$DST" 2>/dev/null)" ]; then
      echo "FATAL: /data/platform became non-empty before install; refusing to overwrite it." >&2
      rm -rf "$_tmp" 2>/dev/null || true
      _restore_quarantine
      exit 3
    fi
    rm -rf "$DST" 2>/dev/null || true
  fi
  if ! mv -T "$_tmp" "$DST" 2>/dev/null; then
    echo "FATAL: could not move reseeded platform into place" >&2
    rm -rf "$_tmp" 2>/dev/null || true
    _restore_quarantine
    exit 3
  fi

  chown -R mobius:mobius "$DST" 2>/dev/null || true
  rm -f /data/.platform-upgrade-available /data/.platform-conflict \
    /data/.platform-rolled-back /data/.platform-offline \
    /data/.platform-restart-needed
  echo "Restore complete: /data/platform (platform-baked)"
  exit 0
fi

echo "Unknown mode: $MODE" >&2
exit 1
