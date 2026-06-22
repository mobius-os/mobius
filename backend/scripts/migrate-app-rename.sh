#!/bin/bash
# migrate-app-rename.sh — idempotent in-place rename of the two core apps on an
# existing instance: mind→memory and dreaming→reflection.
#
# Renames each app's SLUG + display name in the DB, and moves the slug-keyed
# on-disk state (the source/config dir under /data/apps/<slug>/, the editable
# skill file, the cron-log files, and the crontab entry). It deliberately does
# NOT touch /data/apps/<numeric-id>/ — the app's numeric id is preserved, so its
# reports/storage (e.g. Reflection's brief history) stay exactly where they are.
#
# Why in-place rather than the install-core-apps "register new + archive old"
# pattern: Reflection owns real data (the briefs under its numeric-id dir). A
# fresh registration would mint a NEW numeric id and strand that history; an
# in-place slug rename keeps the same row + id + data.
#
# Idempotent: a no-op on a fresh instance (no old-slug app) and on one already
# migrated (the new-slug app already exists). Safe to run on every boot. Run as
# the `mobius` user (it writes /data + the mobius crontab; running as root would
# poison /data ownership and target the wrong crontab).
#
# MUST run BEFORE init_skills.py (which would otherwise re-seed a fresh
# reflection.md/memory.md and lose the agent's edits to the old file) and BEFORE
# install-core-apps.sh (which would otherwise register NEW memory/reflection
# apps and orphan the old ones).
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"
DB="$DATA_DIR/db/ultimate.db"
[ -f "$DB" ] || exit 0   # no DB yet (very first boot) → nothing to migrate

migrate_one() { # $1=old_slug  $2=new_slug  $3=new_display_name
  local old="$1" new="$2" name="$3" migrated_id

  # DB: rename slug+name in place ONLY when the old exists and the new doesn't.
  # Prints the (unchanged) numeric id when it migrated, nothing otherwise.
  migrated_id="$(DB="$DB" python3 -c '
import os, sqlite3, sys
old, new, name = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(os.environ["DB"])
cur = con.cursor()
o = cur.execute("select id from apps where slug=?", (old,)).fetchone()
n = cur.execute("select id from apps where slug=?", (new,)).fetchone()
if o and not n:
    cur.execute("update apps set slug=?, name=? where slug=?", (new, name, old))
    con.commit()
    print(o[0])
' "$old" "$new" "$name" 2>/dev/null)"

  [ -z "$migrated_id" ] && return 0   # fresh, or already migrated
  echo "migrate-app-rename: $old -> $new (app id $migrated_id; data preserved)"

  # source/config dir (slug-keyed) — NOT the numeric-id storage dir
  if [ -d "$DATA_DIR/apps/$old" ] && [ ! -e "$DATA_DIR/apps/$new" ]; then
    mv "$DATA_DIR/apps/$old" "$DATA_DIR/apps/$new"
  fi
  # editable skill file (preserve the agent's edits under the new name)
  if [ -f "$DATA_DIR/shared/skills/$old.md" ] && [ ! -e "$DATA_DIR/shared/skills/$new.md" ]; then
    mv "$DATA_DIR/shared/skills/$old.md" "$DATA_DIR/shared/skills/$new.md"
  fi
  # cron-log files: <old>.log/.lock/.heartbeat → <new>.*
  shopt -s nullglob
  for f in "$DATA_DIR"/cron-logs/"$old".*; do
    mv "$f" "$DATA_DIR/cron-logs/$new.${f##*"$old".}"
  done
  shopt -u nullglob
  # crontab: repoint /data/apps/<old>/ paths to /data/apps/<new>/
  if crontab -l 2>/dev/null | grep -q "/apps/$old/"; then
    crontab -l 2>/dev/null | sed "s#/apps/$old/#/apps/$new/#g" | crontab - 2>/dev/null \
      && echo "migrate-app-rename: repointed crontab $old -> $new"
  fi
}

migrate_one mind memory Memory
migrate_one dreaming reflection Reflection
exit 0
