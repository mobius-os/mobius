"""Raw-sqlite3 owner-row access for the frozen recovery container.

Lifted from `backend/app/routes/recover.py:_owner_password_hash`. Uses
stdlib `sqlite3` ONLY — no SQLAlchemy, no `app.models`, no `app.database`
— so the recovery surface keeps working when the platform's ORM/import
chain is broken (the exact case recovery exists for).

DB-independent fallback (O2): the SQLite owner row survives a broken
platform but NOT a wiped/corrupt DB — the exact disaster recovery exists
for. So when (and only when) the DB is UNREADABLE, these lookups fall
back to a small credential seed the platform mirrors to
`/data/.recovery-owner.json` at owner creation and boot (written by
`backend/app/recovery_seed.py`). The fallback fires ONLY on a DB error,
never on a readable-but-empty DB: a fresh install and a completed factory
reset both leave a readable, owner-less DB (`DELETE FROM owner`, not
`DROP TABLE`), so they always read as "no owner" and a stale seed left by
a failed unlink cannot authenticate anyone while the DB is intact. The
seed is only ever written after an owner row is committed, so it can
never authenticate before an owner exists (the first-boot-takeover
guard). Kept stdlib-only + zero `app.*` imports like the rest of this
module; the seed format is a trivial JSON object agreed with
`recovery_seed.py`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

# Recovery's view of the DB path — read straight from env so it matches
# the platform without importing app.config. Same default the platform
# uses (config.py / docker-compose DATABASE_URL).
_DB_URL = os.environ.get("DATABASE_URL", "sqlite:////data/db/ultimate.db")
DB_PATH = (
  _DB_URL.removeprefix("sqlite:///")
  if _DB_URL.startswith("sqlite:")
  else _DB_URL
)

# Credential seed path — kept in lockstep with `recovery_seed.py`'s
# `OWNER_SEED_PATH`. Same DATA_DIR resolution the recovery secret uses.
_OWNER_SEED_PATH = Path(
  os.environ.get("DATA_DIR", "/data")
) / ".recovery-owner.json"


# SQLite busy/locked (error codes 5/6) mean the DB is present and healthy
# but momentarily contended — NOT the wiped/corrupt disaster the seed
# fallback is for. Falling back there could bypass the readable-empty
# precedence while a real DB is briefly unavailable (e.g. a mid-write
# checkpoint), so busy/locked is fail-closed: no seed, login just fails
# and the caller retries.
_SQLITE_BUSY_CODES = {5, 6}  # SQLITE_BUSY, SQLITE_LOCKED


def _db_is_unreadable(exc: Exception) -> bool:
  """True if `exc` means the DB is genuinely absent/corrupt (fall back to
  the seed); False for a transient busy/locked DB (fail closed)."""
  code = getattr(exc, "sqlite_errorcode", None)
  if code in _SQLITE_BUSY_CODES:
    return False
  msg = str(exc).lower()
  if "database is locked" in msg or "database is busy" in msg:
    return False
  return True


def _read_owner_seed() -> Optional[dict]:
  """Returns the parsed owner seed `{username, hashed_password}`, or None.

  Fails closed (returns None) on a missing, unreadable, or malformed
  seed — the fallback must degrade to "login failed", never raise.
  """
  try:
    data = json.loads(_OWNER_SEED_PATH.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  if not isinstance(data, dict):
    return None
  user = data.get("username")
  pw = data.get("hashed_password")
  if isinstance(user, str) and user and isinstance(pw, str) and pw:
    return {"username": user, "hashed_password": pw}
  return None


def owner_password_hash(username: str) -> Optional[str]:
  """Returns the owner's hashed_password for `username`, else None.

  Raw sqlite3, read-only intent. When the DB is readable it is
  authoritative (returns the row's hash, or None if there is no such
  owner). ONLY when the DB is UNREADABLE (missing/corrupt/locked file,
  missing table) does it fall back to the on-disk seed — so a readable
  owner-less DB (fresh install or completed factory reset) always reads
  as "no owner", never via a stale seed. A read-only `mode=ro` URI keeps
  a broken/locked DB from being mutated by the auth path, and a short
  busy timeout avoids hanging on a write-locked DB.
  """
  if not username:
    return None
  try:
    uri = f"file:{DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as con:
      row = con.execute(
        "SELECT hashed_password FROM owner WHERE username = ? LIMIT 1",
        (username,),
      ).fetchone()
      return row[0] if row else None
  except (sqlite3.Error, OSError) as exc:
    if not _db_is_unreadable(exc):
      return None  # transient busy/locked DB: fail closed, ignore the seed
    seed = _read_owner_seed()
    if seed and seed["username"] == username:
      return seed["hashed_password"]
    return None


def owner_exists() -> bool:
  """Returns True iff an owner is known to this instance.

  Used for the first-boot-takeover guard: until an owner exists, the
  recovery surface is read-only and every destructive route refuses. When
  the DB is readable it is authoritative (True iff it holds an owner row);
  ONLY when the DB is UNREADABLE does the seed decide. A fresh or
  factory-reset instance leaves a readable, owner-less DB, so it correctly
  reads False even if a stale seed lingers.
  """
  try:
    uri = f"file:{DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as con:
      row = con.execute("SELECT 1 FROM owner LIMIT 1").fetchone()
      return row is not None
  except (sqlite3.Error, OSError) as exc:
    if not _db_is_unreadable(exc):
      return False  # transient busy/locked DB: fail closed, ignore the seed
    return _read_owner_seed() is not None


def owner_exists_for(username: str) -> bool:
  """Returns True iff an owner with `username` is known to this instance
  (DB when readable, else the seed — same precedence as the hash lookup)."""
  return owner_password_hash(username) is not None
