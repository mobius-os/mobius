"""Password hashing and JWT utilities."""

from datetime import UTC, datetime, timedelta
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings


def hash_password(password: str) -> str:
  """Returns a bcrypt hash of the given password."""
  # bcrypt only ever uses the first 72 bytes of the input; bcrypt>=5 raises
  # ValueError on longer inputs instead of silently truncating, so truncate
  # explicitly. Hashes produced by bcrypt 4.x (silent truncation) or by this
  # code still verify — both feed bcrypt the same 72-byte prefix — and a
  # >72-byte password no longer crashes.
  return bcrypt.hashpw(
    password.encode()[:72], bcrypt.gensalt(rounds=12)
  ).decode()


def verify_password(plain: str, hashed: str) -> bool:
  """Returns True if the plain password matches the hash."""
  # Match hash_password's 72-byte truncation (see there): bcrypt>=5 raises on
  # >72-byte inputs, and the stored hash was computed from the first 72 bytes.
  return bcrypt.checkpw(plain.encode()[:72], hashed.encode())


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
  *,
  expires_delta: timedelta = timedelta(hours=8),
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
  row-existence.
  """
  claims = {"sub": owner_username, "scope": "app", "app_id": app_id}
  if app_nonce is not None:
    claims["app_nonce"] = app_nonce
  return create_access_token(
    claims,
    expires_delta=expires_delta,
    token_epoch=token_epoch,
  )


def create_media_token(chat_id: str, owner_username: str, token_epoch: int) -> str:
  """Creates a short-lived JWT scoped to uploads and media for one chat.

  The token's `scope` is "media" and `media_chat` carries the chat_id so the
  serve routes can verify the token is for the exact resource being requested.
  TTL is 15 minutes — long enough for a page session to render all images,
  short enough that a URL leaking into logs expires quickly.

  Signed with the same SECRET_KEY as all other tokens; revocable via
  token_epoch so a "sign out everywhere" invalidates outstanding media tokens.
  """
  return create_access_token(
    {"sub": owner_username, "scope": "media", "media_chat": chat_id},
    expires_delta=timedelta(minutes=15),
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
