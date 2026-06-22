#!/bin/bash
# migrate-app-rename.sh — idempotent in-place rename of the two core apps on an
# existing instance: mind→memory and dreaming→reflection.
#
# Renames each app's SLUG + display name + SOURCE_DIR in the DB, and moves the
# slug-keyed on-disk state (the source/config dir under /data/apps/<slug>/, the
# editable skill file, the cron-log files, and the crontab entry). It
# deliberately does NOT touch /data/apps/<numeric-id>/ — the app's numeric id is
# preserved, so its reports/storage (e.g. Reflection's brief history) stay put.
#
# source_dir IS updated (load-bearing): register_app.py's _find_existing matches
# an existing app by source_dir, not name. If the migrated row kept the old
# source_dir, install-core-apps' re-sync would MISS it and register a DUPLICATE
# app — stranding the brief history. So the DB rename sets source_dir to the new
# path too.
#
# Why in-place rather than the install-core-apps "register new + archive old"
# pattern: Reflection owns real data (the briefs under its numeric-id dir). A
# fresh registration would mint a NEW numeric id and strand that history; an
# in-place rename keeps the same row + id + data.
#
# Idempotent + self-healing: a no-op on a fresh instance (no old-slug app) and on
# one already fully migrated. The DB step and the filesystem/crontab steps are
# DECOUPLED — each runs on its own guard (old present / new absent) — so a run
# interrupted after the DB commit but before the moves still completes on a later
# boot, and a row left half-migrated by an earlier buggy version (slug renamed
# but source_dir stale) is repaired. Safe to run on every boot.
#
# MUST run BEFORE init_skills.py (which would reseed a fresh reflection.md/
# memory.md and lose the agent's edits) and BEFORE install-core-apps.sh (which
# would register duplicates). Run as the `mobius` user (it writes /data + the
# mobius crontab; as root it would poison /data ownership + target root's crontab).
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"
DB="$DATA_DIR/db/ultimate.db"
[ -f "$DB" ] || exit 0   # no DB yet (very first boot) → nothing to migrate

migrate_one() { # $1=old_slug  $2=new_slug  $3=new_display_name
  local old="$1" new="$2" name="$3"
  local newdir="$DATA_DIR/apps/$new"

  # DB: rename slug+name+source_dir in place when the old slug exists and the new
  # doesn't; OR repair a half-migrated row (new slug present but source_dir still
  # points at the old dir — an earlier buggy run, or a partial run). Idempotent.
  DB="$DB" NEWDIR="$newdir" python3 - "$old" "$new" "$name" <<'PY' 2>&1 || true
import os, sqlite3, sys
old, new, name = sys.argv[1], sys.argv[2], sys.argv[3]
newdir = os.environ["NEWDIR"]
con = sqlite3.connect(os.environ["DB"]); cur = con.cursor()
o = cur.execute("select id, source_dir from apps where slug=?", (old,)).fetchone()
n = cur.execute("select id, source_dir from apps where slug=?", (new,)).fetchone()
if o and not n:
    cur.execute("update apps set slug=?, name=?, source_dir=? where slug=?",
                (new, name, newdir, old))
    con.commit()
    print(f"migrate-app-rename: DB {old} -> {new} (id {o[0]}, source_dir set)")
elif n and (n[1] or "").rstrip("/").endswith("/" + old):
    # half-migrated by an earlier run: slug already new, source_dir still old
    cur.execute("update apps set source_dir=? where slug=?", (newdir, new))
    con.commit()
    print(f"migrate-app-rename: repaired stale source_dir for {new} (id {n[0]})")
PY

  # Filesystem + crontab — run INDEPENDENTLY of the DB step (each guarded by
  # old-exists + new-absent) so a partial prior run still completes.
  if [ -d "$DATA_DIR/apps/$old" ] && [ ! -e "$newdir" ]; then
    mv "$DATA_DIR/apps/$old" "$newdir" && echo "migrate-app-rename: moved apps/$old -> apps/$new"
  fi
  if [ -f "$DATA_DIR/shared/skills/$old.md" ] && [ ! -e "$DATA_DIR/shared/skills/$new.md" ]; then
    mv "$DATA_DIR/shared/skills/$old.md" "$DATA_DIR/shared/skills/$new.md"
  fi
  shopt -s nullglob
  for f in "$DATA_DIR"/cron-logs/"$old".*; do
    mv "$f" "$DATA_DIR/cron-logs/$new.${f##*"$old".}"
  done
  shopt -u nullglob
  if crontab -l 2>/dev/null | grep -q "/apps/$old/"; then
    crontab -l 2>/dev/null | sed "s#/apps/$old/#/apps/$new/#g" | crontab - 2>/dev/null \
      && echo "migrate-app-rename: repointed crontab $old -> $new"
  fi
}

migrate_one mind memory Memory
migrate_one dreaming reflection Reflection
exit 0
