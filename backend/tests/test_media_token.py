# backend/tests/test_media_token.py
"""Tests for media-token minting and the hardened serve routes.

Covers the security contract introduced to prevent 30-day owner JWTs
from appearing in ?token= query params on image-serving routes:

 1. POST /api/chats/{id}/media-token mints a short-lived, chat-scoped token.
 2. Serve routes accept media tokens on ?token= and owner JWTs on the header.
 3. Owner JWTs are explicitly rejected on ?token= (the key security fix).
 4. Media tokens for the wrong chat are rejected.
 5. Expired media tokens are rejected.
 6. App-scoped tokens are rejected on both paths.
"""
import io
from datetime import UTC, datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upload_file(client, auth, chat, content=b"data", name="img.png"):
  r = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", (name, io.BytesIO(content), "image/png"))],
    headers=auth,
  )
  assert r.status_code == 200
  return r.json()[0]["name"]


# ---------------------------------------------------------------------------
# Media token minting
# ---------------------------------------------------------------------------

def test_issue_media_token_returns_token_and_ttl(client, auth, chat):
  """POST /api/chats/{id}/media-token returns a token and expires_in=900."""
  r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert "token" in body
  assert body["expires_in"] == 900


def test_issue_media_token_payload_claims(client, auth, chat):
  """Minted token has scope='media' and media_chat=<chat_id>."""
  from app.auth import decode_access_token
  r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  payload = decode_access_token(r.json()["token"])
  assert payload["scope"] == "media"
  assert payload["media_chat"] == chat.id


def test_issue_media_token_expiry_is_15_min(client, auth, chat):
  """Media token expires within ~15 minutes of mint time."""
  from app.auth import decode_access_token
  before = datetime.now(UTC)
  r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  payload = decode_access_token(r.json()["token"])
  exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
  assert exp > before + timedelta(minutes=14)
  assert exp < before + timedelta(minutes=16)


def test_issue_media_token_requires_owner_auth(client, chat):
  """Media token endpoint requires authentication."""
  r = client.post(f"/api/chats/{chat.id}/media-token")
  assert r.status_code == 401


def test_issue_media_token_rejects_cross_site(client, auth, chat):
  """Media token endpoint applies the CSRF dependency."""
  r = client.post(
    f"/api/chats/{chat.id}/media-token",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert r.status_code == 403


def test_issue_media_token_rejects_nonexistent_chat(client, auth):
  """Media token endpoint returns 404 for a chat that doesn't exist."""
  import uuid
  r = client.post(
    f"/api/chats/{uuid.uuid4()}/media-token",
    headers=auth,
  )
  assert r.status_code == 404


# ---------------------------------------------------------------------------
# Upload serve route: media token on ?token=
# ---------------------------------------------------------------------------

def test_serve_upload_with_media_token(client, auth, chat):
  """GET /uploads/{file}?token=<media-token> returns 200."""
  name = _upload_file(client, auth, chat, b"hello")
  token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = token_r.json()["token"]
  r = client.get(
    f"/api/chats/{chat.id}/uploads/{name}",
    params={"token": media_token},
  )
  assert r.status_code == 200


def test_serve_upload_rejects_owner_jwt_on_query_param(client, auth, chat):
  """GET /uploads/{file}?token=<owner-jwt> must return 403.

  This is the core security fix: the 30-day owner JWT must not leak into
  access logs, browser history, or Referer headers via ?token=.
  """
  from app.auth import create_access_token
  name = _upload_file(client, auth, chat)
  owner_jwt = create_access_token({"sub": "test"})
  r = client.get(
    f"/api/chats/{chat.id}/uploads/{name}",
    params={"token": owner_jwt},
  )
  assert r.status_code == 403
  # The error message should mention media token or owner JWT restriction.
  assert "media token" in r.json()["detail"].lower() or "owner" in r.json()["detail"].lower()


def test_serve_upload_accepts_owner_jwt_on_header(client, auth, chat):
  """GET /uploads/{file} with Authorization: Bearer <owner-jwt> returns 200."""
  name = _upload_file(client, auth, chat, b"hello-header")
  r = client.get(f"/api/chats/{chat.id}/uploads/{name}", headers=auth)
  assert r.status_code == 200


def test_serve_upload_rejects_media_token_for_wrong_chat(client, auth, chat, db):
  """A media token minted for chat A must not serve chat B's uploads."""
  import uuid
  from app import models
  # Create a second chat.
  chat_b = models.Chat(id=str(uuid.uuid4()), title="Chat B", messages=[])
  db.add(chat_b)
  db.commit()

  name = _upload_file(client, auth, chat, b"secret")
  # Mint a token for chat_b but use it on chat (different chat_id).
  token_r = client.post(f"/api/chats/{chat_b.id}/media-token", headers=auth)
  media_token = token_r.json()["token"]
  r = client.get(
    f"/api/chats/{chat.id}/uploads/{name}",
    params={"token": media_token},
  )
  assert r.status_code == 403


def test_serve_upload_rejects_app_token_on_query_param(client, auth, chat, db):
  """An app-scoped token must be rejected on ?token= for uploads."""
  from app import models
  from app.auth import create_app_token
  # Create a minimal App row to satisfy _enforce_app_scope's existence check.
  from app import models as _m
  app_row = _m.App(
    name="Test", description="test", slug="test-app-tok",
    jsx_source="export default () => null",
    token_nonce="nonce1",
  )
  db.add(app_row)
  db.commit()
  db.refresh(app_row)

  name = _upload_file(client, auth, chat)
  from app.database import SessionLocal
  _db = SessionLocal()
  owner = _db.query(_m.Owner).filter(_m.Owner.username == "test").first()
  _db.close()
  app_token = create_app_token(app_row.id, "test", owner.token_epoch, "nonce1")
  r = client.get(
    f"/api/chats/{chat.id}/uploads/{name}",
    params={"token": app_token},
  )
  assert r.status_code == 403


def test_serve_upload_rejects_expired_media_token(client, auth, chat):
  """An expired media token must return 401."""
  from app.auth import create_access_token
  name = _upload_file(client, auth, chat, b"exp-data")
  expired = create_access_token(
    {"sub": "test", "scope": "media", "media_chat": chat.id},
    expires_delta=timedelta(seconds=-1),
  )
  r = client.get(
    f"/api/chats/{chat.id}/uploads/{name}",
    params={"token": expired},
  )
  assert r.status_code == 401


# ---------------------------------------------------------------------------
# Chat media serve route: media token on ?token=
# ---------------------------------------------------------------------------

def _setup_media_image(client, auth, chat):
  """Writes a fake image directly to the chat media directory."""
  import pathlib
  from app.config import get_settings
  media_dir = pathlib.Path(get_settings().data_dir) / "chats" / chat.id / "media"
  media_dir.mkdir(parents=True, exist_ok=True)
  fname = "test-media.png"
  (media_dir / fname).write_bytes(b"\x89PNG\r\n")
  return fname


def test_serve_media_with_media_token(client, auth, chat):
  """GET /media/{file}?token=<media-token> returns 200."""
  fname = _setup_media_image(client, auth, chat)
  token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = token_r.json()["token"]
  r = client.get(
    f"/api/chats/{chat.id}/media/{fname}",
    params={"token": media_token},
  )
  assert r.status_code == 200


def test_serve_media_rejects_owner_jwt_on_query_param(client, auth, chat):
  """GET /media/{file}?token=<owner-jwt> must return 403."""
  from app.auth import create_access_token
  fname = _setup_media_image(client, auth, chat)
  owner_jwt = create_access_token({"sub": "test"})
  r = client.get(
    f"/api/chats/{chat.id}/media/{fname}",
    params={"token": owner_jwt},
  )
  assert r.status_code == 403


def test_serve_media_accepts_owner_jwt_on_header(client, auth, chat):
  """GET /media/{file} with Authorization: Bearer returns 200."""
  fname = _setup_media_image(client, auth, chat)
  r = client.get(
    f"/api/chats/{chat.id}/media/{fname}",
    headers=auth,
  )
  assert r.status_code == 200


def test_serve_media_rejects_media_token_for_wrong_chat(client, auth, chat, db):
  """A media token for the wrong chat is rejected."""
  import uuid
  from app import models
  chat_b = models.Chat(id=str(uuid.uuid4()), title="Chat B", messages=[])
  db.add(chat_b)
  db.commit()

  fname = _setup_media_image(client, auth, chat)
  token_r = client.post(f"/api/chats/{chat_b.id}/media-token", headers=auth)
  media_token = token_r.json()["token"]
  r = client.get(
    f"/api/chats/{chat.id}/media/{fname}",
    params={"token": media_token},
  )
  assert r.status_code == 403
