#!/bin/sh
# recovery_snapshot.sh — rescue uncommitted local edits BEFORE a
# destructive restore or crash-loop overwrite copies the baked floor over
# them.
#
# Why this exists: the platform self-update path commits local edits first
# and refuses if the tree is still dirty, so a clean update never loses
# work. The RESTORE paths do not have that discipline — recovery_restore.sh
# (git reset --hard HEAD / cp baked over the tree) and the entrypoint
# crash-loop auto-restore overwrite the live worktree directly, so any
# uncommitted agent/owner edit is gone with nothing to recover from. That
# asymmetry is the verified "my settings got removed" loss class. This
# helper closes it: every tree-replacing op snapshots first.
#
# Contract: BEST-EFFORT and BOUNDED. A snapshot failure or slowness must
# NEVER abort a restore or block boot. Callers invoke it guarded
# (`... || true`) and the boot-critical caller additionally wraps it in
# `timeout`. It always exits 0.
#
# Usage: recovery_snapshot.sh <label> [path ...]
#   label  short tag recorded in the rescue dir name + manifest
#   path   dirs/files to snapshot; defaults to the agent-editable source
#          trees (platform app/scripts + shell src). Build output, vendored
#          deps, bytecache, and git internals are always excluded — they're
#          regenerable and huge (shell/node_modules alone is ~30k files and
#          would blow the boot health window).
#
# Snapshots land in /data/.rescue/<UTC-ts>-<label>-<pid>/ as per-target
# .tar.gz files plus a manifest.txt. Old snapshots (>14 days) are pruned.

set -u

LABEL="${1:-restore}"
if [ "$#" -gt 0 ]; then shift; fi

# Default to the agent-editable source trees. Each is small once vendored
# deps / build output are excluded, so snapshotting the union on every
# restore (even a mode that only touches one of them) is cheap and means
# the rescue is always comprehensive.
if [ "$#" -eq 0 ]; then
  set -- /data/platform/app /data/platform/scripts /data/shell/src
fi

# Rescue output root. Defaults to /data/.rescue; MOBIUS_RESCUE_ROOT lets a
# test harness redirect it without touching the production path.
RESCUE_ROOT="${MOBIUS_RESCUE_ROOT:-/data/.rescue}"

# Make the rescue root sticky + world-writable (like /tmp) so BOTH root (the
# crash-loop / boot-time .recover-pending restore) and the mobius user (an
# agent-invoked restore) can create snapshot subdirs — regardless of which
# created the root first or whether the boot-time `chown -R /data` has run
# yet. The first creator owns it so its chmod succeeds; later callers find it
# already 1777 and their failed chmod is harmless.
mkdir -p "$RESCUE_ROOT" 2>/dev/null || true
chmod 1777 "$RESCUE_ROOT" 2>/dev/null || true

# A failed mkdir (read-only volume, etc.) is not fatal — just skip.
TS=$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo unknown)
DEST="${RESCUE_ROOT}/${TS}-${LABEL}-$$"
if ! mkdir "$DEST" 2>/dev/null; then
  echo "recovery_snapshot: could not create $DEST — skipping snapshot" >&2
  exit 0
fi

_saved=0
for tgt in "$@"; do
  # Only absolute paths: tar runs with `-C /` + "${tgt#/}", so a relative
  # arg like `app` would archive /app (the baked floor), not the editable
  # tree. Production callers pass absolute paths; guard manual/test misuse.
  case "$tgt" in
    /*) ;;
    *) echo "recovery_snapshot: skipping non-absolute target '$tgt'" >&2; continue ;;
  esac
  [ -e "$tgt" ] || continue
  # A symlinked target would archive the link, not the tree; production
  # targets are real dirs, so that's accepted (tar -h would risk loops).
  # Flatten the absolute path into a single archive name.
  name=$(printf '%s' "$tgt" | sed 's#^/##; s#/#_#g')
  # Archive paths relative to / so extraction is unambiguous. node_modules,
  # dist, bytecache, and .git are excluded (regenerable / huge / internal).
  if tar -C / \
      --exclude='*/node_modules' --exclude='*/dist' \
      --exclude='*/__pycache__' --exclude='*.pyc' --exclude='*/.git' \
      -czf "$DEST/${name}.tar.gz" "${tgt#/}" 2>/dev/null; then
    _saved=$((_saved + 1))
  fi
done

{
  echo "label: $LABEL"
  echo "when: $TS"
  echo "pid: $$"
  echo "targets: $*"
  echo "archives_written: $_saved"
} > "$DEST/manifest.txt" 2>/dev/null || true

if [ "$_saved" -eq 0 ]; then
  # Nothing got archived (empty/missing targets, or tar absent). Drop the
  # dir (manifest and all) so /data/.rescue doesn't fill with empty noise.
  rm -rf "$DEST" 2>/dev/null || true
  echo "recovery_snapshot: no archives written for [$*]" >&2
else
  echo "recovery_snapshot: rescued $_saved target(s) to $DEST" >&2
fi

# TTL purge: drop rescue snapshots older than 14 days. Best-effort; a small
# host shouldn't accumulate them, but a runaway crash-loop could.
find "$RESCUE_ROOT" -maxdepth 1 -type d -name '2*' -mtime +14 \
  -exec rm -rf {} + 2>/dev/null || true

# Count cap: keep at most the newest 30 snapshots so a fast persistent
# crash-loop (a snapshot every 3rd boot) can't saturate /data well before
# the 14-day TTL would prune them.
ls -1dt "$RESCUE_ROOT"/2* 2>/dev/null | tail -n +31 | while IFS= read -r _old; do
  rm -rf "$_old" 2>/dev/null || true
done

# Always succeed — a snapshot is a safety net, never a gate.
printf '%s\n' "$DEST"
exit 0
