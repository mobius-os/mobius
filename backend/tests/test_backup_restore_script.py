"""backup-data.py / restore-data.py and backup_lib regressions.

Pure logic (hashing, manifest shape, retention selection + pinning,
transactional-swap rollback) is driven directly. The two scripts run end
to end through subprocess against a throwaway fake /data — no Docker —
proving the backup -> destroy -> restore round trip and every refusal
gate (running server, non-empty target, target under data root, missing
ack, encrypted-without-key, tampered artifact). Malicious archives, an
injected mid-swap failure, and a live-WAL-writer-during-backup pin the
principal safety boundaries the reviewer called out. age is baked into
the image; on a host without it the encrypted round trip is proven by
the rehearsed container drill (see the scripts' headers).
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

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
  assert lib.parse_backup_dirname(
    "mobius-backup-20260719T041700Z.partial") is None
  assert lib.parse_backup_dirname("mobius-backup-nonsense") is None
  assert lib.parse_backup_dirname("something-else") is None


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
  names = _names_for(range(40), base)
  keep, prune = lib.select_backups_to_prune(names, keep_daily=5, keep_weekly=3)
  assert len(keep) == 8
  assert len(prune) == 32
  assert set(keep).isdisjoint(prune)
  newest5 = _names_for(range(5), base)
  assert set(newest5) <= set(keep)
  weekly = [n for n in keep if n not in newest5]
  iso = [lib.parse_backup_dirname(n).isocalendar()[:2] for n in weekly]
  assert len(iso) == len(set(iso))


def test_rotation_never_prunes_unparseable_names():
  base = datetime(2026, 7, 19, tzinfo=timezone.utc)
  names = _names_for(range(3), base) + ["not-a-backup", ".backup.lock"]
  keep, prune = lib.select_backups_to_prune(names, keep_daily=1, keep_weekly=0)
  assert "not-a-backup" not in prune and "not-a-backup" not in keep
  assert ".backup.lock" not in prune


def test_rotation_pins_survive_outside_the_window():
  base = datetime(2026, 7, 19, tzinfo=timezone.utc)
  names = _names_for(range(5), base)  # 5 consecutive days
  oldest = names[-1]
  # retention=1 would normally prune the oldest four; pinning the oldest
  # (e.g. the last complete-secrets backup) keeps it regardless.
  keep, prune = lib.select_backups_to_prune(
    names, keep_daily=1, keep_weekly=0, pinned={oldest})
  assert oldest in keep and oldest not in prune
  assert names[0] in keep  # newest still kept by the daily window


def test_rotation_pins_the_clock_rolled_back_new_backup():
  base = datetime(2026, 7, 19, tzinfo=timezone.utc)
  # The "just published" backup has an OLDER timestamp than an existing
  # one (wall-clock rollback). Pinning it by name keeps it even though it
  # sorts as oldest.
  new_old = f"{lib.BACKUP_PREFIX}{lib.format_ts(base - timedelta(days=2))}"
  existing = f"{lib.BACKUP_PREFIX}{lib.format_ts(base)}"
  keep, prune = lib.select_backups_to_prune(
    [existing, new_old], keep_daily=1, keep_weekly=0, pinned={new_old})
  assert new_old in keep and new_old not in prune


def test_diff_manifest_flags_every_corruption_class():
  manifest = {
    "manifest_version": lib.MANIFEST_VERSION,
    "artifacts": [
      {"name": "data.tar.gz", "bytes": 10, "sha256": "aa"},
      {"name": "secrets.tar.gz.age", "bytes": 20, "sha256": "bb"},
    ],
  }
  ok = {
    "data.tar.gz": {"bytes": 10, "sha256": "aa"},
    "secrets.tar.gz.age": {"bytes": 20, "sha256": "bb"},
  }
  assert lib.diff_manifest(manifest, ok) == []
  bad = {
    "data.tar.gz": {"bytes": 11, "sha256": "aa"},
    "secrets.tar.gz.age": None,
  }
  problems = lib.diff_manifest(manifest, bad)
  assert any("size mismatch" in p for p in problems)
  assert any("missing artifact" in p for p in problems)
  bad_version = {"manifest_version": 999,
                 "artifacts": manifest["artifacts"]}
  vproblems = lib.diff_manifest(bad_version, ok)
  assert any("manifest_version" in p for p in vproblems)


def test_target_is_newer_boundary():
  # Advisory-only comparison used to annotate a refusal message.
  assert lib.target_is_newer(100, 99) is True
  assert lib.target_is_newer(100, 100) is False
  assert lib.target_is_newer(99, 100) is False


def test_swap_transactional_rolls_back_on_injected_failure(
  tmp_path, monkeypatch,
):
  """A failure on the Nth swap must leave the ORIGINAL tree intact — no
  half-restored mix — which is the atomicity the reviewer required."""
  data = tmp_path / "data"
  staging = tmp_path / "staging"
  rb = tmp_path / "rb"
  for n in "ABC":
    (data / n).mkdir(parents=True)
    (data / n / "orig.txt").write_text("OLD-" + n)
    (staging / n).mkdir(parents=True)
    (staging / n / "new.txt").write_text("NEW-" + n)

  real = os.replace
  calls = {"n": 0}

  def flaky(a, b):
    calls["n"] += 1
    if calls["n"] == 3:  # mid-way through the B entry
      raise OSError("injected mid-swap")
    return real(a, b)

  monkeypatch.setattr(lib.os, "replace", flaky)
  with pytest.raises(OSError):
    lib.swap_entries_transactional(str(staging), str(data), str(rb))

  # Every original is back; no NEW file leaked into the live tree.
  for n in "ABC":
    assert (data / n / "orig.txt").read_text() == "OLD-" + n
    assert not (data / n / "new.txt").exists()


def test_swap_transactional_happy_path(tmp_path):
  data = tmp_path / "data"
  staging = tmp_path / "staging"
  rb = tmp_path / "rb"
  (data / "keep").mkdir(parents=True)
  (data / "keep" / "orig").write_text("OLD")
  (staging / "keep").mkdir(parents=True)
  (staging / "keep" / "new").write_text("NEW")
  (staging / "added").mkdir(parents=True)
  (staging / "added" / "x").write_text("X")
  moved = lib.swap_entries_transactional(str(staging), str(data), str(rb))
  assert set(moved) == {"keep", "added"}
  assert (data / "keep" / "new").read_text() == "NEW"
  assert not (data / "keep" / "orig").exists()
  assert (data / "added" / "x").read_text() == "X"


# --------------------------------------------------------------------------
# Fixtures + subprocess wrappers
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
  db.close()
  (root / "apps" / "notes" / "data").mkdir(parents=True)
  (root / "apps" / "notes" / "data" / "n.json").write_text('{"k":"v"}')
  (root / "apps" / "notes" / "index.jsx").write_text("export default 1")
  (root / "shared" / "skills").mkdir(parents=True)
  (root / "shared" / "skills" / "s.md").write_text("# skill")
  (root / "cli-auth" / "claude").mkdir(parents=True)
  (root / "cli-auth" / "claude" / ".credentials.json").write_text("SECRET")
  (root / ".secret-key").write_text("SIGNING-KEY")
  (root / "service-token.txt").write_text("JWT")
  (root / ".recovery-secret").write_text("RSECRET")
  (root / ".recovery-owner.json").write_text('{"owner":"x"}')
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


def _dead_url():
  """A health URL whose port is (almost certainly) closed, so the server
  probe reads 'down' deterministically."""
  s = socket.socket()
  s.bind(("127.0.0.1", 0))
  port = s.getsockname()[1]
  s.close()
  return f"http://127.0.0.1:{port}/api/health"


def _backup(*args, expect=0, health=None):
  return _run(BACKUP, "--health-url", health or _dead_url(), *args,
              expect=expect)


def _restore(*args, expect=0, health=None):
  return _run(RESTORE, "--server-stopped", "--health-url",
              health or _dead_url(), *args, expect=expect)


class _OkHandler(BaseHTTPRequestHandler):
  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"ok")

  def log_message(self, *a):
    pass


def _serve():
  """Starts a stub 'backend' returning 200 on any path; returns (url,
  stop_fn)."""
  srv = HTTPServer(("127.0.0.1", 0), _OkHandler)
  t = threading.Thread(target=srv.serve_forever, daemon=True)
  t.start()
  url = f"http://127.0.0.1:{srv.server_address[1]}/api/health"
  return url, srv.shutdown


# --------------------------------------------------------------------------
# Backup: content routing, secrets, consistency, target + retention gates
# --------------------------------------------------------------------------


def test_backup_skips_secrets_without_recipient(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = next(target.glob("mobius-backup-*"))
  manifest = json.loads((bdir / "manifest.json").read_text())
  assert manifest["encryption"]["secrets"] == "skipped"
  assert manifest["consistency"] == "cold"
  assert not (bdir / "secrets.tar.gz").exists()
  assert not (bdir / "secrets.tar.gz.age").exists()
  with tarfile.open(bdir / "data.tar.gz") as t:
    members = t.getnames()
  assert "db/ultimate.db" in members
  assert any(m.endswith("apps/notes/data/n.json") for m in members)
  assert any(m.endswith("shared/skills/s.md") for m in members)
  assert not any("logs" in m for m in members)
  assert not any(m.startswith("platform") for m in members)
  assert not any(".secret-key" in m for m in members)


def test_backup_plaintext_secrets_perms_and_hashes(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--plaintext-secrets")
  bdir = next(target.glob("mobius-backup-*"))
  manifest = json.loads((bdir / "manifest.json").read_text())
  assert manifest["encryption"]["secrets"] == "plaintext"
  assert lib.verify_manifest_hashes(manifest, str(bdir)) == []
  # Partial dir was 0700 (final inherits the rename); plaintext secrets
  # file is 0600 with no chmod-after window.
  assert (os.stat(bdir).st_mode & 0o777) == 0o700
  assert (os.stat(bdir / "secrets.tar.gz").st_mode & 0o777) == 0o600
  with tarfile.open(bdir / "secrets.tar.gz") as t:
    secret_members = t.getnames()
  for expected in (".secret-key", "service-token.txt", ".recovery-secret",
                   ".recovery-owner.json"):
    assert any(m == expected or m.endswith("/" + expected)
               for m in secret_members), f"{expected} missing from secrets"
  assert any("cli-auth" in m for m in secret_members)
  with tarfile.open(bdir / "data.tar.gz") as t:
    data_members = t.getnames()
  allm = secret_members + data_members
  assert not any(m.endswith(".recover-pending") for m in allm)
  assert not any(m.endswith("recovery_chat.jsonl") for m in allm)
  assert not any(m.endswith(".boot-attempt") for m in allm)


def test_backup_refuses_running_server_and_allows_online(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  url, stop = _serve()
  try:
    # Default (cold) mode refuses while the backend answers.
    _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
         "--health-url", url, expect=2)
    assert not list(target.glob("mobius-backup-*"))
    # --online proceeds and labels the artifact honestly.
    _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
         "--health-url", url, "--online")
    bdir = next(target.glob("mobius-backup-*"))
    manifest = json.loads((bdir / "manifest.json").read_text())
    assert manifest["consistency"] == "crash-consistent-per-tree"
  finally:
    stop()


def test_backup_rejects_bad_targets(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  # target == data root
  _backup("--data-dir", str(data), "--target-dir", str(data), expect=2)
  # target strictly under data root on the same filesystem
  under = data / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(under), expect=2)
  assert not under.exists() or not list(under.glob("mobius-backup-*"))
  # ...allowed with the explicit same-volume opt-in
  _backup("--data-dir", str(data), "--target-dir", str(under),
          "--allow-same-volume")
  assert list(under.glob("mobius-backup-*"))


def test_backup_rejects_zero_retention(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--keep-daily", "0", "--keep-weekly", "0", expect=2)


def test_backup_pins_complete_secrets_backup_over_skipped(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  # Backup 1 carries complete (plaintext) secrets; 2 and 3 skip them.
  # With keep-daily=1 the newest wins the window, but the complete-secrets
  # backup must be PINNED so rotation never leaves only secrets-less ones.
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--plaintext-secrets", "--keep-daily", "1", "--keep-weekly", "0")
  complete = next(target.glob("mobius-backup-*")).name
  time.sleep(1.1)
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--keep-daily", "1", "--keep-weekly", "0")
  time.sleep(1.1)
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--keep-daily", "1", "--keep-weekly", "0")
  remaining = {p.name for p in target.glob("mobius-backup-*")}
  assert complete in remaining  # the only complete-secrets copy survived
  # The middle skipped backup was pruned; the newest + complete are kept.
  assert len(remaining) == 2


def test_backup_wal_writer_open_during_backup_snapshot_is_consistent(
  tmp_path,
):
  """An ACTIVE writer holding an uncommitted transaction (WAL live) must
  not corrupt or leak into the snapshot; committed rows are present, the
  uncommitted row is not, and integrity_check passes."""
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  dbpath = str(data / "db" / "ultimate.db")
  writer = sqlite3.connect(dbpath, timeout=30)
  writer.execute("PRAGMA journal_mode=WAL")
  writer.execute("PRAGMA busy_timeout=30000")
  # Commit a row (lands in the -wal), then hold an UNCOMMITTED insert open
  # across the whole backup.
  writer.execute("INSERT INTO t (v) VALUES ('committed')")
  writer.commit()
  writer.execute("BEGIN IMMEDIATE")
  writer.execute("INSERT INTO t (v) VALUES ('UNCOMMITTED')")
  try:
    _backup("--data-dir", str(data), "--target-dir", str(target),
            "--plaintext-secrets")
  finally:
    writer.rollback()
    writer.close()
  bdir = next(target.glob("mobius-backup-*"))
  import tempfile
  ex = Path(tempfile.mkdtemp())
  with tarfile.open(bdir / "data.tar.gz") as t:
    t.extract("db/ultimate.db", ex, filter="data")
  snap = sqlite3.connect(str(ex / "db" / "ultimate.db"))
  assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
  vals = {r[0] for r in snap.execute("SELECT v FROM t")}
  snap.close()
  assert "committed" in vals
  assert "UNCOMMITTED" not in vals


def test_encryption_requested_without_age_refuses(tmp_path):
  if shutil.which("age"):
    return
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--age-recipient", "age1fakefakefake", expect=2)
  assert not list(target.glob("mobius-backup-*"))


def test_backup_refuses_unreadable_secret_early(tmp_path):
  if os.geteuid() == 0:
    return
  data = _seed_data_dir(tmp_path / "data")
  os.chmod(data / ".secret-key", 0o000)
  target = tmp_path / "backups"
  proc = _backup("--data-dir", str(data), "--target-dir", str(target),
                 "--plaintext-secrets", expect=2)
  assert ".secret-key" in (proc.stdout + proc.stderr)
  assert not list(target.glob("mobius-backup-*"))
  # Skip mode never reads secrets, so the unreadable one is irrelevant.
  _backup("--data-dir", str(data), "--target-dir", str(target))
  assert len(list(target.glob("mobius-backup-*"))) == 1


# --------------------------------------------------------------------------
# Restore: round trip, refusal gates, malicious archives
# --------------------------------------------------------------------------


def test_restore_round_trip_and_gates(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--plaintext-secrets")
  bdir = str(next(target.glob("mobius-backup-*")))

  dest = tmp_path / "restored"
  dest.mkdir()

  # Refuse without the acknowledgement flag — and touch nothing.
  _run(RESTORE, bdir, "--data-dir", str(dest), "--server-stopped",
       "--health-url", _dead_url(), expect=4)
  assert not (dest / "db").exists()

  # Refuse without --server-stopped.
  _run(RESTORE, bdir, "--data-dir", str(dest),
       "--i-understand-this-overwrites", "--health-url", _dead_url(),
       expect=4)
  assert not (dest / "db").exists()

  # Empty target restores cleanly (no --force needed).
  _restore(bdir, "--data-dir", str(dest),
           "--i-understand-this-overwrites")
  assert (dest / "db" / "ultimate.db").exists()
  assert (dest / "apps" / "notes" / "data" / "n.json").read_text() == \
    '{"k":"v"}'
  assert (dest / ".secret-key").read_text() == "SIGNING-KEY"
  assert (os.stat(dest / ".secret-key").st_mode & 0o777) == 0o600
  conn = sqlite3.connect(str(dest / "db" / "ultimate.db"))
  assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
  conn.close()

  # Now the target holds data -> restoring again needs --force.
  _restore(bdir, "--data-dir", str(dest),
           "--i-understand-this-overwrites", expect=4)
  _restore(bdir, "--data-dir", str(dest),
           "--i-understand-this-overwrites", "--force")


def test_restore_refuses_running_server(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--plaintext-secrets")
  bdir = str(next(target.glob("mobius-backup-*")))
  dest = tmp_path / "restored"
  dest.mkdir()
  url, stop = _serve()
  try:
    _run(RESTORE, bdir, "--data-dir", str(dest), "--server-stopped",
         "--i-understand-this-overwrites", "--health-url", url, expect=4)
    assert not (dest / "db").exists()
  finally:
    stop()


def test_restore_refuses_tampered_backup(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--plaintext-secrets")
  bdir = Path(next(target.glob("mobius-backup-*")))
  with open(bdir / "data.tar.gz", "ab") as f:
    f.write(b"tampered")
  dest = tmp_path / "restored"
  dest.mkdir()
  proc = _restore(str(bdir), "--data-dir", str(dest),
                  "--i-understand-this-overwrites", expect=3)
  assert "verification" in (proc.stdout + proc.stderr).lower()
  assert not (dest / "db").exists()


def _maltar(path, name, *, ttype=tarfile.REGTYPE, linkname=""):
  with tarfile.open(path, "w:gz") as tar:
    ti = tarfile.TarInfo(name)
    ti.type = ttype
    if ttype == tarfile.REGTYPE:
      data = b"x"
      ti.size = len(data)
      import io
      tar.addfile(ti, io.BytesIO(data))
    else:
      ti.linkname = linkname
      tar.addfile(ti)


def test_safe_extract_contains_malicious_members(tmp_path):
  """The Python 3.12 'data' filter the restore relies on must never let a
  member escape the destination. Traversal and escaping sym/hardlinks are
  REJECTED outright; an absolute path is NEUTRALISED (leading slash
  stripped, so it stays inside dest) — both are safe."""
  raising = [
    ("traversal", dict(name="../escape.txt")),
    ("symlink", dict(name="lnk", ttype=tarfile.SYMTYPE,
                     linkname="../../../../etc/passwd")),
    ("hardlink", dict(name="hrd", ttype=tarfile.LNKTYPE,
                      linkname="../../../../etc/passwd")),
  ]
  for label, kw in raising:
    arc = tmp_path / f"{label}.tar.gz"
    _maltar(str(arc), **kw)
    dest = tmp_path / f"out-{label}"
    dest.mkdir()
    with pytest.raises(tarfile.FilterError):
      with tarfile.open(arc) as t:
        t.extractall(dest, filter="data")
    assert list(dest.iterdir()) == []  # nothing escaped

  # Absolute path: contained under dest, never written at the abs path.
  arc = tmp_path / "abs.tar.gz"
  _maltar(str(arc), name="/tmp/mobius-escape-should-not-exist")
  dest = tmp_path / "out-abs"
  dest.mkdir()
  with tarfile.open(arc) as t:
    t.extractall(dest, filter="data")
  assert not os.path.exists("/tmp/mobius-escape-should-not-exist")
  assert (dest / "tmp" / "mobius-escape-should-not-exist").exists()
