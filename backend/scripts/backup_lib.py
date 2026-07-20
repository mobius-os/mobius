"""Pure + low-level helpers shared by backup-data.py and restore-data.py.

Most of this module is pure (hashing, manifest assembly, retention
selection) so the logic most likely to silently corrupt a restore — a
wrong hash, a rotation that deletes the wrong backup — is unit-testable
without Docker. The one stateful primitive here is the transactional
entry swap used by restore: it lives here so its rollback behaviour can
be driven directly by a test with injected failures, which a subprocess
cannot. The driver scripts own the rest of the side effects (sqlite
snapshot, tar, age, server probing).
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Bump only on an incompatible manifest change. restore-data.py refuses a
# manifest whose version it does not recognise rather than guess at a
# shape it cannot safely parse.
MANIFEST_VERSION = 1
BACKUP_PREFIX = "mobius-backup-"
# Timestamp in backup dir names: UTC, second precision, and lexically
# sortable so "newest-first" is a plain reverse string sort even without
# parsing. The suffix ".partial" marks a run still in progress.
TS_FORMAT = "%Y%m%dT%H%M%SZ"
PARTIAL_SUFFIX = ".partial"


def format_ts(dt):
  """Formats a datetime as the UTC backup-dir timestamp."""
  return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_backup_dirname(name):
  """Returns the UTC datetime encoded in a backup dir name, or None.

  Anything that is not exactly ``mobius-backup-<ts>`` (a stray file, a
  crashed ``.partial`` dir, an unrelated entry) returns None so callers
  skip it rather than mis-sort it into the retention set. This is the
  guard that keeps rotation from ever deleting a non-backup.
  """
  if not name.startswith(BACKUP_PREFIX):
    return None
  if name.endswith(PARTIAL_SUFFIX):
    return None
  stamp = name[len(BACKUP_PREFIX):]
  try:
    dt = datetime.strptime(stamp, TS_FORMAT)
  except ValueError:
    return None
  return dt.replace(tzinfo=timezone.utc)


def sha256_file(path, chunk_size=1024 * 1024):
  """Streams a file through sha256 so a multi-GB archive never loads
  fully into memory. Returns the lowercase hex digest."""
  h = hashlib.sha256()
  with open(path, "rb") as f:
    for block in iter(lambda: f.read(chunk_size), b""):
      h.update(block)
  return h.hexdigest()


def select_backups_to_prune(names, keep_daily, keep_weekly, pinned=()):
  """Splits backup dir names into (keep, prune) under an N-daily +
  M-weekly policy, with an explicit pin set that is never pruned.

  A backup is kept when it is pinned, OR it is one of the ``keep_daily``
  most recent, OR it is the newest backup in one of the next
  ``keep_weekly`` distinct ISO weeks below the daily window. ``pinned``
  lets the driver protect the just-published backup and the newest
  backup that still carries complete secrets, so rotation can never
  drop the last usable copy in favour of an older or secrets-less one.

  Invariant: an unparseable name is never returned in ``prune`` — the
  caller must not delete what this function could not positively
  identify as a backup. Both lists come back newest-first.
  """
  pinned = set(pinned)
  dated = []
  for n in names:
    dt = parse_backup_dirname(n)
    if dt is not None:
      dated.append((dt, n))
  # The timestamp is embedded in the name, so this is a total order.
  dated.sort(key=lambda x: x[0], reverse=True)

  keep = set(n for _dt, n in dated if n in pinned)
  # Daily window: the newest keep_daily backups, unconditionally.
  daily = max(keep_daily, 0)
  for _dt, n in dated[:daily]:
    keep.add(n)

  # Weekly window: below the daily window, keep the newest backup in each
  # of the next keep_weekly distinct ISO (year, week) buckets. Older
  # backups sharing an already-kept week are pruned — one per week.
  weeks_kept = []
  weekly = max(keep_weekly, 0)
  for dt, n in dated[daily:]:
    if len(weeks_kept) >= weekly:
      break
    iso = dt.isocalendar()
    key = (iso.year, iso.week)
    if key in weeks_kept:
      continue
    weeks_kept.append(key)
    keep.add(n)

  keep_names = [n for _dt, n in dated if n in keep]
  prune = [n for _dt, n in dated if n not in keep]
  return keep_names, prune


def build_manifest(*, created_at, data_dir, build_sha, consistency, source,
                   retention, encryption, artifacts, notes):
  """Assembles the manifest dict written beside the archives.

  Pure so a test can pin the exact shape restore-data.py later parses;
  the writer and the reader must never drift apart. ``consistency`` is
  "cold" (server verified down for the whole run) or
  "crash-consistent-per-tree" (--online: DB snapshot is internally
  consistent, but the surrounding trees were copied live with no
  cross-file ordering guarantee).
  """
  created = created_at.astimezone(timezone.utc)
  return {
    "manifest_version": MANIFEST_VERSION,
    "created_at": created.isoformat().replace("+00:00", "Z"),
    "created_unix": int(created.timestamp()),
    "data_dir": data_dir,
    "build_sha": build_sha,
    "consistency": consistency,
    "source": source,
    "retention": retention,
    "encryption": encryption,
    "artifacts": artifacts,
    "notes": notes,
  }


def diff_manifest(manifest, observed):
  """Compares a manifest against observed artifact facts. Pure.

  ``observed`` maps artifact name -> ``{"bytes": int, "sha256": str}``,
  or None when the file is absent. Returns a list of human-readable
  problem strings, empty only when the backup is wholly intact. This is
  the trust gate restore leans on before it touches /data, so it lives
  here where a test can drive it with crafted inputs.
  """
  problems = []
  version = manifest.get("manifest_version")
  if version != MANIFEST_VERSION:
    problems.append(
      f"manifest_version {version!r} != supported {MANIFEST_VERSION}")
  artifacts = manifest.get("artifacts") or []
  if not artifacts:
    problems.append("manifest lists no artifacts")
  for art in artifacts:
    name = art.get("name")
    obs = observed.get(name)
    if obs is None:
      problems.append(f"missing artifact: {name}")
      continue
    if obs.get("bytes") != art.get("bytes"):
      problems.append(
        f"size mismatch for {name}: "
        f"{obs.get('bytes')} != {art.get('bytes')} (recorded)")
    if obs.get("sha256") != art.get("sha256"):
      problems.append(f"sha256 mismatch for {name}")
  return problems


def verify_manifest_hashes(manifest, backup_dir, hasher=sha256_file):
  """Builds observed facts from disk, then defers to diff_manifest.

  Returns the same problem list. ``hasher`` is injectable only so the
  driver can swap in a progress-reporting variant; the comparison logic
  lives in the pure diff_manifest above.
  """
  observed = {}
  for art in manifest.get("artifacts") or []:
    name = art.get("name")
    path = os.path.join(backup_dir, name)
    if not os.path.isfile(path):
      observed[name] = None
      continue
    observed[name] = {
      "bytes": os.path.getsize(path),
      "sha256": hasher(path),
    }
  return diff_manifest(manifest, observed)


def target_is_newer(live_newest_unix, backup_created_unix):
  """Advisory only: True when the live target's newest mtime is later
  than the backup's capture time.

  This is NOT a reliable ordering relation (integer-second mtimes, clock
  skew, restore-then-rebackup all defeat it), so restore uses it only to
  ANNOTATE its refusal message. The real gate is target non-emptiness +
  --force; see restore-data.py.
  """
  return live_newest_unix > backup_created_unix


# Only these errnos are a DEFINITIVE "nothing is listening" — the sole
# signal we accept as "server down." A DNS failure (gaierror) is the same
# class (the host resolves to nothing). Everything else stays "up."
_DOWN_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def server_responding(url, timeout=3.0):
  """True when a server is (or MIGHT be) up at url. FAIL-SAFE.

  This is a safety gate: a cold backup / a restore must never proceed
  against a live-but-wedged server. So only a DEFINITIVE
  connection-refused / host-unreachable / no-such-host counts as 'down'
  (return False). Any HTTP answer, a TIMEOUT, a TLS error, or any other
  probe failure is treated as RESPONDING (return True) — a hung server
  that accepts the socket but never replies must not mint a cold-labeled
  backup or let a restore swap the DB underneath it.
  """
  try:
    with urllib.request.urlopen(url, timeout=timeout) as r:
      return r.status < 600
  except urllib.error.HTTPError:
    return True  # the server answered, even if 4xx/5xx
  except urllib.error.URLError as e:
    reason = e.reason
    if isinstance(reason, socket.gaierror):
      return False  # host does not resolve -> nothing is there
    # A timeout surfaces as URLError(reason=TimeoutError); that is NOT a
    # definitive down -> fall through to 'up'.
    if isinstance(reason, OSError) and not isinstance(reason, TimeoutError) \
        and getattr(reason, "errno", None) in _DOWN_ERRNOS:
      return False
    return True  # timeout / TLS / unknown transport error -> assume up
  except (TimeoutError, socket.timeout):
    return True  # wedged server accepted the socket but never replied
  except Exception:
    return True  # anything unexpected -> fail safe, assume up


class RollbackError(Exception):
  """The forward restore failed AND rolling the originals back also
  failed for at least one entry.

  Carries what is where so the operator (or recoveryd) can finish the
  recovery by hand. The invariant this exception exists to signal: the
  originals it names are STILL in ``rollback_dir`` and were NEVER
  deleted — the caller MUST NOT delete that directory.
  """

  def __init__(self, original_error, rollback_dir, unrecovered, restored):
    self.original_error = original_error
    self.rollback_dir = rollback_dir
    self.unrecovered = unrecovered   # names still stashed in rollback_dir
    self.restored = restored          # names successfully put back
    super().__init__(
      f"restore failed ({original_error!r}) and rollback was INCOMPLETE; "
      f"{len(unrecovered)} original(s) remain stashed in {rollback_dir}: "
      f"{unrecovered}. DO NOT delete that directory — restore them by "
      f"hand.")


def _remove_path(path):
  """Removes a file, symlink, or directory tree if it exists."""
  if os.path.islink(path) or os.path.isfile(path):
    os.unlink(path)
  elif os.path.isdir(path):
    shutil.rmtree(path)


def swap_entries_transactional(staging, data_dir, rollback_dir):
  """Replaces each top-level entry in data_dir with the matching entry
  from staging, all-or-nothing, keeping displaced originals in
  rollback_dir.

  For each staged entry: an existing live entry is RENAMED into
  rollback_dir (never deleted) before the staged entry is renamed into
  place. On ANY forward failure, every change made so far is undone —
  entries added where none existed are removed, and every stashed
  original is moved back — so the caller is left with either the
  fully-restored tree or the exact original tree, never a half-restored
  mix (the atomicity the reviewer required).

  If a ROLLBACK step itself fails, the originals it could not restore are
  LEFT in rollback_dir and this raises RollbackError naming them; the
  caller must preserve rollback_dir. The invariant holds either way: an
  original is never deleted while it is still the only copy of that
  entry.

  staging, data_dir, and rollback_dir must share a filesystem so every
  rename is atomic and allocates a new inode (a process holding an old
  DB fd cannot then corrupt the restored file). The caller deletes
  rollback_dir only after this returns successfully. Returns the list of
  entry names now live from the backup.
  """
  os.makedirs(rollback_dir, exist_ok=True)
  swapped = []   # names whose original was stashed in rollback_dir
  added = []     # names that had no original (nothing to restore)
  try:
    for name in sorted(os.listdir(staging)):
      src = os.path.join(staging, name)
      dst = os.path.join(data_dir, name)
      if os.path.lexists(dst):
        # Stash first, then place. If the place fails, the rollback
        # loop below still finds the stashed original in rollback_dir.
        os.replace(dst, os.path.join(rollback_dir, name))
        os.replace(src, dst)
        swapped.append(name)
      else:
        os.replace(src, dst)
        added.append(name)
    return swapped + added
  except BaseException as forward_err:
    # Undo entries we added where nothing existed before (removing an
    # added entry never loses an original).
    for name in added:
      try:
        _remove_path(os.path.join(data_dir, name))
      except OSError:
        pass
    # Restore every stashed original. Scanning rollback_dir covers the
    # in-flight entry whose original was moved but whose replacement did
    # not land, not just the fully-swapped ones. Track any step that
    # itself fails — its original STAYS stashed (never deleted).
    unrecovered = []
    restored = []
    for name in sorted(os.listdir(rollback_dir)):
      dst = os.path.join(data_dir, name)
      try:
        _remove_path(dst)                                  # clear the slot
        os.replace(os.path.join(rollback_dir, name), dst)  # put it back
        restored.append(name)
      except OSError:
        # Could not restore this original; it remains in rollback_dir.
        unrecovered.append(name)
    if unrecovered:
      raise RollbackError(forward_err, rollback_dir, unrecovered, restored)
    raise


def fsync_path(path):
  """fsyncs a single file's contents to disk. Best-effort on platforms
  that reject fsync for the path type."""
  fd = os.open(path, os.O_RDONLY)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


def fsync_dir(path):
  """fsyncs a directory entry so a rename/create in it is durable.

  Durability of a rename needs the DIRECTORY fsync'd, not just the file —
  this is what makes the "publish, then prune" ordering safe across a
  crash. Best-effort: some filesystems reject directory fsync.
  """
  try:
    fd = os.open(path, os.O_RDONLY)
  except OSError:
    return
  try:
    os.fsync(fd)
  except OSError:
    pass
  finally:
    os.close(fd)


def fsync_tree(root):
  """fsyncs every file under root (RAISING on any file fsync failure —
  the caller must NOT prune older backups when the new one could not be
  made durable), then best-effort fsyncs the directories (dir fsync is
  unsupported on some filesystems; a rename's durability is a bonus, not
  the gate)."""
  for dirpath, _dirs, files in os.walk(root):
    for f in files:
      fsync_path(os.path.join(dirpath, f))  # raises on failure
    fsync_dir(dirpath)  # best-effort
