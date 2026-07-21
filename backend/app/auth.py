"""Password hashing and JWT utilities."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings


# bcrypt ignores bytes after the first 72. Pre-hashing new passwords makes the
# full UTF-8 input significant while retaining bcrypt's salt and work factor.
# The prefix makes the format self-describing so hashes created by older Mobius
# versions can still be verified and upgraded after a successful login.
PASSWORD_HASH_PREFIX = "bcrypt-sha256$v1$"


def _password_digest(password: str) -> bytes:
  """Returns a fixed-width bcrypt input derived from the full password."""
  return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")


def hash_password(password: str) -> str:
  """Returns a versioned bcrypt hash covering the full password."""
  bcrypt_hash = bcrypt.hashpw(
    _password_digest(password), bcrypt.gensalt(rounds=12)
  ).decode("ascii")
  return PASSWORD_HASH_PREFIX + bcrypt_hash


def verify_password(plain: str, hashed: str) -> bool:
  """Verifies current hashes and legacy raw-bcrypt hashes."""
  try:
    if hashed.startswith(PASSWORD_HASH_PREFIX):
      bcrypt_hash = hashed[len(PASSWORD_HASH_PREFIX):]
      candidate = _password_digest(plain)
    else:
      # Older Mobius releases passed the raw first 72 bytes to bcrypt. Keep
      # this path indefinitely so existing local installations can sign in.
      bcrypt_hash = hashed
      candidate = plain.encode("utf-8")[:72]
    return bcrypt.checkpw(candidate, bcrypt_hash.encode("ascii"))
  except (AttributeError, TypeError, ValueError):
    return False


def password_needs_rehash(hashed: str) -> bool:
  """Returns True for a legacy hash that should migrate after login."""
  return not hashed.startswith(PASSWORD_HASH_PREFIX)


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


def create_chat_embed_media_token(
  *,
  owner_username: str,
  token_epoch: int,
  app_id: int,
  app_nonce: str,
  chat_id: str,
  session_id: str,
  expires_delta: timedelta = timedelta(minutes=15),
) -> str:
  """Create a URL-safe media token chained to one live embed session."""
  return create_access_token(
    {
      "sub": owner_username,
      "scope": "chat_embed_media",
      "app_id": app_id,
      "app_nonce": app_nonce,
      "media_chat": chat_id,
      "embed_session": session_id,
    },
    expires_delta=expires_delta,
    token_epoch=token_epoch,
  )


def create_chat_embed_session_token(
  *,
  owner_username: str,
  token_epoch: int,
  app_id: int,
  app_nonce: str,
  chat_id: str,
  instance_id: str,
  session_id: str,
  role: str,
  operations: list[str],
  expires_delta: timedelta = timedelta(minutes=15),
) -> str:
  """Mint the in-memory bearer used by one authorized chat embed.

  The one-time bootstrap grant is a random opaque secret stored hashed in the
  database; after exchange, this signed session is what ChatView presents on
  API requests. It is deliberately narrower than an app token: exact app
  installation, chat, embed instance, role and operation set are claims, and
  the dependency layer re-checks the live grant/app/chat rows on every use.
  """
  return create_access_token(
    {
      "sub": owner_username,
      "scope": "chat_embed",
      "app_id": app_id,
      "app_nonce": app_nonce,
      "chat_id": chat_id,
      "embed_instance": instance_id,
      "embed_session": session_id,
      "embed_role": role,
      "embed_ops": operations,
    },
    expires_delta=expires_delta,
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
