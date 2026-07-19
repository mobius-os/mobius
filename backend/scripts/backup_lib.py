"""Pure helpers shared by backup-data.py and restore-data.py.

This module is deliberately free of any side effect that needs a
container or a live /data volume: hashing, manifest assembly, retention
selection, and the staleness comparison. Keeping them here lets the
suite exercise the parts most likely to silently corrupt a restore (a
wrong hash, a rotation that deletes the wrong backup) without Docker.
The two driver scripts own every side effect — sqlite snapshot, tar,
age, filesystem moves; this module only computes and compares.
"""

from __future__ import annotations

import hashlib
import os
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


def select_backups_to_prune(names, keep_daily, keep_weekly):
  """Splits backup dir names into (keep, prune) under an N-daily +
  M-weekly policy.

  A backup is kept when it is one of the ``keep_daily`` most recent, OR
  it is the newest backup in one of the next ``keep_weekly`` distinct
  ISO weeks below the daily window. Everything else prunes.

  Invariant: an unparseable name is never returned in ``prune`` — the
  caller must not delete what this function could not positively
  identify as a backup. Both lists come back newest-first.
  """
  dated = []
  for n in names:
    dt = parse_backup_dirname(n)
    if dt is not None:
      dated.append((dt, n))
  # The timestamp is embedded in the name, so this is a total order.
  dated.sort(key=lambda x: x[0], reverse=True)

  keep = set()
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


def build_manifest(*, created_at, data_dir, build_sha, source, retention,
                   encryption, artifacts, notes):
  """Assembles the manifest dict written beside the archives.

  Pure so a test can pin the exact shape restore-data.py later parses;
  the writer and the reader must never drift apart.
  """
  created = created_at.astimezone(timezone.utc)
  return {
    "manifest_version": MANIFEST_VERSION,
    "created_at": created.isoformat().replace("+00:00", "Z"),
    "created_unix": int(created.timestamp()),
    "data_dir": data_dir,
    "build_sha": build_sha,
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
  """True when the live target holds data written after the backup was
  captured — the signal restore uses to refuse a stale overwrite unless
  forced. Equal timestamps are NOT newer, so restoring a just-taken
  backup onto its own instance is allowed.
  """
  return live_newest_unix > backup_created_unix
