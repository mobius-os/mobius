"""Isolated auth for the FROZEN recovery container (recoveryd).

Kept self-contained inside the frozen `/app/recovery/` bundle so the
recovery container shares ZERO code with the agent-editable platform tree
(`/data/platform/app`). This copy is baked root-owned + chmod a-w and cannot
be touched by the agent.

Imports ONLY stdlib + bcrypt (a hard backend dependency that lives in
root-owned site-packages, off the agent's write surface). It must NOT
import anything from `app.*` or `/data/platform`.

Cookie format is HMAC-SHA256-signed JSON: `<b64(payload)>.<b64(sig)>`.
Deliberately not JWT — no library, no algorithm-negotiation surface,
no library-CVE blast radius.

The recovery HMAC key is derived from `.recovery-secret` on the
recoveryd-ONLY volume (RECOVERY_LIVE_ROOT, /recovery-live), NOT from
`SECRET_KEY`. Independence from SECRET_KEY is load-bearing: when
SECRET_KEY drifts (the documented outage mode that invalidates all
JWTs) the recovery surface is exactly when the user most needs it, so
tying it to the same key would break both together. Keeping it off
`/data` is equally load-bearing: the platform `chown -R mobius:mobius
/data`s on every boot, so a secret under /data is readable by the
mobius user and a compromised platform agent could forge a recovery
cookie with it; the app container never mounts /recovery-live. The
secret is generated once (secrets.token_hex(32)) and never rotated by
anything else.
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
SESSION_TTL_SECONDS = 1800  # 30 min — shorter than the platform's 1h
# recover surface (the destructive floor warrants a tighter window).
PASSWORD_HASH_PREFIX = "bcrypt-sha256$v1$"

# Recovery secret file lives on the recoveryd-ONLY volume, off the
# shared /data volume the platform can read (see the module docstring).
# Determined once at module scope; overridable for tests via the
# RECOVERY_LIVE_ROOT env var. Kept in step with recoveryd's own
# RECOVERY_LIVE_ROOT default so both resolve to the same file.
_RECOVERY_SECRET_PATH = Path(
  os.environ.get("RECOVERY_LIVE_ROOT", "/recovery-live")
) / ".recovery-secret"


def verify_password(plain: str, hashed: str) -> bool:
  """Checks current and legacy password hashes without importing the app."""
  try:
    if hashed.startswith(PASSWORD_HASH_PREFIX):
      bcrypt_hash = hashed[len(PASSWORD_HASH_PREFIX):]
      candidate = hashlib.sha256(
        plain.encode("utf-8")
      ).hexdigest().encode("ascii")
    else:
      # Raw bcrypt is the format written by older platform releases.
      bcrypt_hash = hashed
      candidate = plain.encode("utf-8")[:72]
    return bcrypt.checkpw(candidate, bcrypt_hash.encode("ascii"))
  except (AttributeError, TypeError, ValueError):
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
    # The recoveryd-only volume mount exists in prod, but a fresh volume
    # (or the test sibling dir) may not yet — create it so the first cookie
    # can mint the key.
    os.makedirs(path.parent, exist_ok=True)
    # Write to a UNIQUE temp (crash-safe — the target is never left partial),
    # then create the target with os.link, which fails atomically if it already
    # exists. recoveryd is multi-threaded, so on a fresh volume several worker
    # threads can each mint a candidate at once; os.link makes exactly one win
    # and the losers re-read the winner's key, so every concurrent caller agrees
    # on one secret. A shared temp + overwriting rename (the prior approach) let
    # the last writer clobber a key another thread had ALREADY signed a cookie
    # with, invalidating that cookie.
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
    raise RuntimeError(
      f"could not write recovery secret to {path}: {exc}"
    ) from exc


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
