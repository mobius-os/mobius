"""Writes the DB-independent owner credential seed for the recovery floor.

The recovery container (`backend/recovery/`) authenticates the owner so
it can run the Tier-1 "Restore platform" action. Its default source of
truth is the SQLite owner row (`recovery_db.py`), which survives a broken
platform but NOT a wiped or corrupt database — the exact disaster the
owner most needs recovery for. This module closes that gap by mirroring
the owner's username + bcrypt hash into a small JSON file on the `/data`
volume (`/data/.recovery-owner.json`) that `recovery_db.py` falls back to
when the DB is unreadable.

Load-bearing invariants:

- The seed is written ONLY after an owner row is committed (from
  `routes/auth.py:setup()` and the idempotent boot sync below), so it can
  never authenticate before an owner exists on this volume — the
  first-boot-takeover guard the recovery floor depends on.
- It stores the bcrypt HASH, never the plaintext password. That is the
  same secret already sitting in the DB and already read by the recovery
  floor, so the seed adds no new exposure.
- It lives OUTSIDE the frozen recovery bundle on purpose: writing is a
  platform responsibility (the platform is the only process that knows a
  new owner/password), while `recovery_db.py` owns the read/fallback. The
  two agree on one trivial, stable file format; neither imports the other.

Written chmod 600 owned by the `mobius` platform user; the recovery
container runs as root and can read it. A factory reset deletes it (see
`routes/recover.py`), and because the reader only falls back when the DB
is UNREADABLE (never when it is readable-but-empty), a stale seed left by
a failed unlink is harmless as long as the DB itself is intact.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("moebius.recovery_seed")

# Fixed, volume-backed path — kept in lockstep with `recovery_db.py`'s
# `_OWNER_SEED_PATH`. Resolved from DATA_DIR the same way the recovery
# secret is (`recovery_auth.py`), overridable for tests.
OWNER_SEED_PATH = Path(
  os.environ.get("DATA_DIR", "/data")
) / ".recovery-owner.json"


def write_owner_seed(username: str, hashed_password: str) -> bool:
  """Atomically writes the owner seed. Returns True on success.

  Best-effort: a write failure is logged and swallowed (returns False) so
  a read-only or full disk can never break owner creation or boot — the
  seed is a recovery convenience, not a correctness dependency. Uses a
  temp file in the same directory + `os.replace` so a reader never sees a
  torn file and a re-seed atomically supersedes the old one.
  """
  if not username or not hashed_password:
    return False
  payload = json.dumps(
    {"username": username, "hashed_password": hashed_password},
    separators=(",", ":"), sort_keys=True,
  )
  path = OWNER_SEED_PATH
  try:
    fd, tmpname = tempfile.mkstemp(
      prefix=".recovery-owner.", dir=str(path.parent)
    )
    try:
      with os.fdopen(fd, "w") as fh:
        fh.write(payload)
      os.chmod(tmpname, 0o600)
      os.replace(tmpname, path)
      return True
    except Exception:
      try:
        os.unlink(tmpname)
      except OSError:
        pass
      raise
  except Exception as exc:
    log.warning("could not write recovery owner seed to %s: %s", path, exc)
    return False


def delete_owner_seed() -> None:
  """Removes the owner seed. Best-effort — used by factory reset so a
  wiped instance can't authenticate the prior owner on the recovery
  surface. Safe to call when the file is absent."""
  try:
    OWNER_SEED_PATH.unlink(missing_ok=True)
  except OSError as exc:
    log.warning("could not delete recovery owner seed: %s", exc)


def _read_seed_hash() -> Optional[str]:
  """Returns the hashed_password currently in the seed, or None."""
  try:
    data = json.loads(OWNER_SEED_PATH.read_text(encoding="utf-8"))
    h = data.get("hashed_password")
    return h if isinstance(h, str) and h else None
  except (OSError, ValueError):
    return None


def sync_owner_seed(db) -> bool:
  """Idempotently reconciles the seed with the current DB owner.

  Called at boot so instances that completed setup BEFORE this feature
  shipped get a seed, and so the seed tracks the owner if a
  password-change path is ever added (there is none today). Writes only
  when the seed is missing or its hash differs from the DB owner's, so a
  steady-state boot does no disk I/O. Best-effort and DB-driven: when no
  owner row exists yet it does nothing (never creates a seed without an
  owner — the takeover guard), and it never DELETES a seed here (only
  factory reset does), so a transiently-unreadable DB at boot can't drop
  a valid seed.
  """
  try:
    from app import models
    owner = db.query(models.Owner).first()
    if owner is None:
      return False
    if _read_seed_hash() == owner.hashed_password:
      return False
    return write_owner_seed(owner.username, owner.hashed_password)
  except Exception as exc:
    log.warning("recovery owner seed sync skipped: %s", exc)
    return False
