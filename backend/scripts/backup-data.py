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

Consistency — two honest modes
------------------------------
The DB is snapshotted with the sqlite3 online backup API (never a raw
copy — ultimate.db is WAL-mode; a cp would tear a half-written page). But
the DB snapshot and the surrounding trees (apps/, chats/, shared/) are
captured at slightly different instants, so a backup taken while the app
writes could pair DB state from T1 with file state from T2. Rather than
build an app-side quiesce lock (unearned machinery for a single-owner
daily backup), the script is explicit:

  default (COLD): refuses if the backend is responding on its health
    URL. The operator stops the app first; nothing writes during the
    run; the artifact is mutually consistent. This is the DR/migration
    path. Manifest records consistency: "cold".
  --online: the cron path. Runs against a live server and records
    consistency: "crash-consistent-per-tree" — the DB snapshot is
    internally consistent, the trees are copied live, and there is NO
    cross-file ordering guarantee. Honest, and fine for a daily rolling
    backup where the DB is the authoritative store.

What it captures (per the live /data layout, not a hardcoded list)
------------------------------------------------------------------
"Include unless excluded" (fail-safe: a future /data dir is captured by
default). Routed into two archives:

  data.tar.gz     the consistent SQLite snapshot + every non-secret tree
                  (apps/, chats/, compiled/, shared/, .git, ...). shared/
                  + .git are git-tracked but the git repo dies WITH the
                  volume, so a true DR artifact carries them too.
  secrets.tar.*   cli-auth/, app-secrets/, push/, service-token.txt,
                  .secret-key, .recovery-secret, .recovery-owner.json.

Encryption policy (secrets never plaintext-offsite)
---------------------------------------------------
The secrets archive is STREAMED straight into `age` (no full plaintext
buffer, no plaintext file on disk). age is recipient-based (X25519
public key): the always-on host can encrypt but CANNOT decrypt its own
backups — the private identity only exists at restore time, off-host.
Configure via --age-recipient / --age-recipients-file or
MOBIUS_BACKUP_AGE_RECIPIENT[S_FILE].

With NO recipient: data.tar.gz is written plaintext-local and secrets
are SKIPPED, not silently written plaintext. --plaintext-secrets is a
THROWAWAY-only override; the file is created atomically at mode 0600
inside a 0700 partial dir.

Target
------
--target-dir / MOBIUS_BACKUP_DIR. A target ON THE SAME VOLUME as /data is
rejected (it dies with the volume it protects) unless --allow-same-volume
is passed for a deliberate local, non-DR copy; the data root itself and
any parent of it are always rejected. A separate volume mounted under
/data (e.g. /data/backups-external, a different filesystem) is accepted.
Capacity is preflighted (statvfs) before the snapshot.

Rotation
--------
--keep-daily (default 7) + --keep-weekly (default 4); total must be >= 1.
The just-published backup and the newest backup that still carries
complete secrets are always pinned, so rotation can never drop the last
usable copy. Artifacts + manifest + parent dir are fsync'd before any
prune. Rotation only ever deletes dirs it can positively parse as
backups.

Cron install (opt-in, NOT shipped armed)
----------------------------------------
The platform ships this script (and bakes `age` into the image) but
registers nothing on prod. To opt in, add a mobius-user crontab entry
(never root — root-owned files under /data break later mobius ops):

  17 4 * * * MOBIUS_BACKUP_DIR=/data/backups-external \
    MOBIUS_BACKUP_AGE_RECIPIENTS_FILE=/data/backups-external/recipients.txt \
    python3 /app/scripts/backup-data.py --online \
      >> /data/cron-logs/backup-data.log 2>&1

The rehearsed restore drill is documented in restore-data.py's header.

Exit codes: 0 ok, 2 usage/config/refusal, 3 backup failed.
"""

from __future__ import annotations

import argparse
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
  ".setup-claim",            # first-boot claim secret / stale runtime state
}
# Transient marker files at the /data root — runtime signals, not data.
# Deliberately NOT ".recover" (that would also match the SECRET files
# .recovery-secret / .recovery-owner.json); those transient names are in
# EXCLUDE_NAMES instead.
EXCLUDE_PREFIXES = (".platform", ".boot", ".last-successful", ".pm-commit")
# Prefixes for platform bootstrap scratch/quarantine dirs (platform.*)
# and this run's own transient restore staging/rollback dirs.
EXCLUDE_STARTSWITH = (
  "platform.", ".restore-staging.", ".restore-rollback.",
  ".setup-claim.",           # crash-left claim temp files
)

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
# Free-space headroom demanded on top of the estimated source size.
DISK_HEADROOM = 64 * 1024 * 1024


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


def _tree_size(path):
  total = 0
  if os.path.isfile(path):
    return os.path.getsize(path)
  for dirpath, _dirs, files in os.walk(path):
    for f in files:
      try:
        total += os.path.getsize(os.path.join(dirpath, f))
      except OSError:
        pass
  return total


def estimate_source_bytes(data_dir, target_top):
  """Approximate uncompressed size of everything this run will archive,
  for the capacity preflight. The DB snapshot is ~ the live db size."""
  total = 0
  for name in sorted(os.listdir(data_dir)):
    if name == DB_DIRNAME:
      db = os.path.join(data_dir, name)
      if os.path.isdir(db):
        for f in os.listdir(db):
          if f.endswith(".db"):
            total += os.path.getsize(os.path.join(db, f))
      continue
    if name in SECRET_NAMES:
      total += _tree_size(os.path.join(data_dir, name))
      continue
    if is_excluded(name, target_top):
      continue
    total += _tree_size(os.path.join(data_dir, name))
  return total


def statvfs_free(path):
  s = os.statvfs(path)
  return s.f_bavail * s.f_frsize


def _nearest_existing(path):
  p = os.path.realpath(path)
  while not os.path.exists(p) and p != os.path.dirname(p):
    p = os.path.dirname(p)
  return p


def validate_target(data_dir, target_dir, allow_same_volume):
  """Rejects a dangerous target and returns its kind for the manifest.

  Uses os.path.realpath on BOTH sides so a symlink cannot alias the
  target back onto the data dir / volume. data_dir itself and any parent
  of it are always rejected (the recursion / whole-volume-archive
  footgun). Any target on the SAME physical filesystem (same st_dev) as
  the data dir — whether under /data by path or not — is rejected unless
  --allow-same-volume, because it shares the data volume's fate. A target
  on a genuinely separate device is a durable DR target and is accepted.
  """
  rdata = os.path.realpath(data_dir)
  rtarget = os.path.realpath(target_dir)
  if rtarget == rdata:
    die("target dir must not be the data dir itself", code=2)
  if os.path.commonpath([rtarget, rdata]) == rtarget:
    die("target dir must not be a parent of the data dir", code=2)
  dev_data = os.stat(rdata).st_dev
  dev_target = os.stat(_nearest_existing(rtarget)).st_dev
  if dev_target == dev_data:
    if not allow_same_volume:
      die("target shares the SAME volume as the data dir (same device); a "
          "backup there dies with the volume it protects. Point "
          "--target-dir at a SEPARATE filesystem (a second docker volume "
          "or a bind mount from another disk). Pass --allow-same-volume "
          "only for a deliberate local, non-DR copy.", code=2)
    return "same-volume"
  return "external"


def snapshot_dbs(db_dir, dest_dir):
  """Writes a consistent snapshot of every *.db in db_dir into dest_dir
  using the sqlite3 online backup API.

  Raw db-wal/db-shm are intentionally NOT copied: the backup API folds
  the WAL into the single snapshot file. Returns per-db facts.
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
    # generous busy timeout so a concurrent writer just makes us wait.
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
    log(f"snapshotted db/{name} ({os.path.getsize(dst)} bytes, consistent)")
  return facts


def add_tree(tar, src_path, arcname, newest):
  """Adds a file or directory tree to an open tar, tracking the newest
  mtime seen (an advisory freshness signal recorded in the manifest)."""
  newest[0] = max(newest[0], os.path.getmtime(src_path))
  if os.path.isdir(src_path):
    for root, _dirs, files in os.walk(src_path):
      for f in files:
        try:
          newest[0] = max(newest[0], os.path.getmtime(os.path.join(root, f)))
        except OSError:
          pass
  tar.add(src_path, arcname=arcname, recursive=True)


def encrypt_secrets_stream(members, data_dir, out_path, recipients,
                           recipients_files):
  """Streams a gz tar of the secret members straight into age's stdin,
  so a full plaintext buffer never exists in memory or on disk. Raises
  on any failure; main() unwinds and removes the partial dir."""
  cmd = ["age", "--output", out_path]
  for r in recipients:
    cmd += ["-r", r]
  for rf in recipients_files:
    cmd += ["-R", rf]
  proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE)
  try:
    # Streaming tar mode (w|gz) — writes forward-only into the pipe.
    with tarfile.open(fileobj=proc.stdin, mode="w|gz") as tar:
      for name in members:
        tar.add(os.path.join(data_dir, name), arcname=name, recursive=True)
  finally:
    proc.stdin.close()  # EOF to age; do NOT communicate() (double-close)
  # Secrets are small (well under a pipe buffer), so age's stderr can't
  # back-pressure us into a deadlock — read it after sending EOF.
  err = proc.stderr.read()
  proc.stderr.close()
  if proc.wait() != 0:
    raise RuntimeError(
      f"age encryption failed: {err.decode(errors='replace')}")


def write_secrets_plaintext(members, data_dir, out_path):
  """Writes a gz tar of the secrets atomically at mode 0600 (O_CREAT|
  O_EXCL, no chmod-after race). THROWAWAY use only."""
  fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  try:
    with os.fdopen(fd, "wb") as f:
      with tarfile.open(fileobj=f, mode="w|gz") as tar:
        for name in members:
          tar.add(os.path.join(data_dir, name), arcname=name,
                  recursive=True)
  except BaseException:
    try:
      os.unlink(out_path)
    except OSError:
      pass
    raise


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


def _newest_complete_secrets(target_dir, names):
  """Newest backup that carries complete secrets AND passes hash
  verification (a usable full DR copy), or None.

  The manifest LABEL alone is not trusted — a secrets-skipped label is
  just as easy to corrupt as the archive, and pinning a copy whose
  archives don't match their recorded hashes would defeat the point. So
  every candidate is verified with the same code restore uses before it
  is treated as the pinned complete-secrets backup.
  """
  dated = sorted(
    ((lib.parse_backup_dirname(n), n) for n in names
     if lib.parse_backup_dirname(n) is not None),
    reverse=True)
  for _dt, n in dated:
    bdir = os.path.join(target_dir, n)
    try:
      m = json.load(open(os.path.join(bdir, "manifest.json")))
    except Exception:
      continue
    if m.get("encryption", {}).get("secrets") not in ("encrypted",
                                                       "plaintext"):
      continue
    if lib.verify_manifest_hashes(m, bdir):  # non-empty == problems
      continue  # corrupt/incomplete — do not treat as the safe copy
    return n
  return None


def rotate(target_dir, keep_daily, keep_weekly, just_published):
  names = [n for n in os.listdir(target_dir)
           if os.path.isdir(os.path.join(target_dir, n))]
  # Pin the just-published backup AND the newest still-complete-secrets
  # backup so rotation can never drop the last usable copy in favour of
  # an older or secrets-skipped one.
  pinned = {just_published}
  complete = _newest_complete_secrets(target_dir, names)
  if complete:
    pinned.add(complete)
  keep, prune = lib.select_backups_to_prune(
    names, keep_daily, keep_weekly, pinned=pinned)
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
  ap.add_argument("--online", action="store_true",
                  help="allow running against a live server "
                       "(crash-consistent-per-tree, the cron path)")
  ap.add_argument("--health-url",
                  default=os.environ.get("MOBIUS_BACKUP_HEALTH_URL", ""),
                  help="backend health URL used to detect a running "
                       "server (default http://127.0.0.1:$PORT/api/health)")
  ap.add_argument("--allow-same-volume", action="store_true",
                  help="permit a target on the same volume as /data "
                       "(local, non-DR copy)")
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

  # Retention sanity: a total of zero would prune everything, including
  # the backup we are about to publish.
  if args.keep_daily < 0 or args.keep_weekly < 0 \
      or (args.keep_daily + args.keep_weekly) < 1:
    die("keep-daily + keep-weekly must be >= 1 (and neither negative)",
        code=2)

  recipients, recipients_files = resolve_recipients(args)
  encrypt = bool(recipients or recipients_files)
  if encrypt and args.plaintext_secrets:
    die("--plaintext-secrets is meaningless with a recipient configured; "
        "pick one", code=2)
  if encrypt and shutil.which("age") is None:
    die("age not found on PATH but an age recipient was configured; "
        "install it (apt-get install -y age) or run without a recipient "
        "for a plaintext-local backup", code=2)

  target_kind = validate_target(data_dir, target_dir, args.allow_same_volume)

  # Consistency mode: default cold (server must be down); --online is the
  # honestly-labelled live cron path.
  health_url = args.health_url or \
      f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/api/health"
  if not args.online:
    if lib.server_responding(health_url):
      die(f"backend is responding at {health_url}; a cold backup needs "
          "it stopped so the DB snapshot and the trees are mutually "
          "consistent. Stop the app, or pass --online for a "
          "crash-consistent-per-tree backup.", code=2)
    consistency = "cold"
  else:
    consistency = "crash-consistent-per-tree"

  # Exclude the target's top-level component from the walk when it lives
  # inside /data, so we never back up our own backups. realpath both
  # sides so a symlinked target can't slip past this by aliasing.
  target_top = None
  rdata = os.path.realpath(data_dir)
  rtarget = os.path.realpath(target_dir)
  if os.path.commonpath([rdata, rtarget]) == rdata and rtarget != rdata:
    target_top = os.path.relpath(rtarget, rdata).split(os.sep)[0]

  # Preflight secret readability only when secrets will actually be read.
  if encrypt or args.plaintext_secrets:
    unreadable = [n for n in sorted(SECRET_NAMES)
                  if os.path.exists(os.path.join(data_dir, n))
                  and not all_readable(os.path.join(data_dir, n))]
    if unreadable:
      die(f"secret paths not readable as this user: {unreadable}; run the "
          "backup as the owner of /data (mobius). On a test container: "
          "docker exec -u root <c> chown -R mobius:mobius /data", code=2)

  os.makedirs(target_dir, exist_ok=True)
  os.chmod(target_dir, 0o700)

  # Capacity preflight before any expensive work.
  need = estimate_source_bytes(data_dir, target_top) + DISK_HEADROOM
  free = statvfs_free(target_dir)
  if free < need:
    die(f"insufficient space on target: need ~{need} bytes, have {free}",
        code=3)

  # A flock keeps two runs (cron overlap, manual + cron) from racing.
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

  for entry in os.listdir(target_dir):
    if entry.endswith(lib.PARTIAL_SUFFIX):
      shutil.rmtree(os.path.join(target_dir, entry), ignore_errors=True)

  if args.dry_run:
    log(f"dry-run: would write {final_dir}")
    log(f"dry-run: consistency={consistency} target_kind={target_kind} "
        f"encrypt={encrypt} plaintext_secrets={args.plaintext_secrets}")
    return

  os.makedirs(partial_dir)
  os.chmod(partial_dir, 0o700)
  newest = [0.0]
  notes = []
  artifacts = []
  manifest = None
  if consistency == "crash-consistent-per-tree":
    notes.append("--online: DB snapshot is internally consistent; other "
                 "trees copied live with NO cross-file ordering "
                 "guarantee.")
  if target_kind == "same-volume":
    notes.append("SAME-VOLUME target (--allow-same-volume): dies with the "
                 "volume it protects; NOT disaster recovery.")

  try:
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
        # never be silently dropped by an exclude pattern it matches.
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
    # never touches them, and both stored paths STREAM (no full plaintext
    # buffer / no plaintext-then-chmod race).
    secrets_state = "skipped"
    if secret_members and (encrypt or args.plaintext_secrets):
      for name in secret_members:
        newest[0] = max(newest[0],
                        os.path.getmtime(os.path.join(data_dir, name)))
      if encrypt:
        out = os.path.join(partial_dir, "secrets.tar.gz.age")
        encrypt_secrets_stream(secret_members, data_dir, out,
                               recipients, recipients_files)
        artifacts.append(_artifact(partial_dir, "secrets.tar.gz.age"))
        secrets_state = "encrypted"
        log(f"wrote secrets.tar.gz.age (age-encrypted, "
            f"{os.path.getsize(out)} bytes)")
      else:  # args.plaintext_secrets
        out = os.path.join(partial_dir, "secrets.tar.gz")
        write_secrets_plaintext(secret_members, data_dir, out)
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
      consistency=consistency,
      source={
        "db_files": db_facts,
        "newest_mtime_unix": int(newest[0]),
        "secret_members": secret_members,
        "target_kind": target_kind,
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

    os.replace(partial_dir, final_dir)
    log(f"backup complete: {final_dir}")
  except Exception as exc:  # noqa: BLE001 — clean up any partial artifact
    shutil.rmtree(partial_dir, ignore_errors=True)
    die(f"backup failed, partial removed: {exc}")

  # Durability gate BEFORE any prune: fsync the published artifacts. If a
  # file fsync fails the new backup may not be durable, so an fsync
  # failure ABORTS pruning — we keep every older copy rather than delete
  # one against a possibly-lost new backup.
  durable = True
  try:
    lib.fsync_tree(final_dir)
    lib.fsync_dir(target_dir)
  except OSError as exc:
    durable = False
    log(f"WARNING: fsync failed ({exc}); SKIPPING rotation so no older "
        "backup is pruned against a possibly-undurable new one")

  if durable:
    rotate(target_dir, args.keep_daily, args.keep_weekly,
           os.path.basename(final_dir))
  else:
    log("retention SKIPPED (fsync failure)")

  total = sum(a["bytes"] for a in artifacts)
  log(f"artifacts={len(artifacts)} total_bytes={total} "
      f"consistency={consistency} "
      f"secrets={manifest['encryption']['secrets']}")


if __name__ == "__main__":
  main()
