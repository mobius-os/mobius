#!/bin/bash
# This is the migration source of truth for the pre-rename core app slugs
# mind->memory and dreaming->reflection. Keep those old source slugs visible
# here until the supported migration window closes.
#
# The migration renames each app's slug, display name, and source_dir in place,
# then moves slug-keyed on-disk state under /data/apps/<slug>, the editable skill
# file, cron logs, and the crontab entry. It deliberately does not touch
# /data/apps/<numeric-id>, so report history and id-keyed storage stay attached
# to the same app row.
#
# source_dir is load-bearing because register_app.py's _find_existing matches an
# existing app by source_dir, not name. A migrated row with the old source_dir
# would make install-core-apps register a duplicate and strand brief history.
#
# Reflection owns real data under its numeric app id, so this must be an
# in-place rename rather than install-core-apps' "register new and archive old"
# pattern.
#
# The database, filesystem, and crontab steps are guarded independently. A fresh
# instance, an already migrated instance, or a boot interrupted partway through a
# previous run can safely run this script again.
#
# Run before init_skills.py and install-core-apps.sh, as the mobius user, so live
# skill edits are moved before seeding and app rows are renamed before core app
# registration.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"
DB="$DATA_DIR/db/ultimate.db"
[ -f "$DB" ] || exit 0   # no db yet on first boot, so there is nothing to migrate.

# Return the first base-plus-extension path that does not already exist.
next_available_path() {
  local base="$1" ext="$2" candidate i
  candidate="${base}${ext}"
  if [ ! -e "$candidate" ]; then
    printf '%s\n' "$candidate"
    return
  fi
  i=1
  while :; do
    candidate="${base}.${i}${ext}"
    if [ ! -e "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
    i=$((i + 1))
  done
}

# Move a slug-keyed app source directory only when it cannot clobber another.
move_source_dir() {
  local old="$1" new="$2" olddir="$DATA_DIR/apps/$1" newdir="$DATA_DIR/apps/$2"
  if [ -d "$olddir" ] && [ ! -e "$newdir" ]; then
    mv "$olddir" "$newdir" && echo "migrate-app-rename: moved apps/$old -> apps/$new"
  elif [ -d "$olddir" ] && [ -e "$newdir" ]; then
    echo "migrate-app-rename: WARN source dir conflict for $old -> $new; preserved both" >&2
  fi
}

# Move or archive a renamed skill without leaving both names active.
move_skill_file() {
  local old="$1" new="$2"
  local oldfile="$DATA_DIR/shared/skills/$old.md"
  local newfile="$DATA_DIR/shared/skills/$new.md"
  local archive_dir archive
  if [ -f "$oldfile" ] && [ ! -e "$newfile" ]; then
    mv "$oldfile" "$newfile" && echo "migrate-app-rename: moved skill $old.md -> $new.md"
  elif [ -f "$oldfile" ] && [ -e "$newfile" ]; then
    archive_dir="$DATA_DIR/shared/skills/.rename-conflicts"
    mkdir -p "$archive_dir"
    archive="$(next_available_path "$archive_dir/$old.pre-rename" ".md")"
    mv "$oldfile" "$archive" \
      && echo "migrate-app-rename: WARN archived conflicting skill $old.md at $archive" >&2
  fi
}

# Move old cron logs to the new prefix without overwriting existing logs.
move_cron_logs() {
  local old="$1" new="$2" f suffix dest
  shopt -s nullglob
  for f in "$DATA_DIR"/cron-logs/"$old".*; do
    suffix="${f##*"$old".}"
    dest="$DATA_DIR/cron-logs/$new.$suffix"
    if [ -e "$dest" ]; then
      dest="$(next_available_path "$DATA_DIR/cron-logs/$new.pre-rename" ".$suffix")"
      mv "$f" "$dest" \
        && echo "migrate-app-rename: WARN preserved existing cron log $new.$suffix; moved old log to $dest" >&2
    else
      mv "$f" "$dest" && echo "migrate-app-rename: moved cron log $old.$suffix -> $new.$suffix"
    fi
  done
  shopt -u nullglob
}

# Rewrite only crontab command text, preserving comments and env lines.
rewrite_crontab() {
  local old="$1" new="$2" before after
  before="$(mktemp)"
  after="$(mktemp)"
  if ! crontab -l >"$before" 2>/dev/null; then
    rm -f "$before" "$after"
    return
  fi
  OLD_SLUG="$old" NEW_SLUG="$new" python3 -c '
import os
import sys

old = "/apps/{}/".format(os.environ["OLD_SLUG"])
new = "/apps/{}/".format(os.environ["NEW_SLUG"])
changed = False

for line in sys.stdin:
    raw = line.rstrip("\n")
    stripped = raw.lstrip()
    if not stripped or stripped.startswith("#"):
        print(raw)
        continue
    first = stripped.split(None, 1)[0]
    if "=" in first and not first.startswith("@"):
        print(raw)
        continue
    if stripped.startswith("@"):
        parts = stripped.split(None, 1)
        command_index = len(raw) if len(parts) == 1 else raw.find(parts[1])
    else:
        parts = stripped.split(None, 5)
        command_index = len(raw) if len(parts) < 6 else raw.find(parts[5])
    prefix = raw[:command_index]
    command = raw[command_index:]
    updated = command.replace(old, new)
    changed = changed or updated != command
    print(prefix + updated)

sys.exit(0 if changed else 3)
' <"$before" >"$after"
  case "$?" in
    0)
      cat "$after" | crontab - 2>/dev/null \
        && echo "migrate-app-rename: repointed crontab $old -> $new"
      ;;
    3)
      ;;
    *)
      echo "migrate-app-rename: WARN failed to rewrite crontab $old -> $new" >&2
      ;;
  esac
  rm -f "$before" "$after"
}

# Migrate one old app slug to its new platform identity.
migrate_one() {
  local old="$1" new="$2" name="$3"
  local newdir="$DATA_DIR/apps/$new"

  # Rename the row in place when only the old slug exists, repair stale
  # source_dir when only the new slug exists, and preserve both rows on conflict.
  DB="$DB" NEWDIR="$newdir" python3 - "$old" "$new" "$name" <<'PY' 2>&1 || true
import os, sqlite3, sys
old, new, name = sys.argv[1], sys.argv[2], sys.argv[3]
newdir = os.environ["NEWDIR"]
con = sqlite3.connect(os.environ["DB"]); cur = con.cursor()
if not cur.execute(
    "select 1 from sqlite_master where type='table' and name='apps'"
).fetchone():
    # A fresh image can contain the database file before FastAPI creates its
    # schema.  The filesystem/crontab migration below is deliberately
    # independent, so skip only this database step without a noisy traceback.
    sys.exit(0)
o = cur.execute("select id, source_dir from apps where slug=?", (old,)).fetchone()
n = cur.execute("select id, source_dir from apps where slug=?", (new,)).fetchone()
if o and not n:
    cur.execute("update apps set slug=?, name=?, source_dir=? where slug=?",
                (new, name, newdir, old))
    con.commit()
    print(f"migrate-app-rename: DB {old} -> {new} (id {o[0]}, source_dir set)")
elif o and n:
    print(
        f"migrate-app-rename: WARN db conflict for {old} -> {new}; "
        f"preserved old id {o[0]} and new id {n[0]}",
        file=sys.stderr,
    )
elif n and (n[1] or "").rstrip("/").endswith("/" + old):
    cur.execute("update apps set source_dir=? where slug=?", (newdir, new))
    con.commit()
    print(f"migrate-app-rename: repaired stale source_dir for {new} (id {n[0]})")
PY

  move_source_dir "$old" "$new"
  move_skill_file "$old" "$new"
  move_cron_logs "$old" "$new"
  rewrite_crontab "$old" "$new"
}

migrate_one mind memory Memory
migrate_one dreaming reflection Reflection
exit 0
