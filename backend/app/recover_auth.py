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
no library-CVE blast radius. The secret key reused from
`SECRET_KEY` ensures parity with the existing /recover/auth cookie
so old + new auth paths can interop during the transition.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

import bcrypt


COOKIE_NAME = "moebius_recover"
SESSION_TTL_SECONDS = 3600  # 1 hour, matches the existing recover.py value


def verify_password(plain: str, hashed: str) -> bool:
  """Bcrypt password check. Returns False on any error rather than
  raising — recovery surfaces shouldn't 500 on a bad cookie."""
  try:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
  except Exception:
    return False


def _b64encode(b: bytes) -> str:
  return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
  padding = "=" * (-len(s) % 4)
  return base64.urlsafe_b64decode(s + padding)


def _secret_key_bytes() -> bytes:
  """Returns SECRET_KEY as bytes. Read at call time, not module
  load, so a key rotation propagates without a restart."""
  key = os.environ.get("SECRET_KEY", "")
  if not key:
    # Fail loudly — recovery without a signing key is broken.
    raise RuntimeError("SECRET_KEY env var is empty")
  return key.encode("utf-8")


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
    _secret_key_bytes(), payload_b, hashlib.sha256,
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
    expected = hmac.new(
      _secret_key_bytes(), payload_b, hashlib.sha256,
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
