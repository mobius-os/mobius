#!/usr/bin/env python3
"""One idempotent backup run of the owner data that lives OUTSIDE the
/data git safety net.

Why this exists
---------------
`/data` is a git repo (owned by mobius) whose nightly Dreaming commit is
the "undo" for shared/memory and shared/skills. But that safety net is
on the SAME docker volume it protects, and it deliberately gitignores
the volatile/secret/runtime trees (db/, cli-auth/, app-secrets/,
apps/*/data/, chats/, compiled/). So a lost or corrupted volume takes
BOTH the data and its git history with it — unrecoverable by procedure.
This script captures a self-contained artifact of everything on the
volume that a fresh boot could not re-create from the image, so
restore-data.py can rebuild the instance elsewhere.

What it captures (per the live /data layout, not a hardcoded list)
------------------------------------------------------------------
It walks /data and includes everything EXCEPT trees that are transient
(logs, browser profiles, run markers), rebuilt from the image on boot
(platform/), or this script's own output (the backup target). That
fail-safe "include unless excluded" stance means a future /data dir is
captured by default rather than silently missed. Entries are routed
into two archives by sensitivity:

  data.tar.gz     the consistent SQLite snapshot (see below) plus every
                  non-secret tree: apps/, chats/, compiled/, shared/,
                  and anything else new. shared/ is git-tracked but the
                  git repo dies with the volume, so a true DR artifact
                  must carry it too.
  secrets.tar.*   cli-auth/, app-secrets/, push/, service-token.txt,
                  .secret-key, .recovery-secret, .recovery-owner.json.
                  These decrypt the instance's JWTs and provider auth —
                  they must never sit in a backup as plaintext that
                  leaves the host.

The DB is snapshotted with the sqlite3 online backup API (never a raw
file copy — ultimate.db runs in WAL mode, so a cp would tear a
half-written page and miss the -wal tail). The API produces a single
consistent .db with the WAL already folded in.

Encryption policy (earns the machinery: secrets never plaintext-offsite)
------------------------------------------------------------------------
The secrets archive is built in memory and encrypted straight to disk
with `age`, so plaintext secrets never touch the filesystem. age is
recipient-based: configure one or more X25519 PUBLIC keys and the
always-on host can encrypt but CANNOT decrypt its own backups — the
private identity only ever exists at restore time, off-host. Configure
via --age-recipient / --age-recipients-file or the env vars
MOBIUS_BACKUP_AGE_RECIPIENT / MOBIUS_BACKUP_AGE_RECIPIENTS_FILE.

With no recipient configured the run is "plaintext-local": data.tar.gz
is written unencrypted (fine for a same-host copy) and the SECRETS
archive is SKIPPED by default — a backup missing its secrets is
recoverable-with-effort, a plaintext secrets archive that leaks is a
key compromise. Pass --plaintext-secrets to override for a throwaway
drill only; the manifest records the choice loudly.

Target
------
--target-dir (or MOBIUS_BACKUP_DIR) is where artifacts land, default
/data/backups (already gitignored, so a local run never pollutes the
safety-net repo). For real durability point it at a SECOND filesystem:
a bind-mounted host dir or a separate docker volume mounted at
/data/backups-external (also gitignored, see entrypoint.sh). A backup
on the same volume it protects is NOT disaster recovery — the card
tracks the offsite decision the owner still has to make.

Rotation
--------
Keeps --keep-daily (default 7) most-recent backups plus --keep-weekly
(default 4) older weekly backups; the rest prune. Rotation only ever
deletes directories it can positively parse as mobius backups.

Cron install (opt-in, NOT shipped armed)
----------------------------------------
The platform ships this script but registers nothing on prod. To opt in
on an instance, add a crontab entry for the mobius user (never root —
root-owned files under /data break later mobius operations), e.g. a
daily 04:17 run whose target is a mounted second volume:

  ( crontab -u mobius -l 2>/dev/null | grep -vF backup-data.py; \
    echo '17 4 * * * MOBIUS_BACKUP_DIR=/data/backups-external \
      MOBIUS_BACKUP_AGE_RECIPIENTS_FILE=/data/backups-external/recipients.txt \
      python3 /app/scripts/backup-data.py \
        >> /data/cron-logs/backup-data.log 2>&1' ) | crontab -u mobius -

Rehearsed restore drill
-----------------------
This backup path was proven end to end against a throwaway,
per-slug-isolated container: seed owner + app data, back up (both the
age-encrypted and the plaintext-refusal paths), `docker compose down
-v` to destroy the volume, bring up a fresh container, restore, and
confirm the owner logs in and the data survives. The full drill
procedure lives in restore-data.py's header and the feature card.

Exit codes: 0 ok, 2 usage/config error, 3 backup failed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backup_lib as lib  # noqa: E402

# Trees under /data that are NEVER backed up: transient runtime state,
# content rebuilt from the image on boot, or this script's own output.
# Matched against the top-level entry name relative to data_dir.
EXCLUDE_NAMES = {
  "backups",                 # our own default output (recursion guard)
  "backups-external",        # our own second-volume output
  "agent-browser-profiles",  # per-chat Chrome caches, regenerated
  "logs",                    # rotating logs, not user data
  "cron-logs",               # scheduled-task output
  "generated",               # transient scratch
  "run",                     # per-boot runtime markers
  "platform",                # own git repo, re-seeded from the image
  "shell",                   # retired legacy residue
  "lost+found",
  ".recover-pending",        # transient recovery marker
  "recovery_chat.jsonl",     # transient recovery-chat scratch
}
# Transient marker files at the /data root — runtime signals, not data.
# Deliberately NOT ".recover" (that would also match the SECRET files
# .recovery-secret / .recovery-owner.json); the transient recovery names
# are listed explicitly in EXCLUDE_NAMES instead.
EXCLUDE_PREFIXES = (".platform", ".boot", ".last-successful", ".pm-commit")
# Prefixes for platform bootstrap scratch/quarantine dirs (platform.*).
EXCLUDE_STARTSWITH = ("platform.",)

# Top-level entries routed into the ENCRYPTED secrets archive. Everything
# else non-excluded goes into data.tar.gz.
SECRET_NAMES = {
  "cli-auth", "app-secrets", "push",
  "service-token.txt", ".secret-key",
  ".recovery-secret", ".recovery-owner.json",
}

# The live DB dir is handled specially: we inject a consistent snapshot
# rather than copy the hot files, so the raw dir is skipped in the walk.
DB_DIRNAME = "db"


def log(msg):
  print(f"[backup] {msg}", flush=True)


def die(msg, code=3):
  print(f"[backup] ERROR: {msg}", file=sys.stderr, flush=True)
  sys.exit(code)


def is_excluded(name, target_top):
  """True when a top-level /data entry must not be backed up."""
  if name in EXCLUDE_NAMES:
    return True
  if target_top is not None and name == target_top:
    return True  # the backup target itself, when it lives inside /data
  if name.startswith(EXCLUDE_PREFIXES):
    return True
  if name.startswith(EXCLUDE_STARTSWITH):
    return True
  return False


def snapshot_dbs(db_dir, dest_dir):
  """Writes a consistent snapshot of every *.db in db_dir into dest_dir
  using the sqlite3 online backup API.

  Returns per-db facts for the manifest. Raw db-wal/db-shm are
  intentionally NOT copied: the backup API folds the WAL into the single
  snapshot file, so a restore needs only the one .db.
  """
  import sqlite3
  os.makedirs(dest_dir, exist_ok=True)
  facts = []
  if not os.path.isdir(db_dir):
    return facts
  for name in sorted(os.listdir(db_dir)):
    if not name.endswith(".db"):
      continue
    src = os.path.join(db_dir, name)
    if not os.path.isfile(src):
      continue
    dst = os.path.join(dest_dir, name)
    # Read-only URI so the snapshot cannot mutate the live DB, and a
    # generous busy timeout so a concurrent writer just makes us wait
    # rather than raising "database is locked".
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
      source.execute("PRAGMA busy_timeout=30000")
      target = sqlite3.connect(dst)
      try:
        source.backup(target)
      finally:
        target.close()
    finally:
      source.close()
    facts.append({
      "name": name,
      "source_bytes": os.path.getsize(src),
      "snapshot_bytes": os.path.getsize(dst),
    })
    log(f"snapshotted db/{name} "
        f"({os.path.getsize(dst)} bytes, consistent)")
  return facts


def add_tree(tar, src_path, arcname, newest):
  """Adds a file or directory tree to an open tar, tracking the newest
  mtime seen (the freshness signal restore compares against)."""
  newest[0] = max(newest[0], os.path.getmtime(src_path))
  if os.path.isdir(src_path):
    for root, _dirs, files in os.walk(src_path):
      for f in files:
        full = os.path.join(root, f)
        try:
          newest[0] = max(newest[0], os.path.getmtime(full))
        except OSError:
          pass
  tar.add(src_path, arcname=arcname, recursive=True)


def run_age_encrypt(plaintext, out_path, recipients, recipients_files):
  """Encrypts bytes to out_path with `age`, reading plaintext from stdin
  so it never lands on disk. Raises on any failure — a silent plaintext
  fallback for secrets would be a footgun, not a convenience."""
  # main() preflights age's presence before any partial dir exists; this
  # raises (not sys.exit) so a mid-run failure unwinds through main's
  # except handler and the half-built partial is cleaned up.
  cmd = ["age", "--output", out_path]
  for r in recipients:
    cmd += ["-r", r]
  for rf in recipients_files:
    cmd += ["-R", rf]
  proc = subprocess.run(cmd, input=plaintext, capture_output=True)
  if proc.returncode != 0:
    raise RuntimeError(
      f"age encryption failed: {proc.stderr.decode(errors='replace')}")


def all_readable(path):
  """True when path (and, for a dir, every file under it) is readable by
  the current user. Backups run as mobius; a root-owned 600 secret would
  otherwise fail deep in the run, after the expensive DB snapshot."""
  if os.path.isfile(path):
    return os.access(path, os.R_OK)
  if os.path.isdir(path):
    if not os.access(path, os.R_OK | os.X_OK):
      return False
    for dp, _dirs, files in os.walk(path):
      for f in files:
        if not os.access(os.path.join(dp, f), os.R_OK):
          return False
    return True
  return True


def resolve_recipients(args):
  """Merges recipient config from flags + env. Returns (keys, files)."""
  keys = list(args.age_recipient or [])
  files = list(args.age_recipients_file or [])
  env_key = os.environ.get("MOBIUS_BACKUP_AGE_RECIPIENT", "").strip()
  if env_key:
    keys += env_key.replace(",", " ").split()
  env_file = os.environ.get("MOBIUS_BACKUP_AGE_RECIPIENTS_FILE", "").strip()
  if env_file:
    files.append(env_file)
  for f in files:
    if not os.path.isfile(f):
      die(f"age recipients file not found: {f}", code=2)
  return keys, files


def read_build_sha(data_dir):
  """Best-effort build sha for provenance; never fatal."""
  for path in (os.path.join(data_dir, "platform", ".baked-sha"),
               "/app/build-info.json"):
    try:
      if path.endswith(".json"):
        return str(json.loads(open(path).read()).get("sha") or "unknown")
      return open(path).read().strip() or "unknown"
    except Exception:
      continue
  return os.environ.get("BUILD_SHA", "unknown")


def _artifact(backup_dir, name):
  path = os.path.join(backup_dir, name)
  return {
    "name": name,
    "bytes": os.path.getsize(path),
    "sha256": lib.sha256_file(path),
  }


def rotate(target_dir, keep_daily, keep_weekly):
  names = [n for n in os.listdir(target_dir)
           if os.path.isdir(os.path.join(target_dir, n))]
  keep, prune = lib.select_backups_to_prune(names, keep_daily, keep_weekly)
  for n in prune:
    shutil.rmtree(os.path.join(target_dir, n), ignore_errors=True)
    log(f"rotated out old backup: {n}")
  log(f"retention: kept {len(keep)}, pruned {len(prune)}")


def main():
  ap = argparse.ArgumentParser(
    description="Back up owner data outside the /data git safety net.")
  ap.add_argument("--data-dir",
                  default=os.environ.get("DATA_DIR", "/data"))
  ap.add_argument("--target-dir",
                  default=os.environ.get("MOBIUS_BACKUP_DIR",
                                         "/data/backups"))
  ap.add_argument("--keep-daily", type=int,
                  default=int(os.environ.get("MOBIUS_BACKUP_KEEP_DAILY",
                                             "7")))
  ap.add_argument("--keep-weekly", type=int,
                  default=int(os.environ.get("MOBIUS_BACKUP_KEEP_WEEKLY",
                                             "4")))
  ap.add_argument("--age-recipient", action="append",
                  help="age X25519 public key (repeatable)")
  ap.add_argument("--age-recipients-file", action="append",
                  help="file of age recipients, one per line (repeatable)")
  ap.add_argument("--plaintext-secrets", action="store_true",
                  help="THROWAWAY ONLY: write secrets unencrypted when no "
                       "age recipient is configured")
  ap.add_argument("--dry-run", action="store_true",
                  help="report what would happen without writing a backup")
  args = ap.parse_args()

  data_dir = os.path.abspath(args.data_dir)
  target_dir = os.path.abspath(args.target_dir)
  if not os.path.isdir(data_dir):
    die(f"data dir does not exist: {data_dir}", code=2)

  recipients, recipients_files = resolve_recipients(args)
  encrypt = bool(recipients or recipients_files)
  if encrypt and args.plaintext_secrets:
    die("--plaintext-secrets is meaningless with a recipient configured; "
        "pick one", code=2)
  # Preflight: fail fast (before writing anything) if encryption is asked
  # for but age is missing, rather than silently degrading secrets to
  # plaintext or leaving a half-built backup behind.
  if encrypt and shutil.which("age") is None:
    die("age not found on PATH but an age recipient was configured; "
        "install it (apt-get install -y age) or run without a recipient "
        "for a plaintext-local backup", code=2)
  # Preflight secret readability only when secrets will actually be read
  # (skipped-secrets mode never touches them). Fail early and clearly
  # rather than after the snapshot, which is what the rehearsed drill hit.
  if encrypt or args.plaintext_secrets:
    unreadable = [n for n in sorted(SECRET_NAMES)
                  if os.path.exists(os.path.join(data_dir, n))
                  and not all_readable(os.path.join(data_dir, n))]
    if unreadable:
      die(f"secret paths not readable as this user: {unreadable}; run the "
          "backup as the owner of /data (mobius). On a test container: "
          "docker exec -u root <c> chown -R mobius:mobius /data", code=2)

  # If the target lives inside /data, its top-level component must be
  # excluded from the walk so we never back up our own backups.
  target_top = None
  if os.path.commonpath([data_dir, target_dir]) == data_dir \
      and target_dir != data_dir:
    target_top = os.path.relpath(target_dir, data_dir).split(os.sep)[0]

  os.makedirs(target_dir, exist_ok=True)

  # A flock keeps two runs (cron overlap, manual + cron) from racing on
  # the same target. Non-blocking: a second run exits rather than queue.
  import fcntl
  lock_fd = open(os.path.join(target_dir, ".backup.lock"), "w")
  try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except OSError:
    die("another backup run holds the lock; exiting", code=2)

  created = datetime.now(timezone.utc)
  stamp = lib.format_ts(created)
  final_dir = os.path.join(target_dir, f"{lib.BACKUP_PREFIX}{stamp}")
  partial_dir = final_dir + lib.PARTIAL_SUFFIX

  # Clear crashed partials from prior runs so a retry is clean.
  for entry in os.listdir(target_dir):
    if entry.endswith(lib.PARTIAL_SUFFIX):
      shutil.rmtree(os.path.join(target_dir, entry), ignore_errors=True)

  if args.dry_run:
    log(f"dry-run: would write {final_dir}")
    log(f"dry-run: encrypt secrets={encrypt} "
        f"plaintext_secrets={args.plaintext_secrets}")
    return

  os.makedirs(partial_dir)
  newest = [0.0]  # boxed so add_tree can mutate it in place
  notes = []
  artifacts = []
  manifest = None

  try:
    # Consistent DB snapshot into a temp dir, injected as db/ in the data
    # archive. Its file mtime is "now" and must NOT skew the freshness
    # signal, so it is added to the tar directly, bypassing add_tree.
    snap_dir = tempfile.mkdtemp(dir=partial_dir, prefix=".dbsnap.")
    db_facts = snapshot_dbs(os.path.join(data_dir, DB_DIRNAME), snap_dir)

    data_path = os.path.join(partial_dir, "data.tar.gz")
    secret_members = []
    with tarfile.open(data_path, "w:gz") as tar:
      if db_facts:
        tar.add(snap_dir, arcname=DB_DIRNAME, recursive=True)
      for name in sorted(os.listdir(data_dir)):
        if name == DB_DIRNAME:
          continue
        # Secret routing is checked BEFORE exclusion so a secret can
        # never be silently dropped by an exclude pattern it happens to
        # match — a backup missing its keys is a false sense of safety.
        if name in SECRET_NAMES:
          secret_members.append(name)
          continue
        if is_excluded(name, target_top):
          continue
        add_tree(tar, os.path.join(data_dir, name), name, newest)
    shutil.rmtree(snap_dir, ignore_errors=True)
    artifacts.append(_artifact(partial_dir, "data.tar.gz"))
    log(f"wrote data.tar.gz ({os.path.getsize(data_path)} bytes)")

    # Secrets are read ONLY when they will actually be stored: skip mode
    # never touches them (so an unreadable/irrelevant secret can't fail a
    # skip-mode run), and the in-memory tar means plaintext never hits
    # disk before encryption.
    secrets_state = "skipped"
    if secret_members and (encrypt or args.plaintext_secrets):
      buf = io.BytesIO()
      with tarfile.open(fileobj=buf, mode="w:gz") as star:
        for name in secret_members:
          src = os.path.join(data_dir, name)
          newest[0] = max(newest[0], os.path.getmtime(src))
          star.add(src, arcname=name, recursive=True)
      plaintext = buf.getvalue()
      if encrypt:
        out = os.path.join(partial_dir, "secrets.tar.gz.age")
        run_age_encrypt(plaintext, out, recipients, recipients_files)
        artifacts.append(_artifact(partial_dir, "secrets.tar.gz.age"))
        secrets_state = "encrypted"
        log(f"wrote secrets.tar.gz.age (age-encrypted, "
            f"{os.path.getsize(out)} bytes)")
      else:  # args.plaintext_secrets
        out = os.path.join(partial_dir, "secrets.tar.gz")
        with open(out, "wb") as f:
          f.write(plaintext)
        os.chmod(out, 0o600)
        artifacts.append(_artifact(partial_dir, "secrets.tar.gz"))
        secrets_state = "plaintext"
        notes.append("SECRETS WRITTEN PLAINTEXT — throwaway/local only; "
                     "never move this backup offsite as-is.")
        log("WARNING: wrote secrets.tar.gz UNENCRYPTED "
            "(--plaintext-secrets)")
    elif secret_members:
      notes.append("Secrets NOT backed up: no age recipient configured "
                   "and --plaintext-secrets not set. Configure a "
                   "recipient for a complete, safe DR artifact.")
      log("WARNING: secrets SKIPPED (no age recipient; pass one or "
          "--plaintext-secrets)")

    manifest = lib.build_manifest(
      created_at=created,
      data_dir=data_dir,
      build_sha=read_build_sha(data_dir),
      source={
        "db_files": db_facts,
        "newest_mtime_unix": int(newest[0]),
        "secret_members": secret_members,
      },
      retention={
        "keep_daily": args.keep_daily,
        "keep_weekly": args.keep_weekly,
      },
      encryption={
        "scheme": "age" if encrypt else "none",
        "secrets": secrets_state,
      },
      artifacts=artifacts,
      notes=notes,
    )
    with open(os.path.join(partial_dir, "manifest.json"), "w") as f:
      json.dump(manifest, f, indent=2, sort_keys=True)
      f.write("\n")

    # Atomic publish: rename the fully-built partial into its final name.
    os.replace(partial_dir, final_dir)
    log(f"backup complete: {final_dir}")
  except Exception as exc:  # noqa: BLE001 — clean up any partial artifact
    shutil.rmtree(partial_dir, ignore_errors=True)
    die(f"backup failed, partial removed: {exc}")

  # Rotation runs only after a successful publish, so a failed run can
  # never prune a good backup to make room for a bad one.
  rotate(target_dir, args.keep_daily, args.keep_weekly)

  total = sum(a["bytes"] for a in artifacts)
  log(f"artifacts={len(artifacts)} total_bytes={total} "
      f"secrets={manifest['encryption']['secrets']}")


if __name__ == "__main__":
  main()
