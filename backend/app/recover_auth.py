"""Isolated auth for the recovery surface.

Mirrors the small subset of `app.auth` that recovery actually needs
— password verification + signed-cookie issuance + validation — but
imports ONLY stdlib + bcrypt (the password hashing library that's
a hard dependency of the backend).

The point is decoupling: a bug introduced into `app/auth.py` by the
agent must NOT break the user's path to the recovery chat. Recovery
keeps its own tiny implementation that the agent cannot edit
(frozen via `protected-files.txt`).

Cookie format is HMAC-SHA256-signed JSON: `<b64(payload)>.<b64(sig)>`.
Deliberately not JWT — no library, no algorithm-negotiation surface,
no library-CVE blast radius.

The recovery HMAC key is derived from an independent file,
`/data/.recovery-secret`, NOT from `SECRET_KEY`. This is load-bearing:
when SECRET_KEY drifts (the documented outage mode that invalidates
all JWTs), the recovery surface is exactly when the user most needs
it. Tying it to the same key means both break together. The recovery
secret is generated once (secrets.token_hex(32)) and never rotated
by anything else. Old cookies just become invalid on first deploy
(one re-login); the UX cost is trivial compared to the availability
guarantee.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Optional

import bcrypt


COOKIE_NAME = "moebius_recover"
SESSION_TTL_SECONDS = 3600  # 1 hour, matches the existing recover.py value

# Recovery secret file lives at a fixed, volume-backed path.
# Determined once at module scope so callers don't have to thread
# DATA_DIR through; overridable for tests via _RECOVERY_SECRET_PATH.
_RECOVERY_SECRET_PATH = Path(
  os.environ.get("DATA_DIR", "/data")
) / ".recovery-secret"


def verify_password(plain: str, hashed: str) -> bool:
  """Bcrypt password check. Returns False on any error rather than
  raising — recovery surfaces shouldn't 500 on a bad cookie."""
  try:
    # bcrypt>=5 raises on >72-byte inputs (caught below → silent False), so
    # truncate to the first 72 bytes to match auth.hash_password's contract —
    # otherwise a >72-byte password can't log in on the recovery surface.
    return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
  except Exception:
    return False


def _b64encode(b: bytes) -> str:
  return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
  padding = "=" * (-len(s) % 4)
  return base64.urlsafe_b64decode(s + padding)


def _recovery_secret_bytes() -> bytes:
  """Returns the recovery HMAC key, generating it on first use.

  Read at call time (not module load) so the file can be created
  after module import without a restart — e.g. entrypoint.sh starts
  the server before /data/.recovery-secret exists on a fresh volume.

  Generates the file with chmod 600 if absent. Never rotated by
  anything except a deliberate factory-reset (which regenerates it
  on next boot). Raises RuntimeError only if both read and generate
  fail (genuine disk/permission catastrophe).
  """
  path = _RECOVERY_SECRET_PATH
  # Try reading the existing file first.
  try:
    key = path.read_text(encoding="ascii").strip()
    if key:
      return key.encode("ascii")
  except FileNotFoundError:
    pass
  except Exception as exc:
    raise RuntimeError(f"could not read recovery secret at {path}: {exc}") from exc

  # File absent — generate and persist it. Write to a UNIQUE temp (crash-safe —
  # the target is never left partial), then create the target with os.link,
  # which fails atomically if it already exists. The backend runs sync handlers
  # in AnyIO's threadpool, so on a fresh volume several requests can each mint a
  # candidate at once; os.link makes exactly one win and the losers re-read the
  # winner's key, so every concurrent caller agrees on one secret. A shared temp
  # + overwriting rename (the prior approach) let the last writer clobber a key
  # another thread had ALREADY signed a cookie with, invalidating that cookie.
  # (Mirrors the frozen floor in backend/recovery/recovery_auth.py.)
  new_key = secrets.token_hex(32)
  try:
    fd, tmpname = tempfile.mkstemp(prefix=".recovery-secret.", dir=str(path.parent))
    try:
      with os.fdopen(fd, "w") as fh:
        fh.write(new_key)
      os.chmod(tmpname, 0o600)
      try:
        os.link(tmpname, path)
        return new_key.encode("ascii")
      except FileExistsError:
        winner = path.read_text(encoding="ascii").strip()
        if winner:
          return winner.encode("ascii")
        raise RuntimeError(f"recovery secret at {path} exists but is empty")
    finally:
      try:
        os.unlink(tmpname)
      except OSError:
        pass
  except Exception as exc:
    raise RuntimeError(f"could not write recovery secret to {path}: {exc}") from exc


def create_session_token(username: str) -> str:
  """Creates an HMAC-signed token bearing `username` + expiry."""
  payload = {
    "sub": username,
    "exp": int(time.time()) + SESSION_TTL_SECONDS,
  }
  payload_b = json.dumps(
    payload, separators=(",", ":"), sort_keys=True,
  ).encode("utf-8")
  sig = hmac.new(
    _recovery_secret_bytes(), payload_b, hashlib.sha256,
  ).digest()
  return f"{_b64encode(payload_b)}.{_b64encode(sig)}"


def decode_session_token(token: Optional[str]) -> Optional[str]:
  """Returns the username if the token is valid + unexpired; else None.
  Constant-time signature comparison via hmac.compare_digest."""
  if not token or "." not in token:
    return None
  try:
    payload_part, sig_part = token.split(".", 1)
    payload_b = _b64decode(payload_part)
    sig = _b64decode(sig_part)
    try:
      key = _recovery_secret_bytes()
    except RuntimeError:
      # Can't read the recovery secret — treat the token as invalid
      # rather than raising. The caller (a route handler) would 500
      # otherwise, and the recovery surface must degrade gracefully.
      return None
    expected = hmac.new(
      key, payload_b, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(sig, expected):
      return None
    payload = json.loads(payload_b.decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
      return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
      return None
    return sub
  except Exception:
    return None
