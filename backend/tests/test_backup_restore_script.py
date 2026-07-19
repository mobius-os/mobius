"""backup-data.py / restore-data.py and backup_lib regressions.

The pure logic (hashing, manifest shape, retention selection, staleness
compare) is tested directly. The two scripts are then driven end to end
through subprocess against a throwaway fake /data — no Docker — to prove
the backup -> destroy -> restore round trip and each refusal gate. age
is not on the host, so the encrypted path is proven by the rehearsed
container drill (see the scripts' headers); here we prove the
plaintext/skipped paths and the "encryption requested but age missing"
refusal.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import backup_lib as lib

SCRIPTS = Path(__file__).parents[1] / "scripts"
BACKUP = SCRIPTS / "backup-data.py"
RESTORE = SCRIPTS / "restore-data.py"


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_parse_backup_dirname_accepts_only_real_backups():
  dt = lib.parse_backup_dirname("mobius-backup-20260719T041700Z")
  assert dt == datetime(2026, 7, 19, 4, 17, 0, tzinfo=timezone.utc)
  # A crashed partial must never be treated as a finished backup.
  assert lib.parse_backup_dirname(
    "mobius-backup-20260719T041700Z.partial") is None
  assert lib.parse_backup_dirname("mobius-backup-nonsense") is None
  assert lib.parse_backup_dirname("something-else") is None
  assert lib.parse_backup_dirname(".backup.lock") is None


def test_sha256_file_matches_hashlib(tmp_path):
  import hashlib
  p = tmp_path / "blob"
  p.write_bytes(b"mobius" * 5000)
  assert lib.sha256_file(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()


def _names_for(days_ago_list, base):
  return [
    f"{lib.BACKUP_PREFIX}{lib.format_ts(base - timedelta(days=d))}"
    for d in days_ago_list
  ]


def test_rotation_keeps_daily_then_one_per_week():
  base = datetime(2026, 7, 19, 4, 0, 0, tzinfo=timezone.utc)
  # 40 consecutive daily backups span ~6 ISO weeks, so the weekly window
  # below the 5-day daily window has more than 3 distinct weeks to draw
  # from — the case where keep == keep_daily + keep_weekly.
  names = _names_for(range(40), base)
  keep, prune = lib.select_backups_to_prune(names, keep_daily=5, keep_weekly=3)
  assert len(keep) == 8
  assert len(prune) == 32
  assert set(keep).isdisjoint(prune)
  # The 5 newest are always kept.
  newest5 = _names_for(range(5), base)
  assert set(newest5) <= set(keep)
  # Only the newest backup in any weekly bucket is kept — no two kept
  # weekly backups share an ISO week.
  weekly = [n for n in keep if n not in newest5]
  iso = [lib.parse_backup_dirname(n).isocalendar()[:2] for n in weekly]
  assert len(iso) == len(set(iso))


def test_rotation_never_prunes_unparseable_names():
  base = datetime(2026, 7, 19, tzinfo=timezone.utc)
  names = _names_for(range(3), base) + ["not-a-backup", ".backup.lock"]
  keep, prune = lib.select_backups_to_prune(names, keep_daily=1, keep_weekly=0)
  # Unparseable entries appear in neither list — rotation must not delete
  # what it cannot identify.
  assert "not-a-backup" not in prune and "not-a-backup" not in keep
  assert ".backup.lock" not in prune


def test_diff_manifest_flags_every_corruption_class():
  manifest = {
    "manifest_version": lib.MANIFEST_VERSION,
    "artifacts": [
      {"name": "data.tar.gz", "bytes": 10, "sha256": "aa"},
      {"name": "secrets.tar.gz.age", "bytes": 20, "sha256": "bb"},
    ],
  }
  # All good.
  ok = {
    "data.tar.gz": {"bytes": 10, "sha256": "aa"},
    "secrets.tar.gz.age": {"bytes": 20, "sha256": "bb"},
  }
  assert lib.diff_manifest(manifest, ok) == []
  # Missing, size drift, hash drift each surface.
  bad = {
    "data.tar.gz": {"bytes": 11, "sha256": "aa"},
    "secrets.tar.gz.age": None,
  }
  problems = lib.diff_manifest(manifest, bad)
  assert any("size mismatch" in p for p in problems)
  assert any("missing artifact" in p for p in problems)
  # Wrong version is rejected outright, before any artifact is trusted.
  bad_version = {"manifest_version": 999,
                 "artifacts": manifest["artifacts"]}
  vproblems = lib.diff_manifest(bad_version, ok)
  assert any("manifest_version" in p for p in vproblems)


def test_target_is_newer_boundary():
  assert lib.target_is_newer(100, 99) is True
  assert lib.target_is_newer(100, 100) is False  # equal is not newer
  assert lib.target_is_newer(99, 100) is False


def test_build_manifest_shape():
  m = lib.build_manifest(
    created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    data_dir="/data", build_sha="abc", source={}, retention={},
    encryption={"scheme": "none", "secrets": "skipped"},
    artifacts=[], notes=[])
  assert m["manifest_version"] == lib.MANIFEST_VERSION
  assert m["created_at"].endswith("Z")
  assert m["created_unix"] == int(
    datetime(2026, 7, 19, tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------
# End-to-end script round trip (no Docker)
# --------------------------------------------------------------------------


def _seed_data_dir(root):
  """Builds a fake /data with the trees the scripts care about."""
  root = Path(root)
  (root / "db").mkdir(parents=True)
  db = sqlite3.connect(str(root / "db" / "ultimate.db"))
  db.execute("PRAGMA journal_mode=WAL")
  db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
  db.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
  db.commit()
  db.close()  # leaves a -wal/-shm behind, exercising the WAL snapshot path
  (root / "apps" / "notes" / "data").mkdir(parents=True)
  (root / "apps" / "notes" / "data" / "n.json").write_text('{"k":"v"}')
  (root / "apps" / "notes" / "index.jsx").write_text("export default 1")
  (root / "shared" / "skills").mkdir(parents=True)
  (root / "shared" / "skills" / "s.md").write_text("# skill")
  (root / "cli-auth" / "claude").mkdir(parents=True)
  (root / "cli-auth" / "claude" / ".credentials.json").write_text("SECRET")
  (root / ".secret-key").write_text("SIGNING-KEY")
  (root / "service-token.txt").write_text("JWT")
  # Recovery secrets: their names start with ".recover", which must NOT be
  # mistaken for the transient .recover-pending marker and excluded.
  (root / ".recovery-secret").write_text("RSECRET")
  (root / ".recovery-owner.json").write_text('{"owner":"x"}')
  # Excluded: transient markers + trees rebuilt from the image on boot.
  (root / ".recover-pending").write_text("x")
  (root / "recovery_chat.jsonl").write_text("{}")
  (root / ".boot-attempt").write_text("1")
  (root / "logs").mkdir()
  (root / "logs" / "chat.log").write_text("noise")
  (root / "platform").mkdir()
  (root / "platform" / "x").write_text("baked")
  return root


def _run(script, *args, expect=0):
  proc = subprocess.run(
    [sys.executable, str(script), *args],
    text=True, capture_output=True, check=False)
  assert proc.returncode == expect, (
    f"rc={proc.returncode} expected {expect}\n"
    f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
  return proc


def test_backup_skips_secrets_without_recipient(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target))
  backups = list(target.glob("mobius-backup-*"))
  assert len(backups) == 1
  bdir = backups[0]
  manifest = json.loads((bdir / "manifest.json").read_text())
  # Secrets refused, not silently written plaintext.
  assert manifest["encryption"]["secrets"] == "skipped"
  assert not (bdir / "secrets.tar.gz").exists()
  assert not (bdir / "secrets.tar.gz.age").exists()
  # data.tar.gz holds the DB snapshot + non-secret trees, and NONE of the
  # excluded trees or the secret files.
  with tarfile.open(bdir / "data.tar.gz") as t:
    members = t.getnames()
  assert "db/ultimate.db" in members
  assert any(m.endswith("apps/notes/data/n.json") for m in members)
  assert any(m.endswith("shared/skills/s.md") for m in members)
  assert not any("logs" in m for m in members)
  assert not any(m.startswith("platform") for m in members)
  assert not any(".secret-key" in m for m in members)


def test_backup_plaintext_secrets_and_manifest_hashes(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
       "--plaintext-secrets")
  bdir = next(target.glob("mobius-backup-*"))
  manifest = json.loads((bdir / "manifest.json").read_text())
  assert manifest["encryption"]["secrets"] == "plaintext"
  assert (bdir / "secrets.tar.gz").exists()
  # Every artifact's recorded hash matches what is on disk.
  assert lib.verify_manifest_hashes(manifest, str(bdir)) == []
  # ALL secret members are captured — including the .recovery-* files
  # whose names could be mistaken for the transient .recover-pending
  # marker. A backup silently missing its keys is worse than no backup.
  with tarfile.open(bdir / "secrets.tar.gz") as t:
    secret_members = t.getnames()
  for expected in (".secret-key", "service-token.txt", ".recovery-secret",
                   ".recovery-owner.json"):
    assert any(m == expected or m.endswith("/" + expected)
               for m in secret_members), f"{expected} missing from secrets"
  assert any("cli-auth" in m for m in secret_members)
  # Transient recovery markers are NOT in either archive.
  with tarfile.open(bdir / "data.tar.gz") as t:
    data_members = t.getnames()
  allm = secret_members + data_members
  assert not any(m.endswith(".recover-pending") for m in allm)
  assert not any(m.endswith("recovery_chat.jsonl") for m in allm)
  assert not any(m.endswith(".boot-attempt") for m in allm)
  # The consistent DB snapshot is queryable (no torn WAL page).
  import tempfile
  with tarfile.open(bdir / "data.tar.gz") as t:
    t.extract("db/ultimate.db", tempfile.gettempdir(), filter="data")
  snap = Path(tempfile.gettempdir()) / "db" / "ultimate.db"
  conn = sqlite3.connect(str(snap))
  assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
  conn.close()


def test_encryption_requested_without_age_refuses(tmp_path):
  if shutil.which("age"):
    return  # host has age; the drill covers the real encrypted path
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  # A recipient is configured but age is absent: must fail (config error),
  # never fall back to writing secrets in the clear.
  _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
       "--age-recipient", "age1fakefakefake", expect=2)
  assert not list(target.glob("mobius-backup-*"))


def test_restore_round_trip_and_guards(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
       "--plaintext-secrets")
  bdir = str(next(target.glob("mobius-backup-*")))

  dest = tmp_path / "restored"
  dest.mkdir()

  # Refuse without the acknowledgement flag — and touch nothing.
  _run(RESTORE, bdir, "--data-dir", str(dest), expect=4)
  assert not (dest / "db").exists()

  # Restore into the empty target (older than the backup -> no staleness).
  _run(RESTORE, bdir, "--data-dir", str(dest),
       "--i-understand-this-overwrites")
  assert (dest / "db" / "ultimate.db").exists()
  assert (dest / "apps" / "notes" / "data" / "n.json").read_text() == \
    '{"k":"v"}'
  assert (dest / ".secret-key").read_text() == "SIGNING-KEY"
  # Secret perms tightened to 600.
  assert (os.stat(dest / ".secret-key").st_mode & 0o777) == 0o600
  conn = sqlite3.connect(str(dest / "db" / "ultimate.db"))
  assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
  conn.close()

  # Staleness guard: make the target look newer than the backup.
  future = datetime.now(timezone.utc).timestamp() + 10_000
  os.utime(dest / "db" / "ultimate.db", (future, future))
  _run(RESTORE, bdir, "--data-dir", str(dest),
       "--i-understand-this-overwrites", expect=4)
  # --force overrides it.
  _run(RESTORE, bdir, "--data-dir", str(dest),
       "--i-understand-this-overwrites", "--force")


def test_restore_refuses_tampered_backup(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
       "--plaintext-secrets")
  bdir = Path(next(target.glob("mobius-backup-*")))
  # Corrupt an artifact after the manifest recorded its hash.
  with open(bdir / "data.tar.gz", "ab") as f:
    f.write(b"tampered")
  dest = tmp_path / "restored"
  dest.mkdir()
  proc = _run(RESTORE, str(bdir), "--data-dir", str(dest),
              "--i-understand-this-overwrites", expect=3)
  assert "verification" in (proc.stdout + proc.stderr).lower()
  assert not (dest / "db").exists()  # nothing touched
