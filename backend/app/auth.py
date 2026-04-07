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
) -> str:
  """Creates and returns a signed JWT from the given payload."""
  settings = get_settings()
  payload = data.copy()
  expire = datetime.now(UTC) + (
    expires_delta or timedelta(days=30)
  )
  payload["exp"] = expire
  return jwt.encode(
    payload, settings.secret_key, algorithm="HS256"
  )


def create_app_token(app_id: int, owner_username: str) -> str:
  """Creates a short-lived JWT scoped to a specific mini-app."""
  return create_access_token(
    {"sub": owner_username, "scope": "app", "app_id": app_id},
    expires_delta=timedelta(hours=8),
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
