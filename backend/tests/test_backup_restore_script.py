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
  (root / "credits-client").mkdir()
  (root / "credits-client" / "credentials.json").write_text('{"sealed": "KEY"}')
  (root / ".secret-key").write_text("SIGNING-KEY")
  # The key vault: encrypted keys next to their master key, plus a live socket.
  (root / ".vault").mkdir()
  (root / ".vault" / "master.key").write_bytes(b"M" * 32)
  (root / ".vault" / "keys.json").write_text('{"version": 1, "keys": {}}')
  # Bind by relative name: socket paths are limited to ~108 bytes.
  _cwd = os.getcwd()
  os.chdir(root / ".vault")
  try:
    _vault_socket = socket.socket(socket.AF_UNIX)
    _vault_socket.bind("vault.sock")
    _vault_socket.close()
  finally:
    os.chdir(_cwd)
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
  # Tests back up onto the same tmp filesystem by construction — a
  # deliberate local copy, which is exactly what --allow-same-volume
  # exists to acknowledge. test_backup_rejects_bad_targets covers the
  # refusal shape without the flag.
  return _run(BACKUP, "--health-url", health or _dead_url(),
              "--allow-same-volume", *args, expect=expect)


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
  assert any(m.endswith("recovery_chat.jsonl") for m in members)
  assert not any("logs" in m for m in members)
  assert not any(m.startswith("platform") for m in members)
  assert not any(".secret-key" in m for m in members)
  # Keys and their master key never land in the unencrypted archive.
  assert not any(".vault" in m for m in members)
  assert not any("credits-client" in m for m in members)
  assert not any(".recovery-secret" in m for m in members)
  assert not any(".recovery-owner.json" in m for m in members)
  assert not any(".recover-pending" in m for m in members)


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
  for expected in (".secret-key", "service-token.txt"):
    assert any(m == expected or m.endswith("/" + expected)
               for m in secret_members), f"{expected} missing from secrets"
  assert any("cli-auth" in m for m in secret_members)
  assert {".vault/master.key", ".vault/keys.json"} <= set(secret_members)
  assert ".vault/vault.sock" not in secret_members
  assert any(m.endswith("credits-client/credentials.json") for m in secret_members)
  with tarfile.open(bdir / "data.tar.gz") as t:
    data_members = t.getnames()
  assert not any("credits-client" in m for m in data_members)
  allm = secret_members + data_members
  assert not any(m.endswith(".recovery-secret") for m in allm)
  assert not any(m.endswith(".recovery-owner.json") for m in allm)
  assert not any(m.endswith(".recover-pending") for m in allm)
  assert any(m.endswith("recovery_chat.jsonl") for m in data_members)
  assert not any(m.endswith(".boot-attempt") for m in allm)


def test_backup_refuses_running_server_and_allows_online(tmp_path):
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  url, stop = _serve()
  try:
    # Default (cold) mode refuses while the backend answers.
    _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
         "--health-url", url, "--allow-same-volume", expect=2)
    assert not list(target.glob("mobius-backup-*"))
    # --online proceeds and labels the artifact honestly.
    _run(BACKUP, "--data-dir", str(data), "--target-dir", str(target),
         "--health-url", url, "--allow-same-volume", "--online")
    bdir = next(target.glob("mobius-backup-*"))
    manifest = json.loads((bdir / "manifest.json").read_text())
    assert manifest["consistency"] == "crash-consistent-per-tree"
  finally:
    stop()


def test_backup_rejects_bad_targets(tmp_path):
  # Deliberately bypasses the _backup helper: these are exactly the
  # no---allow-same-volume refusals the helper opts out of.
  data = _seed_data_dir(tmp_path / "data")
  dead = _dead_url()
  # target == data root
  _run(BACKUP, "--health-url", dead, "--data-dir", str(data),
       "--target-dir", str(data), expect=2)
  # target strictly under data root on the same filesystem
  under = data / "backups"
  _run(BACKUP, "--health-url", dead, "--data-dir", str(data),
       "--target-dir", str(under), expect=2)
  assert not under.exists() or not list(under.glob("mobius-backup-*"))
  # same-device target OUTSIDE the data root also needs the opt-in
  outside = tmp_path / "elsewhere"
  _run(BACKUP, "--health-url", dead, "--data-dir", str(data),
       "--target-dir", str(outside), expect=2)
  # ...allowed with the explicit same-volume opt-in
  _backup("--data-dir", str(data), "--target-dir", str(under))
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
  credits_key = dest / "credits-client" / "credentials.json"
  assert credits_key.read_text() == '{"sealed": "KEY"}'
  assert (os.stat(credits_key).st_mode & 0o777) == 0o600
  assert (os.stat(credits_key.parent).st_mode & 0o777) == 0o700
  assert (dest / "recovery_chat.jsonl").read_text() == "{}"
  assert not (dest / ".recovery-secret").exists()
  assert not (dest / ".recovery-owner.json").exists()
  assert not (dest / ".recover-pending").exists()
  conn = sqlite3.connect(str(dest / "db" / "ultimate.db"))
  assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
  conn.close()

  # Now the target holds data -> restoring again needs --force.
  _restore(bdir, "--data-dir", str(dest),
           "--i-understand-this-overwrites", expect=4)
  _restore(bdir, "--data-dir", str(dest),
           "--i-understand-this-overwrites", "--force")


def _vault_program():
  import importlib.util
  spec = importlib.util.spec_from_file_location(
    "mobius_restore_test_vault", SCRIPTS.parent / "vault" / "keyvault.py")
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def _installation(slug, generation=1):
  """The installation id an app ``slug`` got (a reinstall is another generation)."""
  import hashlib
  return hashlib.md5(f"{slug}-{generation}".encode()).hexdigest()


def _set_apps(data, apps, *, install_ids=True):
  """The apps table of the fake /data database: id -> slug, or id -> (slug,
  installation id) for an app installed again under the same slug, or
  (slug, installation id, token nonce) for one whose nonce rotated (a trash
  and recovery, a data wipe). ``install_ids`` False writes a database from
  before installation ids."""
  rows = []
  for app_id, app in sorted(apps.items()):
    slug, install, nonce = (*((app,) if isinstance(app, str) else app), None, None)[:3]
    rows.append((app_id, slug, nonce or f"nonce-{slug}", install or _installation(slug)))
  db = sqlite3.connect(str(Path(data) / "db" / "ultimate.db"))
  db.execute("DROP TABLE IF EXISTS apps")
  if install_ids:
    db.execute("CREATE TABLE apps (id INTEGER PRIMARY KEY, slug TEXT, token_nonce TEXT, install_id TEXT)")
    db.executemany("INSERT INTO apps (id, slug, token_nonce, install_id) VALUES (?, ?, ?, ?)", rows)
  else:
    db.execute("CREATE TABLE apps (id INTEGER PRIMARY KEY, slug TEXT, token_nonce TEXT)")
    db.executemany("INSERT INTO apps (id, slug, token_nonce) VALUES (?, ?, ?)", [r[:3] for r in rows])
  db.commit()
  db.close()


def _current_installs(data):
  db = sqlite3.connect(str(Path(data) / "db" / "ultimate.db"))
  try:
    return dict(db.execute("SELECT id, install_id FROM apps").fetchall())
  finally:
    db.close()


def _vault_granting(data, kv, app_ids):
  """A real vault in the fake /data: one key granted to ``app_ids``, each in
  the installation the current database gives it, and an open request from
  the last of them."""
  installs = _current_installs(data)
  shutil.rmtree(Path(data) / ".vault")
  vault = kv.Vault(Path(data) / ".vault")
  site = {"host": "api.example.com", "methods": ["GET"], "paths": ["/v1/*"]}
  vault.put_key("shared", {
    "value": "dummy-restore-key-0123456789",
    "inject": {"type": "header", "name": "Authorization", "prefix": "Bearer "},
    "grants": {"apps": [{"id": i, "install": installs[i], "sites": [site]} for i in app_ids],
               "agents": True, "sites": []},
  })
  vault.request_access(app_ids[-1], installs[app_ids[-1]], {"key": "other", "reason": "x", "sites": [site]})


def test_a_restore_without_secrets_withdraws_grants_its_apps_do_not_match(tmp_path):
  """The backup has no secrets, so the current vault stays. Its grants name
  this install's app installations: those the restored database lacks are
  withdrawn, and the owner is told to review the rest."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: "weather"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  # Since the backup: weather was purged and its id reused by chess; maps is new.
  _set_apps(data, {1: "notes", 2: "chess", 3: "maps"})
  _vault_granting(data, kv, [1, 2, 3])
  # The agents' card names no app, so no restore withdraws it.
  kv.Vault(data / ".vault").request_for_agents(
    {"key": "shared", "reason": "x", "sites": [{"host": "api.example.com", "methods": ["GET"], "paths": ["/*"]}]},
    actor="agent:c1",
  )

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "is KEPT" in restored.stdout and "Settings › Keys" in restored.stdout
  assert "app ids [2, 3]" in restored.stdout
  vault = kv.Vault(data / ".vault")
  [key] = vault.list_keys()
  assert [grant["id"] for grant in key["grants"]["apps"]] == [1]
  assert [r["id"] for r in vault.list_requests()] == ["agent-shared"]
  assert vault.reveal("shared", ("owner", "")) == "dummy-restore-key-0123456789"


def test_a_secrets_archive_from_before_the_vault_still_withdraws_stale_grants(tmp_path):
  """A backup made before the vault existed has secrets but no `.vault`, so
  the current vault is kept just as when the backup has no secrets at all:
  grants to installations the restored database lacks are withdrawn."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  shutil.rmtree(data / ".vault")
  _set_apps(data, {1: "notes", 2: "weather"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target), "--plaintext-secrets")
  bdir = str(next(target.glob("mobius-backup-*")))
  with tarfile.open(Path(bdir) / "secrets.tar.gz") as archive:
    assert not any(m.startswith(".vault") for m in archive.getnames())
  _set_apps(data, {1: "notes", 2: "chess", 3: "maps"})
  (data / ".vault").mkdir()
  _vault_granting(data, kv, [1, 2, 3])

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "is KEPT" in restored.stdout and "app ids [2, 3]" in restored.stdout
  [key] = kv.Vault(data / ".vault").list_keys()
  assert [grant["id"] for grant in key["grants"]["apps"]] == [1]


def test_a_reused_id_with_the_same_slug_is_still_another_app_after_a_restore(tmp_path):
  """A purged app's id can go to a new installation of an app with the same
  slug; its installation id differs, so the kept vault's grant is withdrawn."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: ("weather", _installation("weather", 1))})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: ("weather", _installation("weather", 2))})
  _vault_granting(data, kv, [1, 2])

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "app ids [2]" in restored.stdout
  [key] = kv.Vault(data / ".vault").list_keys()
  assert [grant["id"] for grant in key["grants"]["apps"]] == [1]


def _load_restore():
  import importlib.util
  spec = importlib.util.spec_from_file_location("mobius_restore_under_test", RESTORE)
  restore = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(restore)
  return restore


def test_the_image_ships_the_vault_program_where_the_restore_script_loads_it(tmp_path):
  """The shipped restore runs as /app/scripts/restore-data.py, so the image
  must carry the vault program where that copy resolves it. Lay out /app from
  the Dockerfile's own `COPY backend/<dir> ./<dir>/` lines and ask the script."""
  import importlib.util
  import re
  dockerfile = (SCRIPTS.parents[1] / "Dockerfile").read_text(encoding="utf-8")
  app_root = tmp_path / "app"
  for source, dest in re.findall(r"^COPY (backend/[\w-]+)/? \./([\w-]+)/$", dockerfile, re.M):
    shutil.copytree(SCRIPTS.parents[1] / source, app_root / dest)
  spec = importlib.util.spec_from_file_location(
    "mobius_image_restore", app_root / "scripts" / "restore-data.py")
  restore = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(restore)
  assert os.path.realpath(restore.VAULT_PROGRAM) == os.path.realpath(app_root / "vault" / "keyvault.py")
  assert os.path.isfile(restore.VAULT_PROGRAM)


def _apps(data):
  db = sqlite3.connect(str(Path(data) / "db" / "ultimate.db"))
  try:
    return dict(db.execute("SELECT id, slug FROM apps").fetchall())
  finally:
    db.close()


def _granted_ids(kv, data):
  [key] = kv.Vault(Path(data) / ".vault").list_keys()
  return [grant["id"] for grant in key["grants"]["apps"]]


def test_a_restore_that_cannot_update_a_running_vault_changes_nothing(tmp_path):
  """The kept vault's grants must be checked against the restored apps, so a
  vault that can't be opened (here: still running) stops the restore before
  it touches /data, rather than leaving grants that may name other apps."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: "maps"})
  _vault_granting(data, kv, [1, 2])
  running = kv.hold_single_instance_lock(data / ".vault")
  try:
    refused = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force", expect=4)
  finally:
    os.close(running)
  assert "Stop Möbius and its key vault" in refused.stderr and "/data unchanged" in refused.stderr
  assert _apps(data) == {1: "notes", 2: "maps"}
  assert _granted_ids(kv, data) == [1, 2]
  assert not list(data.glob(".restore-*"))


@pytest.mark.parametrize("current_database", ["missing", "unreadable"])
def test_a_restore_keeps_grants_the_restored_database_holds_even_without_the_current_one(
  tmp_path, current_database,
):
  """Only the restored database is needed: a grant names its installation,
  so a current database that is lost or corrupt (the disaster a restore is
  for) withdraws nothing the restored one holds."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: "weather"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _vault_granting(data, kv, [1, 2])
  db = data / "db" / "ultimate.db"
  db.unlink()
  if current_database == "unreadable":
    db.write_bytes(b"not a database")

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "is KEPT" in restored.stdout and "withdrew" not in restored.stdout
  assert _granted_ids(kv, data) == [1, 2]
  vault = kv.Vault(data / ".vault")
  assert [r["id"] for r in vault.list_requests()] == ["2-other"]


def test_a_grant_to_an_app_recovered_from_the_trash_since_the_backup_is_kept(tmp_path):
  """Trash and recovery rotate an app's token nonce but keep its
  installation, so its grants survive a restore of a backup from before."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: ("weather", None, "nonce-before-the-trash")})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: ("weather", None, "nonce-after-the-recovery")})
  _vault_granting(data, kv, [1, 2])

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "withdrew" not in restored.stdout
  assert _granted_ids(kv, data) == [1, 2]


def test_a_restored_database_from_before_installation_ids_proves_no_grant(tmp_path):
  """A backup from before installation ids names no installation, so no grant
  of the kept vault can be shown to be for an app it holds."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: "weather"}, install_ids=False)
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: "weather"})
  _vault_granting(data, kv, [1, 2])

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "app ids [1, 2]" in restored.stdout
  assert _granted_ids(kv, data) == []
  assert kv.Vault(data / ".vault").reveal("shared", ("owner", "")) == "dummy-restore-key-0123456789"


def test_a_restored_database_whose_apps_cannot_be_read_withdraws_every_grant(tmp_path):
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  db = sqlite3.connect(str(data / "db" / "ultimate.db"))
  db.execute("DROP TABLE IF EXISTS apps")
  db.commit()
  db.close()
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes"})
  _vault_granting(data, kv, [1])

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "withdrawing every one" in restored.stdout and "app ids [1]" in restored.stdout
  assert _granted_ids(kv, data) == []


def test_a_kept_vault_from_before_installation_ids_has_its_grants_withdrawn(tmp_path):
  """A store a server start never stamped names apps by id alone, which proves
  nothing, so the restore withdraws those grants (values kept)."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _vault_granting(data, kv, [1])
  store = json.loads((data / ".vault" / "keys.json").read_text())
  store["version"] = 2
  record = store["keys"]["shared"]
  [part] = record.pop("parts")  # version 2 held one sealed value per key
  record.update(nonce=part["nonce"], sealed=part["sealed"], multiline=part["multiline"])
  for grant in record["grants"]["apps"]:
    del grant["install"]
  (data / ".vault" / "keys.json").write_text(json.dumps(store))

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "app ids [1]" in restored.stdout
  assert _granted_ids(kv, data) == []
  assert kv.Vault(data / ".vault").reveal("shared", ("owner", "")) == "dummy-restore-key-0123456789"


def test_installs_not_held_keeps_only_installations_the_database_holds():
  restore = _load_restore()
  one, two, three = "1" * 32, "2" * 32, "3" * 32
  named = {(1, one), (2, two), (2, None), (3, three), (4, "4" * 32)}
  installs = {1: one, 2: "other", 4: "f" * 32}
  assert restore.installs_not_held(named, installs) == [(2, None), (2, two), (3, three), (4, "4" * 32)]
  assert restore.installs_not_held({(1, one)}, None) == [(1, one)]
  assert restore.installs_not_held({(1, None)}, {1: None}) == [(1, None)]


def test_a_vault_update_that_fails_changes_nothing(tmp_path, monkeypatch, capsys):
  """The kept vault is updated before the swap, in one change: when that
  fails, nothing is withdrawn and /data is not touched."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: "maps", 3: "chess"})
  _vault_granting(data, kv, [1, 2, 3])
  restore = _load_restore()
  real_open = restore.KeptVault.__init__

  def opened_on_a_full_disk(self, *args, **kwargs):
    real_open(self, *args, **kwargs)
    def disk_full(*_args):
      raise OSError(28, "No space left on device")
    monkeypatch.setattr(self._vault, "_write_store", disk_full)
  monkeypatch.setattr(restore.KeptVault, "__init__", opened_on_a_full_disk)
  monkeypatch.setattr(sys, "argv", [str(RESTORE), "--server-stopped", "--health-url", _dead_url(), bdir,
                                    "--data-dir", str(data), "--i-understand-this-overwrites", "--force"])
  with pytest.raises(SystemExit) as exited:
    restore.main()
  assert exited.value.code == 3
  assert "/data unchanged and no key erased" in capsys.readouterr().err
  assert _apps(data) == {1: "notes", 2: "maps", 3: "chess"}
  assert _granted_ids(kv, data) == [1, 2, 3]
  assert not list(data.glob(".restore-*"))
  os.close(kv.hold_single_instance_lock(data / ".vault"))


def test_a_swap_that_fails_after_the_withdrawal_keeps_every_value(tmp_path, monkeypatch, capsys):
  """A swap that fails is rolled back; the grants withdrawn before it stay
  withdrawn, which the owner can undo, and no value is lost."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: "maps"})
  _vault_granting(data, kv, [1])
  kv.Vault(data / ".vault").save_owned(2, _current_installs(data)[2], "MAPS_KEY", "dummy-maps-key-0123456789")
  restore = _load_restore()

  def swap_fails(*_args):
    raise OSError(28, "No space left on device")
  monkeypatch.setattr(restore.lib, "swap_entries_transactional", swap_fails)
  monkeypatch.setattr(sys, "argv", [str(RESTORE), "--server-stopped", "--health-url", _dead_url(), bdir,
                                    "--data-dir", str(data), "--i-understand-this-overwrites", "--force"])
  with pytest.raises(SystemExit) as exited:
    restore.main()
  assert exited.value.code == 3
  assert "stay withdrawn" in capsys.readouterr().err
  assert _apps(data) == {1: "notes", 2: "maps"}
  vault = kv.Vault(data / ".vault")
  assert vault.reveal("app2-MAPS_KEY", ("owner", "")) == "dummy-maps-key-0123456789"


def _app_saves_its_own_key(data, kv, app_id, secret):
  vault = kv.Vault(Path(data) / ".vault")
  install = _current_installs(data)[app_id]
  name = vault.save_owned(app_id, install, secret, f"dummy-{secret.lower()}-0123456789")["name"]
  return name


@pytest.mark.parametrize("why, kept", [
  ("corrupt database", True), ("recovered from the trash", True), ("id reused", False),
])
def test_a_restore_keeps_the_keys_apps_saved_for_themselves(tmp_path, why, kept):
  """A key an app saved for itself stays its own when the restored database
  holds the app's installation: when the current database is corrupt, or the
  app was recovered from the trash after the backup (its nonce rotated).
  When its id went to another app, the grant is withdrawn but no value is
  erased: the key stays, as the owner's."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: ("weather", None, "first")})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  if why == "recovered from the trash":
    _set_apps(data, {1: "notes", 2: ("weather", None, "rotated-by-the-trash")})
  elif why == "id reused":
    _set_apps(data, {1: "notes", 2: "chess"})
  shutil.rmtree(data / ".vault")
  name = _app_saves_its_own_key(data, kv, 2, "WEATHER_KEY")
  install = _current_installs(data)[2]
  if why == "corrupt database":
    (data / "db" / "ultimate.db").write_bytes(b"not a database")

  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  vault = kv.Vault(data / ".vault")
  [key] = vault.list_keys()
  assert key["name"] == name
  assert vault.reveal(name, ("owner", "")) == "dummy-weather_key-0123456789"
  if kept:
    assert "withdrew" not in restored.stdout
    assert [(g["id"], g["install"]) for g in key["grants"]["apps"]] == [(2, install)]
    assert key["origin"] == {"app": 2, "install": install, "name": "WEATHER_KEY"}
  else:
    assert "app ids [2]" in restored.stdout and "Every key is kept" in restored.stdout
    assert key["grants"]["apps"] == [] and not key.get("origin")


def test_restoring_a_backup_from_before_installation_ids_says_to_allow_apps_again(tmp_path):
  """A backup from before installation ids names no app's installation, so
  every app grant is withdrawn (values kept), and the restore says plainly,
  at its end too, that the owner lets each app use its keys again."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes"}, install_ids=False)
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes"})
  _vault_granting(data, kv, [1])
  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  out = restored.stdout
  assert out.count("from before Möbius gave each app an installation id") == 2
  assert out.rstrip().endswith("it asks again when it next needs a key.")
  assert kv.Vault(data / ".vault").list_keys()[0]["grants"]["apps"] == []


def test_a_backup_mixing_apps_with_and_without_installation_ids_names_those_without(tmp_path):
  """A backup taken after a rollback can hold app rows an older release
  wrote: only those apps' grants can't be matched, and the notice names them."""
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: "weather"})
  db = sqlite3.connect(str(data / "db" / "ultimate.db"))
  db.execute("UPDATE apps SET install_id = NULL WHERE id = 2")
  db.commit()
  db.close()
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: "weather"})
  _vault_granting(data, kv, [1, 2])
  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert restored.stdout.count("NOTE: apps [2] in this backup are from before") == 2
  assert _granted_ids(kv, data) == [1]


def test_a_current_backup_says_nothing_about_installation_ids(tmp_path):
  kv = _vault_program()
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes", 2: "weather"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  _set_apps(data, {1: "notes", 2: "chess"})
  _vault_granting(data, kv, [1, 2])
  restored = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force")
  assert "withdrew" in restored.stdout and "installation id, so" not in restored.stdout


def test_a_vault_store_the_restore_cannot_read_is_named_as_such(tmp_path):
  """A store newer than the restore's vault program is refused with that
  reason (and the way round it), not as a running vault."""
  data = _seed_data_dir(tmp_path / "data")
  _set_apps(data, {1: "notes"})
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target))
  bdir = str(next(target.glob("mobius-backup-*")))
  (data / ".vault" / "keys.json").write_text(json.dumps({"version": 99, "keys": {}}))
  refused = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force", expect=4)
  assert "can't open it" in refused.stderr and "--vault-program" in refused.stderr
  assert "Stop Möbius" not in refused.stderr and "/data unchanged" in refused.stderr
  missing = _restore(bdir, "--data-dir", str(data), "--i-understand-this-overwrites", "--force",
                     "--vault-program", str(tmp_path / "no-such-vault.py"), expect=4)
  assert "vault program not found" in missing.stderr


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


def test_restore_rejects_malicious_archive_via_production_path(tmp_path):
  """A malicious data.tar.gz behind a VALID manifest must be stopped by
  safe_extract itself (the manifest gate can't see inside the archive).
  The restore exits nonzero and the destination stays untouched."""
  data = _seed_data_dir(tmp_path / "data")
  target = tmp_path / "backups"
  _backup("--data-dir", str(data), "--target-dir", str(target),
          "--plaintext-secrets")
  bdir = Path(next(target.glob("mobius-backup-*")))

  arc = bdir / "data.tar.gz"
  _maltar(str(arc), name="lnk", ttype=tarfile.SYMTYPE,
          linkname="../../../../etc/passwd")
  mpath = bdir / "manifest.json"
  manifest = json.loads(mpath.read_text())
  for art in manifest["artifacts"]:
    if art["name"] == "data.tar.gz":
      art["bytes"] = arc.stat().st_size
      art["sha256"] = lib.sha256_file(str(arc))
  mpath.write_text(json.dumps(manifest))

  dest = tmp_path / "restored"
  dest.mkdir()
  proc = _restore(str(bdir), "--data-dir", str(dest),
                  "--i-understand-this-overwrites", expect=3)
  assert list(dest.iterdir()) == []  # nothing escaped, nothing landed
  assert "rejected" in proc.stderr.lower()


def test_rollback_failure_preserves_originals(tmp_path, monkeypatch):
  """If rolling back itself fails, the stashed original is NEVER deleted:
  RollbackError names it, and it survives in rollback_dir for hand
  recovery (exit 5 at the script layer routes through this exception)."""
  staging = tmp_path / "staging"
  data = tmp_path / "data"
  rollback = tmp_path / "rollback"
  staging.mkdir()
  data.mkdir()
  for name in ("alpha", "bravo"):
    (staging / name).write_text(f"new-{name}")
    (data / name).write_text(f"old-{name}")

  real_replace = os.replace

  def broken_replace(src, dst):
    # Forward placement of 'bravo' fails; then restoring 'alpha' from the
    # rollback dir also fails — the double-fault the invariant is for.
    if str(dst) == str(data / "bravo") and str(src).startswith(str(staging)):
      raise OSError("disk error placing bravo")
    if str(src) == str(rollback / "alpha"):
      raise OSError("disk error restoring alpha")
    return real_replace(src, dst)

  monkeypatch.setattr(lib.os, "replace", broken_replace)
  with pytest.raises(lib.RollbackError) as exc:
    lib.swap_entries_transactional(str(staging), str(data), str(rollback))
  err = exc.value
  assert err.unrecovered == ["alpha"]
  # The one true copy of alpha still exists, stashed — never deleted.
  assert (rollback / "alpha").read_text() == "old-alpha"
  assert "DO NOT delete" in str(err)


def test_server_responding_is_fail_safe(tmp_path):
  """Only a definitive nothing-is-listening reads as down; a wedged
  server that accepts but never replies must read as UP."""
  # Definitive down: bound-then-closed port.
  assert lib.server_responding(_dead_url(), timeout=0.5) is False
  # Unresolvable host: down.
  assert lib.server_responding(
    "http://mobius-no-such-host.invalid/api/health", timeout=0.5) is False
  # Accepts the socket, never replies: UP (fail-safe).
  wedged = socket.socket()
  wedged.bind(("127.0.0.1", 0))
  wedged.listen(1)
  port = wedged.getsockname()[1]
  try:
    assert lib.server_responding(
      f"http://127.0.0.1:{port}/api/health", timeout=0.5) is True
  finally:
    wedged.close()


def test_fsync_tree_raises_so_prune_aborts(tmp_path, monkeypatch):
  """A file fsync failure must propagate — the caller treats 'new backup
  not durable' as 'do not prune the old ones'."""
  root = tmp_path / "b"
  root.mkdir()
  (root / "artifact").write_bytes(b"x")

  def broken_fsync(fd):
    raise OSError("fsync failed")

  monkeypatch.setattr(lib.os, "fsync", broken_fsync)
  with pytest.raises(OSError):
    lib.fsync_tree(str(root))
