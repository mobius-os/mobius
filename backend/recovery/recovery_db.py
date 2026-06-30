"""Raw-sqlite3 owner-row access for the frozen recovery container.

Lifted from `backend/app/routes/recover.py:_owner_password_hash`. Uses
stdlib `sqlite3` ONLY — no SQLAlchemy, no `app.models`, no `app.database`
— so the recovery surface keeps working when the platform's ORM/import
chain is broken (the exact case recovery exists for).

Tier-1 floor caveat (O2): this reads the SQLite owner row directly, so
it survives a broken platform but NOT a wiped/corrupt DB. The
DB-independent `owner.json` fallback (owner sign-off O2) is the NEXT
layer and is explicitly deferred from this first floor MVP. When added,
it must be bootstrapped from the owner-creation ceremony so it can never
authenticate before an owner exists (closes the first-boot takeover).
"""

from __future__ import annotations

import os
import sqlite3
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


def owner_password_hash(username: str) -> Optional[str]:
  """Returns the owner's hashed_password for `username`, else None.

  Raw sqlite3, read-only intent. Returns None on ANY error (missing DB,
  missing table, locked file) — recovery must degrade to "login failed",
  never 500. A read-only `mode=ro` URI is used so a broken/locked DB
  can't be mutated by the auth path, and a short busy timeout avoids
  hanging the request thread on a write-locked DB.
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
  except sqlite3.Error:
    return None


def owner_exists() -> bool:
  """Returns True iff at least one Owner row exists.

  Used for the first-boot-takeover guard: until an owner exists, the
  recovery surface is read-only and every destructive route refuses.
  Returns False on any DB error (no DB yet, broken file) — fail closed,
  the safe default for "can a destructive action run".
  """
  try:
    uri = f"file:{DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as con:
      row = con.execute("SELECT 1 FROM owner LIMIT 1").fetchone()
      return row is not None
  except sqlite3.Error:
    return False


def owner_exists_for(username: str) -> bool:
  """Returns True iff an Owner row with `username` exists."""
  return owner_password_hash(username) is not None
