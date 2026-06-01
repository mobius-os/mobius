"""Password hashing and JWT utilities."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet
from jose import JWTError, jwt

from app.config import get_settings


def hash_password(password: str) -> str:
  """Returns a bcrypt hash of the given password."""
  return bcrypt.hashpw(
    password.encode(), bcrypt.gensalt(rounds=12)
  ).decode()


def verify_password(plain: str, hashed: str) -> bool:
  """Returns True if the plain password matches the hash."""
  return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(
  data: dict,
  expires_delta: Optional[timedelta] = None,
  token_epoch: Optional[int] = None,
) -> str:
  """Creates and returns a signed JWT from the given payload.

  When `token_epoch` is given it is stamped as the `epoch` claim so the
  owner-resolving dependency can revoke the token by bumping the
  owner's `token_epoch` (see models.Owner.token_epoch and
  deps._resolve_owner). Callers that mint a token on the owner's behalf
  pass `owner.token_epoch`; callers that don't (none today) leave it
  absent, which the resolver reads as epoch 0.
  """
  settings = get_settings()
  payload = data.copy()
  expire = datetime.now(UTC) + (
    expires_delta or timedelta(days=30)
  )
  payload["exp"] = expire
  if token_epoch is not None:
    payload["epoch"] = token_epoch
  return jwt.encode(
    payload, settings.secret_key, algorithm="HS256"
  )


def create_app_token(
  app_id: int,
  owner_username: str,
  token_epoch: int,
  app_nonce: str | None = None,
) -> str:
  """Creates a short-lived JWT scoped to a specific mini-app.

  Carries the owner's `token_epoch` so a "sign out everywhere" revokes
  outstanding app tokens too — an app token resolves to the Owner row
  and acts on the owner's behalf, so an exfiltrated one is the same
  threat as an exfiltrated login token (only shorter-lived).

  `app_nonce` is the target app's `token_nonce`. Stamping it lets the
  resolver reject a token whose app was deleted and whose integer id was
  reused by a different app (the new app has a different nonce). Omitted
  only by callers without the row; the resolver then falls back to
  row-existence (Codex review #1).
  """
  claims = {"sub": owner_username, "scope": "app", "app_id": app_id}
  if app_nonce is not None:
    claims["app_nonce"] = app_nonce
  return create_access_token(
    claims,
    expires_delta=timedelta(hours=8),
    token_epoch=token_epoch,
  )


def decode_access_token(token: str) -> Optional[dict]:
  """Decodes a JWT and returns the payload, or None if invalid."""
  settings = get_settings()
  try:
    return jwt.decode(
      token, settings.secret_key, algorithms=["HS256"]
    )
  except JWTError:
    return None


def _fernet() -> Fernet:
  """Derives a Fernet instance from SECRET_KEY via SHA-256."""
  raw = hashlib.sha256(get_settings().secret_key.encode()).digest()
  key = base64.urlsafe_b64encode(raw)
  return Fernet(key)


def encrypt_api_key(plaintext: str) -> str:
  """Returns the Fernet-encrypted API key as a URL-safe string."""
  return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
  """Decrypts a Fernet-encrypted API key and returns the plaintext."""
  return _fernet().decrypt(ciphertext.encode()).decode()
