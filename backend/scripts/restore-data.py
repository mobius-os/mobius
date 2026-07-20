#!/usr/bin/env python3
"""Restore an instance from a backup-data.py artifact — the inverse of
backup-data.py.

This overwrites owner data on /data, so it is deliberately hard to fire
by accident, refuses until it has proven the backup intact, and is
transactional: it either fully restores or leaves the original tree
exactly as it found it — never a half-restored mix.

Order of operations (each gate blocks the next)
-----------------------------------------------
 1. Every artifact named in the manifest is hash- and size-verified
    BEFORE anything on /data is touched. A single mismatch aborts.
 2. Server must be stopped. --server-stopped is a REQUIRED acknowledgement
    (the SQLite pool must not keep writing through old fds), and the
    backend health URL is PROBED — if it answers, the restore refuses
    regardless of the flag. The probe repeats immediately before the swap
    to catch a server that came back.
 3. Overwrite guard: any non-empty target requires --force. The old
    mtime comparison is unreliable (integer-second mtimes, clock skew,
    restore-then-rebackup), so it is now only an advisory line in the
    refusal message — the gate is "the target already holds data".
 4. --i-understand-this-overwrites is REQUIRED. A dry-run or an un-acked
    call stops here having mutated nothing.
 5. Encrypted secrets need --age-identity-file (fails fast).
 6. Capacity is preflighted (statvfs) before extraction.

Extraction is staged (.restore-staging.*) then each top-level entry is
swapped in with an atomic rename via backup_lib.swap_entries_transactional:
displaced originals are RENAMED into .restore-rollback.* (never deleted),
and ANY failure rolls every completed swap back before exiting. Same
filesystem, so each rename is atomic and yields a new inode — a process
holding an old DB fd cannot corrupt the restored file. Run with the app
STOPPED and start it after; the restored DB is opened fresh.

Recovery floor
--------------
recoveryd can drive this: mobius user, python3 + tar + (for encrypted
secrets) age + the identity file, writes only under /data, no HTTP route.

The rehearsed restore drill (proven end to end, per-slug throwaway
container)
----------------------------------------------------------------------
Because the restore refuses a running server, the drill drives it from a
MAINTENANCE container that mounts the volume WITHOUT running uvicorn — the
realistic DR shape ("bring the app down, restore, bring it back").

  1. Real container up: owner admin/admin, seed apps/<slug>/data + a
     cli-auth secret, then `docker stop` (server down).
  2. Maintenance container (mounts the volume, no server): cold backup
     with --age-recipients-file => encrypted secrets.tar.gz.age +
     data.tar.gz + manifest. Also confirm no-recipient => secrets
     SKIPPED, and that a backup INSIDE the running server REFUSES (cold
     gate). Copy the backup OFF the volume (docker cp = "offsite"), keep
     the age identity off-box, then destroy the volume.
  3. Fresh volume + real container (writes a fresh DB), confirm admin
     login 401, then `docker stop`.
  4. Maintenance container restore: WITHOUT --force => refused
     (non-empty target); WITH --force --server-stopped + identity =>
     restores transactionally. Exercise the rollback once via an injected
     swap failure (a bind-mount over one target entry -> EBUSY) and
     confirm the earlier-swapped entries roll back.
  5. `docker start`; confirm admin/admin login 200 (DB + .secret-key
     survived) and the seeded app data + secret survived.

Exit codes: 0 ok, 2 usage/config error, 3 verification/capacity failed
(or a full rollback — /data unchanged), 4 refused (a guard tripped: needs
--force / --server-stopped / the ack), 5 restore failed AND rollback was
INCOMPLETE — unrecovered originals preserved in .restore-rollback.* for
hand recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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

# Trees whose presence means "the target already holds data" (the
# overwrite guard) and whose newest mtime feeds the advisory message.
DATA_TREES = ("apps", "chats", "shared")
# Rough gz -> uncompressed expansion for the capacity preflight.
EXPAND_FACTOR = 4
DISK_HEADROOM = 64 * 1024 * 1024


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


def target_has_data(data_dir, ignore=()):
  """True when /data holds ANY entry other than the backup store itself.

  No enumerated tree list to go stale: any file or directory present is
  treated as owner data, so a future data tree is covered automatically.
  ``ignore`` names the top-level entries that are NOT owner data (the
  backup store the restore is reading from, our transient dirs) so
  restoring from a store under /data onto an otherwise-empty volume does
  not falsely trip the guard.
  """
  ignore = set(ignore)
  for name in os.listdir(data_dir):
    if name in ignore:
      continue
    return True
  return False


def live_newest_mtime(data_dir, ignore=()):
  """Newest mtime across every live top-level entry (minus ``ignore``),
  for the advisory freshness line only (NOT a gate)."""
  ignore = set(ignore)
  newest = 0.0
  for name in os.listdir(data_dir):
    if name in ignore:
      continue
    p = os.path.join(data_dir, name)
    try:
      newest = max(newest, os.path.getmtime(p))
    except OSError:
      pass
    if os.path.isdir(p):
      for dp, _dirs, files in os.walk(p):
        for f in files:
          try:
            newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
          except OSError:
            pass
  return newest


def run_age_decrypt(in_path, out_path, identity_files):
  """Decrypts an age file to out_path using the given identity files."""
  import subprocess
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
  traversal and absolute/escaping links (Python 3.12). The archive
  stores paths relative to /data, so the tree lands directly under
  dest.

  A member the filter rejects means the archive is not one our backup
  wrote — hash-valid or not, it is untrustworthy. That is a
  verification failure (exit 3), reported cleanly rather than crashing:
  extraction happens in staging, so /data is untouched either way.
  """
  try:
    with tarfile.open(tar_path, "r:*") as tar:
      tar.extractall(dest, filter="data")
  except (tarfile.FilterError, tarfile.TarError) as exc:
    die(f"archive rejected during extraction ({exc}); this backup is "
        f"not trustworthy", code=3)


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


def refuse_if_server_up(health_url, phase):
  """Refuses when a server answers at health_url — restoring under a live
  SQLite pool risks lost writes through stale fds."""
  if lib.server_responding(health_url):
    die(f"backend is responding at {health_url} ({phase}); stop the app "
        "before restoring (and pass --server-stopped).", code=4)


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
  ap.add_argument("--health-url",
                  default=os.environ.get("MOBIUS_BACKUP_HEALTH_URL", ""),
                  help="backend health URL (default "
                       "http://127.0.0.1:$PORT/api/health)")
  ap.add_argument("--age-identity-file", action="append",
                  help="age identity (private key) file to decrypt "
                       "secrets (repeatable)")
  ap.add_argument("--server-stopped", action="store_true",
                  help="required: acknowledge the app is stopped")
  ap.add_argument("--i-understand-this-overwrites", action="store_true",
                  help="required: acknowledge this overwrites /data")
  ap.add_argument("--force", action="store_true",
                  help="required when the target already holds data")
  ap.add_argument("--dry-run", action="store_true",
                  help="verify + report, never touch /data")
  args = ap.parse_args()

  data_dir = os.path.abspath(args.data_dir)
  if not os.path.isdir(data_dir):
    die(f"data dir does not exist: {data_dir}", code=2)
  backup_dir = resolve_backup_dir(args)
  log(f"restoring from {backup_dir}")

  health_url = args.health_url or \
      f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/api/health"

  manifest_path = os.path.join(backup_dir, "manifest.json")
  if not os.path.isfile(manifest_path):
    die(f"no manifest.json in {backup_dir}", code=2)
  manifest = json.load(open(manifest_path))

  # GATE 1: verify every artifact before touching anything.
  problems = lib.verify_manifest_hashes(manifest, backup_dir)
  if problems:
    for p in problems:
      print(f"[restore]   - {p}", file=sys.stderr)
    die(f"backup failed verification ({len(problems)} problem(s)); "
        "refusing to restore", code=3)
  log(f"verified {len(manifest.get('artifacts', []))} artifact(s) "
      "(sha256 + size)")

  secrets_state = manifest.get("encryption", {}).get("secrets", "skipped")
  art_names = [a["name"] for a in manifest.get("artifacts", [])]
  created_unix = manifest.get("created_unix", 0)
  # The overwrite guard ignores the backup store itself (when it lives
  # under /data) and this run's own transient dirs, so "any other entry
  # counts as data" doesn't misfire on the backups directory.
  ignore = {".backup.lock"}
  store = os.path.realpath(os.path.dirname(backup_dir))
  rdata = os.path.realpath(data_dir)
  if os.path.commonpath([rdata, store]) == rdata and store != rdata:
    ignore.add(os.path.relpath(store, rdata).split(os.sep)[0])
  nonempty = target_has_data(data_dir, ignore=ignore)
  live_newest = int(live_newest_mtime(data_dir, ignore=ignore))

  # GATE 4 (plan / ack): everything above is read-only, so a dry-run or
  # an un-acked call stops here having mutated nothing.
  if args.dry_run or not args.i_understand_this_overwrites:
    log("PLAN (nothing written):")
    log(f"  data_dir      = {data_dir}")
    log(f"  consistency   = {manifest.get('consistency', 'unknown')}")
    log(f"  artifacts     = {art_names}")
    log(f"  secrets       = {secrets_state}")
    log(f"  target_has_data = {nonempty}")
    log(f"  live_newest   = {live_newest}  backup_created = {created_unix}")
    if not args.i_understand_this_overwrites:
      die("refusing to overwrite without "
          "--i-understand-this-overwrites", code=4)
    return

  # GATE 2: server must be stopped (explicit ack + live probe).
  if not args.server_stopped:
    die("refusing without --server-stopped; stop the app first (a live "
        "SQLite pool would lose writes through stale fds)", code=4)
  refuse_if_server_up(health_url, "before restore")

  # GATE 3: overwrite guard — any non-empty target needs --force.
  if nonempty and not args.force:
    hint = ""
    if lib.target_is_newer(live_newest, created_unix):
      hint = (" (advisory: the live data's newest mtime is LATER than this "
              "backup's capture time — it may hold newer work)")
    die(f"target /data already holds data; restoring OVERWRITES it. Re-run "
        f"with --force to proceed.{hint}", code=4)

  # GATE 5: encrypted secrets need a key — fail fast before extraction.
  if secrets_state == "encrypted" and not (args.age_identity_file or []):
    die("secrets are age-encrypted; pass --age-identity-file <key>", code=2)

  # GATE 6: capacity preflight. Staging holds the decompressed archives;
  # the transactional swap and rollback are renames (no extra space).
  artifact_bytes = sum(a.get("bytes", 0)
                       for a in manifest.get("artifacts", []))
  need = artifact_bytes * EXPAND_FACTOR + DISK_HEADROOM
  s = os.statvfs(data_dir)
  free = s.f_bavail * s.f_frsize
  if free < need:
    die(f"insufficient space to stage restore: need ~{need}, have {free}",
        code=3)

  ts = lib.format_ts(datetime.now(timezone.utc))
  staging = tempfile.mkdtemp(dir=data_dir, prefix=".restore-staging.")
  rollback_dir = os.path.join(data_dir, f".restore-rollback.{ts}")
  preserve_rollback = False  # set only when rollback itself failed
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

    # Re-probe just before the swap: a server that came back between the
    # early check and now must not have the DB swapped underneath it.
    refuse_if_server_up(health_url, "just before swap")

    try:
      moved = lib.swap_entries_transactional(staging, data_dir, rollback_dir)
    except lib.RollbackError as rbe:
      # The forward swap failed AND rollback could not restore some
      # originals — they are STILL in rollback_dir. Preserve it, name what
      # is where, and exit with a distinct code. Never delete unrecovered
      # originals.
      preserve_rollback = True
      print(f"[restore]   unrecovered originals (in {rbe.rollback_dir}): "
            f"{rbe.unrecovered}", file=sys.stderr)
      print(f"[restore]   successfully rolled back: {rbe.restored}",
            file=sys.stderr)
      die(f"restore failed AND rollback was INCOMPLETE; "
          f"{len(rbe.unrecovered)} original(s) preserved in "
          f"{rbe.rollback_dir} — recover by hand, do NOT delete it "
          f"(cause: {rbe.original_error!r})", code=5)
    except Exception as exc:  # noqa: BLE001
      # swap already rolled every completed swap back before re-raising,
      # so /data is the exact original tree.
      die(f"restore failed mid-swap and was fully rolled back; /data "
          f"unchanged: {exc}", code=3)
    log(f"restored top-level entries: {moved}")
    # Success: the displaced originals in rollback_dir are no longer
    # needed.
    shutil.rmtree(rollback_dir, ignore_errors=True)
  finally:
    shutil.rmtree(staging, ignore_errors=True)
    # Delete rollback_dir ONLY when it does not hold unrecovered originals
    # — the one case (RollbackError) where it must survive for hand
    # recovery is the whole point of preserve_rollback.
    if not preserve_rollback:
      shutil.rmtree(rollback_dir, ignore_errors=True)

  harden_secret_perms(data_dir)
  log("hardened secret perms (600/700)")
  log("restore complete. Start uvicorn / the container now so it opens "
      "the restored DB fresh.")


if __name__ == "__main__":
  main()
