#!/bin/bash
# This is the migration source of truth for the pre-rename core app slugs
# mind->memory and dreaming->reflection. Keep those old source slugs visible
# here until the supported migration window closes.
#
# This image-owned half moves slug-keyed on-disk state under /data/apps/<slug>,
# the editable skill file, cron logs, and the crontab entry. It deliberately
# does not touch /data/apps/<numeric-id>; the dialect-neutral database half
# preserves that row and numeric identity separately.
#
# The filesystem and crontab steps are guarded independently. The configured
# database is migrated later by SQLAlchemy; this image-owned script must not
# assume either SQLite or a fixed database path.
#
# Run before init_skills.py and install-core-apps.sh, as the mobius user, so live
# skill edits are moved before seeding and app rows are renamed before core app
# registration.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"

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
  local old="$1" new="$2"

  move_source_dir "$old" "$new"
  move_skill_file "$old" "$new"
  move_cron_logs "$old" "$new"
  rewrite_crontab "$old" "$new"
}

migrate_one mind memory
migrate_one dreaming reflection

# The boot caller records the filesystem-domain receipt only after every old
# slug-keyed path disappeared. Database proof is deliberately separate.
DATA_DIR="$DATA_DIR" python3 - <<'PYPROOF'
import glob
import os
import sys
from pathlib import Path

root = Path(os.environ["DATA_DIR"])
old = ("mind", "dreaming")
leftovers = []
for slug in old:
    if (root / "apps" / slug).exists():
        leftovers.append(f"source directory apps/{slug}")
    if (root / "shared" / "skills" / f"{slug}.md").exists():
        leftovers.append(f"skill {slug}.md")
    if glob.glob(str(root / "cron-logs" / f"{slug}.*")):
        leftovers.append(f"cron logs for {slug}")
if leftovers:
    print("migrate-app-rename: incomplete: " + ", ".join(leftovers), file=sys.stderr)
    raise SystemExit(1)
PYPROOF
proof_status=$?
if [ "$proof_status" -ne 0 ]; then
  exit "$proof_status"
fi
if crontab -l 2>/dev/null \
  | grep -Ev '^[[:space:]]*(#|$)' \
  | grep -Eq '/apps/(mind|dreaming)/'; then
  echo "migrate-app-rename: incomplete: crontab still references an old app slug" >&2
  exit 1
fi
