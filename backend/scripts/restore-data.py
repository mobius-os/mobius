#!/usr/bin/env python3
"""Restore an instance from a backup-data.py artifact — the inverse of
backup-data.py.

This overwrites owner data on /data, so it is deliberately hard to fire
by accident and refuses to run until it has proven the backup is intact.

Order of operations (each gate blocks the next)
-----------------------------------------------
 1. --i-understand-this-overwrites is REQUIRED. Without it the script
    prints what it WOULD do and exits non-zero. There is no default that
    mutates /data.
 2. Every artifact named in the manifest is hash- and size-verified
    BEFORE anything on /data is touched. A single mismatch aborts — a
    corrupt backup must never half-overwrite a live instance.
 3. Staleness guard: if the live DB was written AFTER this backup was
    taken, the target holds newer data than the backup and the restore
    refuses unless --force. This is what stops a stale nightly backup
    from silently clobbering a day's work. A fresh-but-initialised
    instance trips this too (its boot wrote a new DB), which is exactly
    when a DR operator wants --force — the fresh DB is not user data.
 4. Secrets: if the backup's secrets archive is age-encrypted, an
    --age-identity-file is required to decrypt it. Secrets are extracted
    with 600/700 perms restored.

Extraction is staged: archives unpack into /data/.restore-staging.* and
each top-level entry is then swapped into place with an atomic rename
(new inode), so a process still holding the old DB inode cannot corrupt
the restored one. Run with the app STOPPED (or immediately restart it):
uvicorn keeps the pre-restore DB inode open until it reconnects.

Recovery floor
--------------
recoveryd can drive this: it runs as mobius, needs only python3 + tar +
(for encrypted secrets) age + the identity file, and writes solely under
/data. No HTTP route runs it — the operator or recovery agent invokes it
directly.

The rehearsed restore drill (proven end to end, per-slug throwaway
container)
----------------------------------------------------------------------
  1. Fresh isolated container (all three of -p / MOBIUS_CONTAINER /
     MOBIUS_IMAGE per CLAUDE.md), owner admin/admin, CLI creds copied so
     cli-auth holds a real secret.
  2. Seed data: create an app + write apps/<slug>/data, send a chat
     (writes the DB).
  3. age-keygen -> recipients.txt; backup-data.py --age-recipients-file
     recipients.txt  => encrypted secrets.tar.gz.age + data.tar.gz +
     manifest.json. Also run with no recipient to confirm secrets are
     SKIPPED (refused), not silently written plaintext.
  4. Copy the backup dir OFF the volume (docker cp to host = "offsite").
  5. docker compose down -v  => destroy the volume (only the per-slug
     project's volume; siblings untouched).
  6. Fresh container on a clean volume. Copy the backup back in.
  7. restore-data.py <backup> --age-identity-file key.txt
     --i-understand-this-overwrites  => refuses (staleness guard: the
     fresh boot wrote a newer DB). Re-run with --force => restores.
  8. docker restart; confirm owner logs in (admin/admin -> 200 token,
     proving .secret-key + DB users survived) and the seeded app + its
     runtime data are present.

Exit codes: 0 ok, 2 usage/config error, 3 verification failed,
4 refused (guard tripped, needs --force or the ack flag).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backup_lib as lib  # noqa: E402

# Files/dirs whose perms must be tightened after extraction — secrets
# arriving from a tar carry the archiver's mode, not the 600/700 the live
# instance expects.
SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700
SECRET_FILES = ("service-token.txt", ".secret-key", ".recovery-secret",
                ".recovery-owner.json")
SECRET_DIRS = ("cli-auth", "app-secrets", "push")

# Trees whose mtime signals "the owner did something after the backup".
# Kept small and DB-first: the DB is the always-written authoritative
# store, so its mtime is the primary freshness signal.
FRESHNESS_TREES = ("apps", "chats", "shared")


def log(msg):
  print(f"[restore] {msg}", flush=True)


def die(msg, code=3):
  print(f"[restore] ERROR: {msg}", file=sys.stderr, flush=True)
  sys.exit(code)


def resolve_backup_dir(args):
  """Returns the backup dir to restore from, explicit or newest."""
  if args.backup_dir:
    d = os.path.abspath(args.backup_dir)
    if not os.path.isdir(d):
      die(f"backup dir not found: {d}", code=2)
    return d
  if args.latest:
    src = os.path.abspath(args.from_dir or "/data/backups")
    dated = []
    for n in os.listdir(src) if os.path.isdir(src) else []:
      dt = lib.parse_backup_dirname(n)
      if dt is not None and os.path.isdir(os.path.join(src, n)):
        dated.append((dt, n))
    if not dated:
      die(f"no backups found under {src}", code=2)
    dated.sort(reverse=True)
    return os.path.join(src, dated[0][1])
  die("give a backup dir or --latest [--from-dir DIR]", code=2)


def live_newest_mtime(data_dir):
  """Newest mtime across the live DB and the freshness trees, or 0 when
  the instance is empty. This is what the staleness guard compares
  against the backup's capture time."""
  newest = 0.0
  db_dir = os.path.join(data_dir, "db")
  if os.path.isdir(db_dir):
    for n in os.listdir(db_dir):
      if n.endswith((".db", ".db-wal", ".db-shm")):
        try:
          newest = max(newest, os.path.getmtime(os.path.join(db_dir, n)))
        except OSError:
          pass
  for tree in FRESHNESS_TREES:
    root = os.path.join(data_dir, tree)
    if not os.path.isdir(root):
      continue
    for dirpath, _dirs, files in os.walk(root):
      for f in files:
        try:
          newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
        except OSError:
          pass
  return newest


def run_age_decrypt(in_path, out_path, identity_files):
  """Decrypts an age file to out_path using the given identity files."""
  if shutil.which("age") is None:
    die("age not found on PATH but the backup's secrets are encrypted; "
        "install it (apt-get install -y age)", code=2)
  if not identity_files:
    die("secrets are age-encrypted; pass --age-identity-file <key>",
        code=2)
  cmd = ["age", "--decrypt", "--output", out_path]
  for idf in identity_files:
    if not os.path.isfile(idf):
      die(f"age identity file not found: {idf}", code=2)
    cmd += ["-i", idf]
  cmd.append(in_path)
  proc = subprocess.run(cmd, capture_output=True)
  if proc.returncode != 0:
    die(f"age decryption failed: {proc.stderr.decode(errors='replace')}")


def safe_extract(tar_path, dest):
  """Extracts a tar into dest with the 'data' filter, which blocks path
  traversal and absolute paths (Python 3.12). The archive stores paths
  relative to /data, so the tree lands directly under dest."""
  with tarfile.open(tar_path, "r:*") as tar:
    tar.extractall(dest, filter="data")


def harden_secret_perms(data_dir):
  """Restores the 600/700 the live instance expects on secret paths."""
  for name in SECRET_FILES:
    p = os.path.join(data_dir, name)
    if os.path.isfile(p):
      os.chmod(p, SECRET_FILE_MODE)
  for name in SECRET_DIRS:
    root = os.path.join(data_dir, name)
    if not os.path.isdir(root):
      continue
    os.chmod(root, SECRET_DIR_MODE)
    for dirpath, dirs, files in os.walk(root):
      for d in dirs:
        os.chmod(os.path.join(dirpath, d), SECRET_DIR_MODE)
      for f in files:
        os.chmod(os.path.join(dirpath, f), SECRET_FILE_MODE)


def swap_into_place(staging, data_dir):
  """Moves each top-level entry from staging into data_dir with an atomic
  rename, replacing any existing entry. Same filesystem, so the rename is
  atomic and yields a NEW inode — a process still holding the old DB inode
  cannot corrupt the restored file."""
  moved = []
  for name in sorted(os.listdir(staging)):
    src = os.path.join(staging, name)
    dst = os.path.join(data_dir, name)
    if os.path.islink(dst) or os.path.isfile(dst):
      os.unlink(dst)
    elif os.path.isdir(dst):
      shutil.rmtree(dst)
    os.replace(src, dst)
    moved.append(name)
  return moved


def main():
  ap = argparse.ArgumentParser(
    description="Restore /data from a backup-data.py artifact.")
  ap.add_argument("backup_dir", nargs="?",
                  help="path to a mobius-backup-<ts> directory")
  ap.add_argument("--latest", action="store_true",
                  help="restore the newest backup under --from-dir")
  ap.add_argument("--from-dir",
                  default=os.environ.get("MOBIUS_BACKUP_DIR",
                                         "/data/backups"),
                  help="where --latest looks (default /data/backups)")
  ap.add_argument("--data-dir",
                  default=os.environ.get("DATA_DIR", "/data"))
  ap.add_argument("--age-identity-file", action="append",
                  help="age identity (private key) file to decrypt "
                       "secrets (repeatable)")
  ap.add_argument("--i-understand-this-overwrites", action="store_true",
                  help="required: acknowledge this overwrites /data")
  ap.add_argument("--force", action="store_true",
                  help="override the staleness guard (target has newer "
                       "data than the backup)")
  ap.add_argument("--dry-run", action="store_true",
                  help="verify + report, never touch /data")
  args = ap.parse_args()

  data_dir = os.path.abspath(args.data_dir)
  if not os.path.isdir(data_dir):
    die(f"data dir does not exist: {data_dir}", code=2)
  backup_dir = resolve_backup_dir(args)
  log(f"restoring from {backup_dir}")

  manifest_path = os.path.join(backup_dir, "manifest.json")
  if not os.path.isfile(manifest_path):
    die(f"no manifest.json in {backup_dir}", code=2)
  manifest = json.load(open(manifest_path))

  # GATE 2: verify every artifact before touching anything.
  problems = lib.verify_manifest_hashes(manifest, backup_dir)
  if problems:
    for p in problems:
      print(f"[restore]   - {p}", file=sys.stderr)
    die(f"backup failed verification ({len(problems)} problem(s)); "
        "refusing to restore", code=3)
  log(f"verified {len(manifest.get('artifacts', []))} artifact(s) "
      "(sha256 + size)")

  # GATE 3: staleness guard.
  created_unix = manifest.get("created_unix", 0)
  live_newest = int(live_newest_mtime(data_dir))
  if lib.target_is_newer(live_newest, created_unix):
    when_live = datetime.fromtimestamp(live_newest, timezone.utc)
    when_bak = datetime.fromtimestamp(created_unix, timezone.utc)
    msg = (f"target has newer data ({when_live.isoformat()}) than the "
           f"backup ({when_bak.isoformat()})")
    if not args.force:
      die(msg + "; refusing without --force", code=4)
    log(f"WARNING: {msg} — overriding (--force)")

  secrets_state = manifest.get("encryption", {}).get("secrets", "skipped")
  art_names = [a["name"] for a in manifest.get("artifacts", [])]

  # GATE 1: the acknowledgement flag. Everything above is read-only, so a
  # dry-run or an un-acked call stops here having mutated nothing.
  if args.dry_run or not args.i_understand_this_overwrites:
    log("PLAN (nothing written):")
    log(f"  data_dir     = {data_dir}")
    log(f"  artifacts    = {art_names}")
    log(f"  secrets      = {secrets_state}")
    log(f"  live_newest  = {live_newest}  backup_created = {created_unix}")
    if not args.i_understand_this_overwrites:
      die("refusing to overwrite without "
          "--i-understand-this-overwrites", code=4)
    return

  # Fail fast, before extracting anything, if the secrets are encrypted
  # but we were given no key to decrypt them.
  if secrets_state == "encrypted" and not (args.age_identity_file or []):
    die("secrets are age-encrypted; pass --age-identity-file <key>", code=2)

  # Stage: unpack every archive into a scratch dir on the same
  # filesystem, then swap top-level entries into place atomically.
  staging = tempfile.mkdtemp(dir=data_dir, prefix=".restore-staging.")
  try:
    if "data.tar.gz" in art_names:
      safe_extract(os.path.join(backup_dir, "data.tar.gz"), staging)
      log("extracted data.tar.gz")

    if secrets_state == "encrypted":
      enc = os.path.join(backup_dir, "secrets.tar.gz.age")
      dec = os.path.join(staging, ".secrets.tar.gz")
      run_age_decrypt(enc, dec, args.age_identity_file or [])
      safe_extract(dec, staging)
      os.unlink(dec)
      log("decrypted + extracted secrets.tar.gz.age")
    elif secrets_state == "plaintext":
      safe_extract(os.path.join(backup_dir, "secrets.tar.gz"), staging)
      log("extracted secrets.tar.gz (plaintext backup)")
    else:
      log("WARNING: backup has NO secrets (skipped at backup time); "
          ".secret-key + provider auth will NOT be restored — the owner "
          "may need to re-authenticate and JWTs will be re-signed")

    moved = swap_into_place(staging, data_dir)
    log(f"restored top-level entries: {moved}")
  finally:
    shutil.rmtree(staging, ignore_errors=True)

  harden_secret_perms(data_dir)
  log("hardened secret perms (600/700)")
  log("restore complete. Restart uvicorn / the container now so it "
      "reopens the restored DB.")


if __name__ == "__main__":
  main()
