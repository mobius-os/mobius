"""Isolated auth for the FROZEN recovery container (recoveryd).

Verbatim lift of `backend/app/recover_auth.py` — kept as a SEPARATE
copy inside the frozen `/app/recovery/` bundle so the recovery
container shares ZERO code with the agent-editable platform tree
(`/data/platform/app`). The platform's own `recover_auth.py` may be
broken or corrupted; this copy is baked root-owned + chmod a-w and
cannot be touched by the agent.

Imports ONLY stdlib + bcrypt (a hard backend dependency that lives in
root-owned site-packages, off the agent's write surface). It must NOT
import anything from `app.*` or `/data/platform`.

Cookie format is HMAC-SHA256-signed JSON: `<b64(payload)>.<b64(sig)>`.
Deliberately not JWT — no library, no algorithm-negotiation surface,
no library-CVE blast radius.

The recovery HMAC key is derived from `/data/.recovery-secret`, NOT
from `SECRET_KEY`. This is load-bearing: when SECRET_KEY drifts (the
documented outage mode that invalidates all JWTs) the recovery surface
is exactly when the user most needs it. Tying it to the same key means
both break together. The recovery secret is generated once
(secrets.token_hex(32)) and never rotated by anything else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import bcrypt


COOKIE_NAME = "moebius_recover"
SESSION_TTL_SECONDS = 1800  # 30 min — shorter than the platform's 1h
# recover surface (the destructive floor warrants a tighter window).

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
    # bcrypt>=5 raises on >72-byte inputs (caught below -> silent False),
    # so truncate to the first 72 bytes to match the platform
    # hash_password contract — otherwise a >72-byte password can't log in
    # on the recovery surface.
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

  Read at call time (not module load) so the file can be created after
  module import without a restart — e.g. a fresh volume creates it the
  first time recoveryd issues a cookie.

  Generates the file with chmod 600 if absent via a temp-file +
  atomic-rename so a crash mid-write never leaves a partial key. Never
  rotated except by a deliberate factory reset. Raises RuntimeError
  only if both read and generate fail (genuine disk catastrophe).
  """
  path = _RECOVERY_SECRET_PATH
  try:
    key = path.read_text(encoding="ascii").strip()
    if key:
      return key.encode("ascii")
  except FileNotFoundError:
    pass
  except Exception as exc:
    raise RuntimeError(
      f"could not read recovery secret at {path}: {exc}"
    ) from exc

  new_key = secrets.token_hex(32)
  try:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_key, encoding="ascii")
    tmp.chmod(0o600)
    tmp.rename(path)
  except Exception as exc:
    raise RuntimeError(
      f"could not write recovery secret to {path}: {exc}"
    ) from exc
  return new_key.encode("ascii")


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
      # rather than raising. The recovery surface must degrade
      # gracefully, never 500.
      return None
    expected = hmac.new(key, payload_b, hashlib.sha256).digest()
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
