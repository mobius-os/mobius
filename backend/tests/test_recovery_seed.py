"""Tests for the DB-independent recovery credential seed (O2).

Covers both halves and, crucially, their agreement across the
platform/recovery isolation boundary: the platform writes the seed
(`app.recovery_seed`) and the frozen recovery bundle reads it
(`recovery_db`), with NO shared import — only a shared file format.

The security-critical property under test is the precedence rule: the
recovery reader falls back to the seed ONLY when the DB is UNREADABLE,
never when it is readable-but-owner-less. So a fresh install and a
completed factory reset (both leave a readable, owner-less DB) always
read as "no owner" even if a stale seed lingers, while a wiped/corrupt
DB falls back to the seed so the owner can still authenticate.
"""

import importlib
import os
import sqlite3
import sys
from pathlib import Path

import bcrypt
import pytest

_RECOVERY_DIR = Path(__file__).resolve().parents[1] / "recovery"
if str(_RECOVERY_DIR) not in sys.path:
  sys.path.insert(0, str(_RECOVERY_DIR))

_USER = "owner"
_PLAINTEXT = "correct horse battery staple"
_HASH = bcrypt.hashpw(_PLAINTEXT.encode()[:72], bcrypt.gensalt(rounds=4)).decode()


def _make_owner_table(db_path: Path, *, rows=True):
  """Creates a minimal owner table matching what recovery_db queries."""
  with sqlite3.connect(db_path) as con:
    con.execute(
      "CREATE TABLE owner (id INTEGER PRIMARY KEY, "
      "username TEXT, hashed_password TEXT)"
    )
    if rows:
      con.execute(
        "INSERT INTO owner (username, hashed_password) VALUES (?, ?)",
        (_USER, _HASH),
      )
    con.commit()


@pytest.fixture()
def env(monkeypatch, tmp_path):
  """Isolated DATA_DIR shared by the platform writer and the frozen
  recovery reader, with recovery_db freshly imported against it."""
  data_dir = tmp_path
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  seed_path = data_dir / ".recovery-owner.json"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  for mod in ("recovery_auth", "recovery_db"):
    sys.modules.pop(mod, None)
  recovery_db = importlib.import_module("recovery_db")
  from app import recovery_seed
  # Point the platform writer at the same seed path the freshly-imported
  # recovery reader resolved from DATA_DIR.
  monkeypatch.setattr(recovery_seed, "OWNER_SEED_PATH", seed_path)
  assert recovery_db._OWNER_SEED_PATH == seed_path
  return {
    "data_dir": data_dir,
    "db_path": db_path,
    "seed_path": seed_path,
    "db": recovery_db,
    "seed": recovery_seed,
  }


# --- write side -----------------------------------------------------------

def test_write_owner_seed_content_and_perms(env):
  assert env["seed"].write_owner_seed(_USER, _HASH) is True
  import json
  data = json.loads(env["seed_path"].read_text())
  assert data == {"username": _USER, "hashed_password": _HASH}
  assert oct(os.stat(env["seed_path"]).st_mode & 0o777) == "0o600"


def test_write_owner_seed_atomic_overwrite(env):
  env["seed"].write_owner_seed(_USER, "hash-one")
  env["seed"].write_owner_seed(_USER, "hash-two")
  import json
  assert json.loads(env["seed_path"].read_text())["hashed_password"] == "hash-two"


def test_write_owner_seed_rejects_empty(env):
  assert env["seed"].write_owner_seed("", _HASH) is False
  assert env["seed"].write_owner_seed(_USER, "") is False
  assert not env["seed_path"].exists()


def test_write_owner_seed_best_effort_on_bad_dir(env, monkeypatch):
  monkeypatch.setattr(
    env["seed"], "OWNER_SEED_PATH", Path("/nonexistent-dir/x/seed.json")
  )
  # A write failure must be swallowed (returns False), never raised.
  assert env["seed"].write_owner_seed(_USER, _HASH) is False


def test_delete_owner_seed(env):
  env["seed"].write_owner_seed(_USER, _HASH)
  assert env["seed_path"].exists()
  env["seed"].delete_owner_seed()
  assert not env["seed_path"].exists()
  # Idempotent — deleting an absent seed is a no-op, not an error.
  env["seed"].delete_owner_seed()


# --- boot sync ------------------------------------------------------------

class _FakeOwner:
  def __init__(self, username, hashed_password):
    self.username = username
    self.hashed_password = hashed_password


class _FakeQuery:
  def __init__(self, owner):
    self._owner = owner

  def first(self):
    return self._owner


class _FakeDB:
  def __init__(self, owner):
    self._owner = owner

  def query(self, _model):
    return _FakeQuery(self._owner)


def test_sync_writes_when_missing(env):
  # Backfill: DB owner exists, seed absent -> written.
  assert env["seed"].sync_owner_seed(_FakeDB(_FakeOwner(_USER, _HASH))) is True
  assert env["seed_path"].exists()


def test_sync_idempotent_when_unchanged(env):
  db = _FakeDB(_FakeOwner(_USER, _HASH))
  assert env["seed"].sync_owner_seed(db) is True
  # Second run with an identical hash writes nothing.
  assert env["seed"].sync_owner_seed(db) is False


def test_sync_updates_on_hash_change(env):
  env["seed"].sync_owner_seed(_FakeDB(_FakeOwner(_USER, "old-hash")))
  assert env["seed"].sync_owner_seed(_FakeDB(_FakeOwner(_USER, "new-hash"))) is True
  import json
  assert json.loads(env["seed_path"].read_text())["hashed_password"] == "new-hash"


def test_sync_no_owner_never_seeds(env):
  # Takeover guard: no owner row -> never create a seed.
  assert env["seed"].sync_owner_seed(_FakeDB(None)) is False
  assert not env["seed_path"].exists()


# --- read side: precedence (the security-critical rule) -------------------

def test_db_readable_with_owner_is_authoritative(env):
  _make_owner_table(env["db_path"], rows=True)
  assert env["db"].owner_password_hash(_USER) == _HASH
  assert env["db"].owner_exists() is True


def test_db_readable_but_empty_ignores_stale_seed(env):
  # THE key property: a readable owner-less DB (fresh / factory-reset)
  # must read as "no owner" even with a seed on disk.
  _make_owner_table(env["db_path"], rows=False)
  env["seed"].write_owner_seed(_USER, _HASH)
  assert env["db"].owner_password_hash(_USER) is None
  assert env["db"].owner_exists() is False


def test_wiped_db_falls_back_to_seed(env):
  # THE feature: DB file gone, seed present -> authenticate via seed.
  assert not env["db_path"].exists()
  env["seed"].write_owner_seed(_USER, _HASH)
  assert env["db"].owner_password_hash(_USER) == _HASH
  assert env["db"].owner_exists() is True
  assert env["db"].owner_exists_for(_USER) is True


def test_corrupt_db_falls_back_to_seed(env):
  env["db_path"].write_bytes(b"this is not a sqlite database at all")
  env["seed"].write_owner_seed(_USER, _HASH)
  assert env["db"].owner_password_hash(_USER) == _HASH
  assert env["db"].owner_exists() is True


def test_zero_byte_db_falls_back_to_seed(env):
  # A 0-byte file is a valid empty SQLite DB with no owner table ->
  # "no such table" -> unreadable -> fallback.
  env["db_path"].write_bytes(b"")
  env["seed"].write_owner_seed(_USER, _HASH)
  assert env["db"].owner_password_hash(_USER) == _HASH


def test_wiped_db_no_seed_fails_closed(env):
  assert not env["db_path"].exists()
  assert env["db"].owner_password_hash(_USER) is None
  assert env["db"].owner_exists() is False


def test_seed_username_mismatch(env):
  # Seed is for a different owner -> no auth for the requested username.
  assert not env["db_path"].exists()
  env["seed"].write_owner_seed("someone-else", _HASH)
  assert env["db"].owner_password_hash(_USER) is None


def test_malformed_seed_fails_closed(env):
  assert not env["db_path"].exists()
  env["seed_path"].write_text("{ not valid json")
  assert env["db"].owner_password_hash(_USER) is None
  assert env["db"].owner_exists() is False


# --- end-to-end: the full recovery auth path over a wiped DB --------------

def test_wiped_db_full_auth_path(env):
  import recovery_auth
  env["seed"].write_owner_seed(_USER, _HASH)
  candidate = env["db"].owner_password_hash(_USER)
  assert candidate is not None
  assert recovery_auth.verify_password(_PLAINTEXT, candidate) is True
  assert recovery_auth.verify_password("wrong password", candidate) is False


# --- busy/locked DB must fail closed, not fall back to the seed -----------

def test_db_is_unreadable_classifier(env):
  cls = env["db"]._db_is_unreadable
  # Genuinely unreadable -> fall back (True).
  assert cls(sqlite3.OperationalError("unable to open database file")) is True
  assert cls(sqlite3.DatabaseError("file is not a database")) is True
  assert cls(sqlite3.OperationalError("no such table: owner")) is True
  assert cls(OSError("permission denied")) is True
  # Transient contention -> fail closed (False).
  assert cls(sqlite3.OperationalError("database is locked")) is False
  assert cls(sqlite3.OperationalError("database is busy")) is False


def test_locked_db_fails_closed_ignores_seed(env):
  # A readable-but-locked DB is healthy, just contended — it must NOT fall
  # back to the seed (that would bypass the readable-empty precedence while
  # a real DB is briefly unavailable). Hold an EXCLUSIVE lock and confirm
  # the lookup returns "login failed", not the seed's hash.
  _make_owner_table(env["db_path"], rows=False)
  env["seed"].write_owner_seed(_USER, _HASH)
  holder = sqlite3.connect(env["db_path"], isolation_level=None)
  try:
    holder.execute("BEGIN EXCLUSIVE")
    assert env["db"].owner_password_hash(_USER) is None
    assert env["db"].owner_exists() is False
  finally:
    holder.execute("ROLLBACK")
    holder.close()
