"""Common federation — the Möbius-to-Möbius social layer (protocol v0).

Every Möbius instance is one user's server. This router gives an instance
three federated capabilities, all instance-to-instance over HTTPS with
Ed25519-signed envelopes and no third-party storage:

1. **Identity** — an actor card (`GET /actor`) publishing federation keys.
   Private participants expose only those keys; joining the community also
   publishes the owner's profile card. Peers verify every envelope against
   the claimed sender's fetched (and cached) actor card.
2. **Direct messages** — a signed envelope POSTed straight to the recipient
   instance's `/inbox`. A first inbound conversation is stored as a quiet
   message request until its owner accepts; an explicit first outbound message
   establishes consent. Each side stores only its own copy (in the Common
   mini-app's per-app storage), so a conversation lives exclusively on the two
   participants' servers.
3. **Community host role** — any instance can host the shared, public parts:
   an opt-in user directory (search) and a message board. Peers register and
   post with the same signed-envelope scheme. Which host to use is the
   owner's choice (default: their own instance).

Public peer surface (no owner auth; envelope signatures are the authority):
  GET  /api/common/actor        federation keys; joined public profile card
  GET  /api/common/avatar       instance profile avatar
  POST /api/common/inbox        deliver a signed DM to this instance's owner
  GET  /api/common/directory    search users registered with this host
  POST /api/common/directory    signed directory registration
  GET  /api/common/board        public board feed of this host
  GET  /api/common/board/media/{post_id}  hosted board image
  POST /api/common/board        signed board post
  POST /api/common/board/reply  signed reply to a hosted board post
  GET  /api/common/board/{post_id}/replies  hosted post replies

Owner surface (owner JWT or the Common app's scoped token):
  GET  /api/common/me           own profile (creates the keypair lazily)
  PUT  /api/common/me           update profile; re-registers with community host
  POST /api/common/send         sign + deliver a DM; store own copy
  POST /api/common/requests/dm/{host}/{decision}  accept/decline/block request
  POST /api/common/publish      sign + submit a board post to the community host
  GET  /api/common/board-media/{post_id}  local/cached community board image
  POST /api/common/reply        sign + submit a board reply to the community host
  GET  /api/common/feed         community host's board (local read when self)
  GET  /api/common/people       community host directory search
  GET  /api/common/peer/{host}  a peer's actor card (profile view)
  GET  /api/common/peer-avatar/{host}  a peer's cached profile avatar

Server-owned state lives under `<data_dir>/common/` (identity + community-host
records). Conversation data lives in the Common mini-app's per-app storage so
the app UI reads it through `window.mobius.storage`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import fs_locks, models, push
from app.common_protocol import (
  ATTACHMENT_MIME_EXT as _ATTACHMENT_MIME_EXT,
  MAX_ATTACHMENT_BYTES, MAX_AVATAR_BYTES, MAX_BIO_CHARS,
  MAX_ENVELOPE_BYTES, MAX_NAME_CHARS, MAX_REPLY_TEXT_CHARS,
  OUTBOUND_TIMEOUT_S, PROTOCOL, ActorVerifier,
  canonical as _canonical, peer_base_url as _peer_base_url,
  read_envelope as _read_envelope, sign as _sign,
  valid_host as _valid_host, valid_id as _valid_id,
  validate_attachment as _validate_attachment,
  validate_reply_to as _validate_reply_to,
  validate_text_or_attachment as _validate_text_or_attachment,
)
from app.common_public import (
  BOARD_PAGE_LIMIT, CommonPublicStore, create_public_router,
)
from app.common_transport import federation_request
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_principal, require_nondelegated_owner_control,
)
from app.routes import identity as identity_routes
from app.storage_io import atomic_write

router = APIRouter(prefix="/api/common", tags=["common"])

APP_SLUG = "common"
PEER_AVATAR_CACHE_TTL_S = 24 * 3600
BOARD_MEDIA_CACHE_TTL_S = 24 * 3600
REQUEST_STATES = {"pending", "accepted", "declined", "blocked"}


# ── paths ────────────────────────────────────────────────────────────────────

def _data_dir() -> str:
  return get_settings().data_dir


_public_store = CommonPublicStore(_data_dir)
_actor_verifier = ActorVerifier(_data_dir)

# Owner/personal code below shares the public service's canonical paths and
# mutations instead of maintaining parallel storage logic.
_common_dir = _public_store.common_dir
_board_media_dir = _public_store.board_media_dir
_find_image = _public_store.find_image
_serve_image = _public_store.serve_image
_read_board = _public_store.read_board
_store_board_post = _public_store.store_post
_toggle_board_like = _public_store.toggle_like
_add_board_reply = _public_store.add_reply
_fetch_actor = _actor_verifier.fetch_actor
_verify_peer_envelope = _actor_verifier.verify_envelope


def _identity_path() -> Path:
  # Unlike the other Common paths, callers use this to distinguish an
  # untouched installation. Merely probing /actor must not materialize state.
  return Path(get_settings().data_dir) / "common" / "identity.json"


def _avatar_path() -> Path:
  return _common_dir() / "avatar.png"


def _peers_dir() -> Path:
  return _actor_verifier.peers_dir()


def _peer_avatar_path(host: str) -> Path:
  safe = re.sub(r"[^a-z0-9.-]", "_", host)
  path = _peers_dir() / "avatars"
  path.mkdir(parents=True, exist_ok=True)
  return path / f"{safe}.png"


def _peer_board_media_dir() -> Path:
  path = _peers_dir() / "board-media"
  path.mkdir(parents=True, exist_ok=True)
  return path


def _peer_board_media_name(host: str, post_id: str) -> str:
  safe_host = re.sub(r"[^a-z0-9.-]", "_", host)
  return f"{safe_host}-{post_id}"


COMMUNITY_HOST = "www.mobius.you"
DEFAULT_COMMUNITY_HOST = COMMUNITY_HOST


def _own_host() -> str:
  return get_settings().domain


def _canonical_community_host(host: str | None = None) -> str:
  """Return the canonical global community host."""
  if _own_host() in ("testserver", "localhost", "127.0.0.1", "mobius.test"):
    return host or _own_host()
  return COMMUNITY_HOST


async def _download_avatar(url: str) -> bytes:
  """Fetch one image while bounding the response body before buffering it."""
  response = await federation_request(
    "GET", url, max_response_bytes=MAX_AVATAR_BYTES,
    response_format="binary", timeout_seconds=OUTBOUND_TIMEOUT_S,
  )
  response.raise_for_status()
  content_type = response.headers.get("content-type", "").split(";", 1)[0]
  if not content_type.strip().lower().startswith("image/"):
    raise ValueError("Avatar response is not an image.")
  if not response.content:
    raise ValueError("Avatar response is empty.")
  return response.content


async def _download_board_media(url: str) -> tuple[str, bytes]:
  """Fetch one hosted board image without buffering more than the wire cap."""
  response = await federation_request(
    "GET", url, max_response_bytes=MAX_ATTACHMENT_BYTES,
    response_format="binary", timeout_seconds=OUTBOUND_TIMEOUT_S,
  )
  response.raise_for_status()
  mime = (
    response.headers.get("content-type", "")
    .split(";", 1)[0].strip().lower()
  )
  if mime not in _ATTACHMENT_MIME_EXT:
    raise ValueError("Board media response is not a supported image.")
  if not response.content:
    raise ValueError("Board media response is empty.")
  return mime, response.content


def _dm_key(shared: bytes) -> bytes:
  from cryptography.hazmat.primitives import hashes
  from cryptography.hazmat.primitives.kdf.hkdf import HKDF
  return HKDF(
    algorithm=hashes.SHA256(),
    length=32,
    salt=b"",
    info=b"common/0 dm v1",
  ).derive(shared)


def _seal_dm(
  message_id: str, recipient_key_b64: str, *, text: str,
  attachment: dict | None, reply_to: dict | None,
) -> dict:
  """Seal one canonical DM payload to a peer's static X25519 key."""
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
  )
  from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
  recipient = X25519PublicKey.from_public_bytes(
    base64.b64decode(recipient_key_b64, validate=True)
  )
  ephemeral = X25519PrivateKey.generate()
  key = _dm_key(ephemeral.exchange(recipient))
  nonce = os.urandom(12)
  plaintext = _canonical({
    "text": text, "attachment": attachment, "reply_to": reply_to,
  })
  ciphertext = ChaCha20Poly1305(key).encrypt(
    nonce, plaintext, message_id.encode("utf-8")
  )
  ephemeral_public = ephemeral.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
  )
  return {
    "v": 1,
    "epk_b64": base64.b64encode(ephemeral_public).decode(),
    "nonce_b64": base64.b64encode(nonce).decode(),
    "ct_b64": base64.b64encode(ciphertext).decode(),
  }


def _open_dm(message_id: str, enc: Any, private_key_b64: str) -> dict:
  """Open one sealed DM payload or return the protocol's generic failure."""
  from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
  )
  from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
  try:
    if not isinstance(enc, dict) or set(enc) != {
      "v", "epk_b64", "nonce_b64", "ct_b64",
    } or enc.get("v") != 1:
      raise ValueError("invalid sealed payload")
    if not all(
      isinstance(enc.get(field), str)
      for field in ("epk_b64", "nonce_b64", "ct_b64")
    ):
      raise ValueError("invalid sealed payload")
    ephemeral_bytes = base64.b64decode(enc["epk_b64"], validate=True)
    nonce = base64.b64decode(enc["nonce_b64"], validate=True)
    ciphertext = base64.b64decode(enc["ct_b64"], validate=True)
    if len(ephemeral_bytes) != 32 or len(nonce) != 12:
      raise ValueError("invalid sealed payload")
    ephemeral = X25519PublicKey.from_public_bytes(ephemeral_bytes)
    private = X25519PrivateKey.from_private_bytes(
      base64.b64decode(private_key_b64, validate=True)
    )
    plaintext = ChaCha20Poly1305(
      _dm_key(private.exchange(ephemeral))
    ).decrypt(nonce, ciphertext, message_id.encode("utf-8"))
    payload = json.loads(plaintext)
    if not isinstance(payload, dict) or set(payload) != {
      "text", "attachment", "reply_to",
    }:
      raise ValueError("invalid sealed payload")
    return payload
  except Exception as exc:
    raise HTTPException(
      status_code=400, detail="Message could not be decrypted."
    ) from exc


def _message_preview(text: str) -> str:
  return text[:120] if text.strip() else "📷 Photo"


def _write_app_attachment(
  container: Path, message_id: str, attachment: tuple[dict, bytes]
) -> dict:
  wire, data = attachment
  ext = _ATTACHMENT_MIME_EXT[wire["mime"]]
  relative = Path("media") / f"{message_id}.{ext}"
  path = container / relative
  path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(path, data)
  return {
    "mime": wire["mime"], "w": wire["w"], "h": wire["h"],
    "file": relative.as_posix(),
  }


# ── identity ────────────────────────────────────────────────────────────────

def _new_encryption_keypair() -> tuple[str, str]:
  """Return a raw X25519 private/public keypair encoded as base64."""
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
  key = X25519PrivateKey.generate()
  private_b64 = base64.b64encode(
    key.private_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PrivateFormat.Raw,
      encryption_algorithm=serialization.NoEncryption(),
    )
  ).decode()
  public_b64 = base64.b64encode(
    key.public_key().public_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PublicFormat.Raw,
    )
  ).decode()
  return private_b64, public_b64


def _load_identity() -> dict:
  """Load (or lazily create) this instance's federation identity."""
  path = _identity_path()
  if path.is_file():
    identity = json.loads(path.read_text())
    if not identity.get("enc_private_key_b64") or not identity.get(
      "enc_public_key_b64"
    ):
      private_b64, public_b64 = _new_encryption_keypair()
      identity["enc_private_key_b64"] = private_b64
      identity["enc_public_key_b64"] = public_b64
      _save_identity(identity)
    return identity
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  from cryptography.hazmat.primitives import serialization
  key = Ed25519PrivateKey.generate()
  private_b64 = base64.b64encode(
    key.private_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PrivateFormat.Raw,
      encryption_algorithm=serialization.NoEncryption(),
    )
  ).decode()
  public_b64 = base64.b64encode(
    key.public_key().public_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PublicFormat.Raw,
    )
  ).decode()
  enc_private_b64, enc_public_b64 = _new_encryption_keypair()
  identity = {
    "private_key_b64": private_b64,
    "public_key_b64": public_b64,
    "enc_private_key_b64": enc_private_b64,
    "enc_public_key_b64": enc_public_b64,
    "name": "",
    "bio": "",
    "community_host": _canonical_community_host(),
    "created_at": int(time.time()),
  }
  atomic_write(path, json.dumps(identity, indent=2))
  path.chmod(0o600)
  return identity


def _save_identity(identity: dict) -> None:
  path = _identity_path()
  atomic_write(path, json.dumps(identity, indent=2))
  path.chmod(0o600)


def _key_actor_doc(identity: dict) -> dict:
  """The key-only actor card used by private federation peers."""
  return {
    "protocol": PROTOCOL,
    "host": _own_host(),
    "public_key": {"alg": "ed25519", "key_b64": identity["public_key_b64"]},
    "encryption_key": {
      "alg": "x25519", "key_b64": identity["enc_public_key_b64"],
    },
    "inbox": "/api/common/inbox",
  }


def _actor_doc(identity: dict, db: Session) -> dict:
  """The joined public profile card; the display name remains local."""
  host = _own_host()
  owner = db.query(models.Owner).first()
  member_since = (
    owner.created_at.date().isoformat()
    if owner is not None and owner.created_at is not None else None
  )
  public_apps = (
    db.query(models.App.name, models.App.description)
    .filter(
      models.App.published_manifest_url.isnot(None),
      models.App.deleted_at.is_(None),
    )
    .order_by(models.App.id.asc())
    .limit(8)
    .all()
  )
  return {
    **_key_actor_doc(identity),
    "address": f"{identity.get('handle') or 'someone'}@{host}",
    "handle": identity.get("handle") or "",
    "bio": identity.get("bio") or "",
    "avatar": _avatar_path().is_file(),
    "joined_at": identity.get("joined_at") or None,
    "member_since": member_since,
    "apps": [
      {"name": app.name, "description": (app.description or "")[:140]}
      for app in public_apps
    ],
  }


# ── peer actor cache ────────────────────────────────────────────────────────

# ── conversation storage (in the Common app's per-app storage) ──────────────

def _common_app(db: Session) -> models.App:
  app = db.query(models.App).filter(models.App.slug == APP_SLUG).first()
  if app is None:
    raise HTTPException(
      status_code=503, detail="The Common app is not installed on this instance."
    )
  return app


def _app_data_dir(app: models.App) -> Path:
  return Path(get_settings().data_dir) / "apps" / str(app.id)


def _conversation_dir(app: models.App, peer_host: str) -> Path:
  safe = re.sub(r"[^a-z0-9.-]", "_", peer_host)
  return _app_data_dir(app) / "conversations" / safe


def _bump_version(app: models.App) -> None:
  """Advance the app's change counter (call while holding its storage lock).

  The open app UI watches this one small file to learn that new federated
  data (a DM, a group message) landed in its storage.
  """
  version_path = _app_data_dir(app) / "state" / "version.json"
  version_path.parent.mkdir(parents=True, exist_ok=True)
  version = 0
  if version_path.is_file():
    version = int(json.loads(version_path.read_text()).get("v") or 0)
  atomic_write(
    version_path, json.dumps({"v": version + 1, "updated_at": time.time()})
  )


async def _store_message(
  db: Session, app: models.App, peer_host: str, record: dict,
  attachment: tuple[dict, bytes] | None = None,
) -> tuple[bool, str]:
  """Store one message atomically and return ``(created, request_state)``.

  A metadata record written by an older Social version is an established
  conversation.  Only a genuinely new inbound conversation becomes a quiet
  request.  Keeping the duplicate check under the app-storage lock also makes
  concurrent federation retries unable to double-count unread/request state.
  """
  async with fs_locks.app_storage_lock(app.id):
    convo = _conversation_dir(app, peer_host)
    meta_path = convo / "meta.json"
    had_meta = meta_path.is_file()
    meta = json.loads(meta_path.read_text()) if had_meta else {}
    state = meta.get("request_status")
    if state not in REQUEST_STATES:
      state = "accepted" if had_meta or record["dir"] == "out" else "pending"
    # Blocking is a receive-time discard policy, not merely a notification
    # preference.  Decide it under the same lock as owner request decisions
    # and before materializing either the message or its attachment.
    if state == "blocked" and record["dir"] == "in":
      return False, state
    msgs = convo / "msgs"
    message_path = msgs / f"{record['id']}.json"
    if message_path.is_file():
      return False, state
    if attachment is not None:
      record["attachment"] = _write_app_attachment(
        convo, record["id"], attachment
      )
    msgs.mkdir(parents=True, exist_ok=True)
    atomic_write(message_path, json.dumps(record))
    meta.update(
      peer=peer_host,
      last_text=_message_preview(record["text"]),
      last_at=record["sent_at"],
      last_dir=record["dir"],
      request_status=state,
    )
    if record.get("peer_handle"):
      meta["peer_handle"] = record["peer_handle"]
    if record["dir"] == "in" and state == "accepted":
      meta["unread"] = int(meta.get("unread") or 0) + 1
    elif record["dir"] == "in" and state == "pending":
      meta["request_count"] = int(meta.get("request_count") or 0) + 1
      meta["unread"] = 0
    elif state != "accepted":
      meta["unread"] = 0
    atomic_write(meta_path, json.dumps(meta))
    _bump_version(app)
    return True, state


async def _prepare_outgoing_conversation(
  app: models.App, peer_host: str,
) -> None:
  """Persist consent for a new owner-initiated DM, or reject a request reply."""
  async with fs_locks.app_storage_lock(app.id):
    convo = _conversation_dir(app, peer_host)
    meta_path = convo / "meta.json"
    if meta_path.is_file():
      meta = json.loads(meta_path.read_text())
      state = meta.get("request_status")
      if state in {"pending", "declined", "blocked"}:
        raise HTTPException(
          status_code=409,
          detail="Accept this message request before replying.",
        )
      # Missing state is the backwards-compatible accepted interpretation.
      return
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(meta_path, json.dumps({
      "peer": peer_host,
      "request_status": "accepted",
      "unread": 0,
    }))
    _bump_version(app)


async def _set_dm_request_state(
  app: models.App, peer_host: str, state: str,
) -> str:
  """Apply one owner decision without deleting the retained conversation."""
  async with fs_locks.app_storage_lock(app.id):
    meta_path = _conversation_dir(app, peer_host) / "meta.json"
    if not meta_path.is_file():
      raise HTTPException(status_code=404, detail="Message request not found.")
    meta = json.loads(meta_path.read_text())
    current = meta.get("request_status")
    if current not in REQUEST_STATES:
      current = "accepted"
    if state == "accepted" and current == "accepted":
      return "accepted"
    if current != "pending":
      raise HTTPException(status_code=409, detail="Message request is no longer pending.")
    meta.update(request_status=state, request_count=0, unread=0)
    atomic_write(meta_path, json.dumps(meta))
    _bump_version(app)
    return state


# ── public peer surface ─────────────────────────────────────────────────────

@router.get("/actor")
def get_actor(db: Session = Depends(get_db)):
  """Publish keys for private federation, and profile data only after join."""
  # An unauthenticated probe must not lazily create an identity on an
  # untouched installation. Authenticated owner use and established
  # federation operations create this file before peers need its keys.
  if not _identity_path().is_file():
    raise HTTPException(status_code=404, detail="Social profile not found.")
  identity = _load_identity()
  if not identity.get("joined_at"):
    return _key_actor_doc(identity)
  return _actor_doc(identity, db)


def _serve_avatar(path: Path) -> FileResponse:
  if not path.is_file():
    raise HTTPException(status_code=404, detail="Avatar not found.")
  return FileResponse(str(path), media_type="image/png")


@router.get("/avatar")
def get_avatar():
  """This instance's public profile avatar. Public by design."""
  if not _identity_path().is_file():
    raise HTTPException(status_code=404, detail="Social profile not found.")
  if not _load_identity().get("joined_at"):
    raise HTTPException(status_code=404, detail="Social profile not found.")
  return _serve_avatar(_avatar_path())


@router.post("/inbox")
async def receive_message(request: Request, db: Session = Depends(get_db)):
  """Accept one signed direct message from a peer instance."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0 or envelope.get("type") != "message":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  if envelope.get("to") != _own_host():
    raise HTTPException(status_code=400, detail="Envelope is addressed elsewhere.")
  message_id = envelope.get("id")
  if not isinstance(message_id, str) or not _valid_id(message_id):
    raise HTTPException(status_code=400, detail="Message id is invalid.")
  actor = await _verify_peer_envelope(envelope)
  encrypted = "enc" in envelope
  if encrypted:
    payload = _open_dm(
      message_id, envelope.get("enc"), _load_identity()["enc_private_key_b64"]
    )
    text = payload.get("text")
    attachment = _validate_attachment(payload.get("attachment"))
    reply_to = _validate_reply_to(payload.get("reply_to"))
  else:
    text = envelope.get("text")
    attachment = _validate_attachment(envelope.get("attachment"))
    reply_to = _validate_reply_to(envelope.get("reply_to"))
  _validate_text_or_attachment(text, attachment, "Message text is invalid.")
  sender = envelope["from"]
  app = _common_app(db)
  sender_label = f"@{actor['handle']}" if actor.get("handle") else sender
  record = {
    "id": message_id,
    "dir": "in",
    "peer": sender,
    "peer_handle": actor.get("handle") or "",
    "text": text,
    "sent_at": envelope["sent_at"],
    "status": "delivered",
  }
  if encrypted:
    record["encrypted"] = True
  if reply_to is not None:
    record["reply_to"] = reply_to
  created, request_state = await _store_message(
    db, app, sender, record, attachment
  )
  if not created:
    # A blocked sender gets the same successful receipt as ordinary delivery;
    # the discard policy is local owner state, not federation metadata.
    if request_state == "blocked":
      return {"status": "delivered"}
    return {"status": "duplicate"}
  owner = db.query(models.Owner).first()
  if owner is not None and request_state == "accepted":
    try:
      push.notify_owner(
        db,
        owner.id,
        title=f"Message from {sender_label}",
        body=_message_preview(text),
        source_type="app",
        source_id=str(app.id),
        target=f"/shell/?app={app.id}",
      )
    except Exception:
      pass  # delivery of the message itself must not fail on push problems
  return {
    "status": "delivered" if request_state == "accepted" else "pending",
  }


# The directory and board peer surface is shared with the isolated host.
_public_router, _public_write_limiter = create_public_router(
  _public_store, _actor_verifier, prefix="",
)
router.include_router(_public_router)


# ── owner surface ───────────────────────────────────────────────────────────

def _require_owner_or_common_app(
  db: Session, principal: Principal
) -> models.App:
  """The owner, or the Common app's own scoped token, may act."""
  app = _common_app(db)
  if principal.app_id is not None and principal.app_id != app.id:
    raise HTTPException(status_code=403, detail="Not available to other apps.")
  return app


class ProfileUpdate(BaseModel):
  bio: str | None = None
  community_host: str | None = None


class SendMessage(BaseModel):
  to: str
  text: str
  peer_handle: str | None = None
  attachment: Any = None
  reply_to: Any = None


def _request_peer(peer_host: str) -> str:
  host = peer_host.strip().lower()
  if not _valid_host(host):
    raise HTTPException(status_code=400, detail="Invalid peer host.")
  return host


class PublishPost(BaseModel):
  text: str
  attachment: Any = None


def _identity_app_id(db: Session) -> int | None:
  """The Möbius · You app on this instance, for the connect deep link."""
  app = db.query(models.App).filter(models.App.slug == "identity").first()
  return app.id if app else None


async def _refresh_profile_cache(db: Session, principal: Principal) -> dict:
  """Pull the mobius.you profile into the federation identity cache.

  Returns {"identity", "profile", "account_error"}. The cache keeps the
  actor card and directory registration working when the account service is
  briefly unreachable; the live profile remains the source of truth.
  """
  identity = _load_identity()
  profile = None
  account_error = None
  try:
    profile = await identity_routes.resolve_owner_profile(db, principal.owner)
  except HTTPException as exc:
    account_error = str(exc.detail)
  if profile:
    name = str(profile.get("display_name") or profile.get("handle") or "")
    handle = str(profile.get("handle") or "")
    if name[:MAX_NAME_CHARS] != identity.get("name") or (
      handle[:MAX_NAME_CHARS] != identity.get("handle")
    ):
      identity["name"] = name[:MAX_NAME_CHARS]
      identity["handle"] = handle[:MAX_NAME_CHARS]
      _save_identity(identity)
    avatar_url = profile.get("avatar_url")
    if (
      isinstance(avatar_url, str)
      and avatar_url
      and avatar_url != identity.get("avatar_source_url")
    ):
      try:
        avatar = await _download_avatar(avatar_url)
        atomic_write(_avatar_path(), avatar)
        identity["avatar_source_url"] = avatar_url
        _save_identity(identity)
      except Exception:
        pass
  return {"identity": identity, "profile": profile, "account_error": account_error}


@router.get("/me")
async def get_me(
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal)
):
  _require_owner_or_common_app(db, principal)
  state = await _refresh_profile_cache(db, principal)
  identity = state["identity"]
  return {
    "host": _own_host(),
    "name": identity.get("name") or "",
    "handle": identity.get("handle") or "",
    "bio": identity.get("bio") or "",
    "community_host": _canonical_community_host(identity.get("community_host")),
    "connected": bool(state["profile"]) or bool(identity.get("name")),
    "joined": bool(identity.get("joined_at")),
    "account_error": state["account_error"],
    "identity_app_id": _identity_app_id(db),
  }


@router.post("/join")
async def join_community(
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal)
):
  """Join Common as the owner's mobius.you identity."""
  require_nondelegated_owner_control(principal)
  _require_owner_or_common_app(db, principal)
  state = await _refresh_profile_cache(db, principal)
  identity = state["identity"]
  if not state["profile"] and not identity.get("name"):
    raise HTTPException(
      status_code=409,
      detail=(
        "No Möbius profile is connected yet. Connect your account in "
        "Möbius · You first."
      ),
    )
  identity["joined_at"] = identity.get("joined_at") or time.time()
  _save_identity(identity)
  status = await _register_with_community_host(identity)
  return {
    "status": "joined",
    "directory": status,
    "name": identity.get("name") or "",
    "handle": identity.get("handle") or "",
  }


async def _register_with_community_host(identity: dict) -> str:
  """Announce this instance to its community host. Returns a status string."""
  host = _canonical_community_host(identity.get("community_host"))
  envelope = {
    "v": 0,
    "type": "register",
    "from": _own_host(),
    "handle": identity.get("handle") or "",
    "bio": identity.get("bio") or "",
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  if host == _own_host():
    # Local shortcut: the community host is this very instance.
    _public_store.register(
      _own_host(), envelope["handle"], envelope["bio"],
    )
    return "registered"
  try:
    response = await federation_request(
      "POST", f"{_peer_base_url(host)}/api/common/directory", json=envelope,
      max_response_bytes=MAX_ENVELOPE_BYTES,
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return "registered"
  except Exception:
    return "unreachable"


@router.put("/me")
async def update_me(
  update: ProfileUpdate,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  require_nondelegated_owner_control(principal)
  _require_owner_or_common_app(db, principal)
  identity = _load_identity()
  if update.bio is not None:
    identity["bio"] = update.bio.strip()[:MAX_BIO_CHARS]
  if update.community_host is not None:
    host = update.community_host.strip().lower()
    if host and not _valid_host(host):
      raise HTTPException(status_code=400, detail="Invalid community host.")
    identity["community_host"] = host or _own_host()
  _save_identity(identity)
  status = (
    await _register_with_community_host(identity)
    if identity.get("joined_at") else "not_joined"
  )
  return {"status": "saved", "directory": status}


@router.post("/requests/dm/{peer_host}/accept")
async def accept_message_request(
  peer_host: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  # Acceptance may be the first authenticated federation action on a legacy
  # plaintext request. Create keys now so the accepted peer can verify and
  # encrypt the owner's reply without requiring a public-directory join.
  _load_identity()
  state = await _set_dm_request_state(app, _request_peer(peer_host), "accepted")
  return {"status": state}


@router.post("/requests/dm/{peer_host}/decline")
async def decline_message_request(
  peer_host: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  state = await _set_dm_request_state(app, _request_peer(peer_host), "declined")
  return {"status": state}


@router.post("/requests/dm/{peer_host}/block")
async def block_message_request(
  peer_host: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  state = await _set_dm_request_state(app, _request_peer(peer_host), "blocked")
  return {"status": state}


@router.post("/send")
async def send_message(
  message: SendMessage,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Sign a DM, deliver it to the peer instance, and store our own copy."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  to_host = message.to.strip().lower()
  text = message.text.strip()
  if not _valid_host(to_host):
    raise HTTPException(status_code=400, detail="Invalid recipient.")
  attachment = _validate_attachment(message.attachment)
  reply_to = _validate_reply_to(message.reply_to)
  _validate_text_or_attachment(text, attachment, "Message text is invalid.")
  identity = _load_identity()
  actor = await _fetch_actor(to_host)
  # The first deliberate outgoing message establishes consent. A reply to an
  # inbound request must instead go through the explicit acceptance action.
  await _prepare_outgoing_conversation(app, to_host)
  message_id = str(uuid.uuid4())
  envelope = {
    "v": 0,
    "type": "message",
    "id": message_id,
    "from": _own_host(),
    "to": to_host,
    "text": text,
    "sent_at": time.time(),
  }
  encryption_key = actor.get("encryption_key")
  encrypted = (
    isinstance(encryption_key, dict)
    and encryption_key.get("alg") == "x25519"
  )
  if encrypted:
    recipient_key_b64 = encryption_key.get("key_b64")
    if not isinstance(recipient_key_b64, str) or not recipient_key_b64:
      raise HTTPException(
        status_code=502, detail=f"{to_host} published no valid encryption key."
      )
    try:
      envelope["enc"] = _seal_dm(
        message_id, recipient_key_b64, text=text,
        attachment=attachment[0] if attachment is not None else None,
        reply_to=reply_to,
      )
    except Exception as exc:
      raise HTTPException(
        status_code=502, detail=f"{to_host} published no valid encryption key."
      ) from exc
    envelope["text"] = ""
  else:
    if attachment is not None:
      envelope["attachment"] = attachment[0]
    if reply_to is not None:
      envelope["reply_to"] = reply_to
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  status = "delivered"
  detail = None
  try:
    response = await federation_request(
      "POST", f"{_peer_base_url(to_host)}/api/common/inbox", json=envelope,
      max_response_bytes=MAX_ENVELOPE_BYTES,
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
  except httpx.HTTPStatusError as exc:
    status = "failed"
    detail = f"The peer rejected the message ({exc.response.status_code})."
  except Exception:
    status = "failed"
    detail = "The peer could not be reached."
  record = {
    "id": envelope["id"],
    "dir": "out",
    "peer": to_host,
    "text": text,
    "sent_at": envelope["sent_at"],
    "status": status,
  }
  if encrypted:
    record["encrypted"] = True
  if message.peer_handle:
    record["peer_handle"] = message.peer_handle.strip()[:MAX_NAME_CHARS]
  if reply_to is not None:
    record["reply_to"] = reply_to
  await _store_message(db, app, to_host, record, attachment)
  return {"status": status, "id": envelope["id"], "detail": detail}


@router.post("/publish")
async def publish_post(
  post: PublishPost,
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Sign a board post and submit it to the community host."""
  require_nondelegated_owner_control(principal)
  _require_owner_or_common_app(db, principal)
  text = post.text.strip()
  attachment = _validate_attachment(post.attachment)
  _validate_text_or_attachment(text, attachment, "Post text is invalid.")
  identity = _load_identity()
  envelope = {
    "v": 0,
    "type": "board_post",
    "id": str(uuid.uuid4()),
    "from": _own_host(),
    "text": text,
    "sent_at": time.time(),
  }
  if attachment is not None:
    envelope["attachment"] = attachment[0]
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  host = _browse_community_host(community_host)
  if host == _own_host():
    board_post = {
      "id": envelope["id"],
      "host": _own_host(),
      "handle": identity.get("handle") or "",
      "text": text,
      "created_at": envelope["sent_at"],
      "replies": [],
    }
    _store_board_post(board_post, attachment)
    return {"status": "posted", "id": envelope["id"]}
  try:
    response = await federation_request(
      "POST", f"{_peer_base_url(host)}/api/common/board", json=envelope,
      max_response_bytes=MAX_ENVELOPE_BYTES,
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return {"status": "posted", "id": envelope["id"]}
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail="Community host could not be reached."
    ) from exc


def _browse_community_host(requested: str | None) -> str:
  """Choose a public read destination without changing membership or identity."""
  if requested is not None:
    host = requested.strip().lower()
    if not _valid_host(host):
      raise HTTPException(status_code=400, detail="Invalid community host.")
    return host
  path = _identity_path()
  identity = json.loads(path.read_text()) if path.is_file() else {}
  return _canonical_community_host(identity.get("community_host"))


@router.get("/replies/{post_id}")
async def get_replies_for_owner(
  post_id: str,
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Read the selected public board's replies through the safe peer transport."""
  _require_owner_or_common_app(db, principal)
  if not _valid_id(post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  host = _browse_community_host(community_host)
  if host == _own_host():
    return _public_store.get_replies(post_id)
  try:
    response = await federation_request(
      "GET", f"{_peer_base_url(host)}/api/common/board/{post_id}/replies",
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result.get("replies"), list):
      raise ValueError("Invalid reply response")
    return result
  except HTTPException:
    raise
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail="Community replies could not be reached."
    ) from exc


@router.get("/board-media/{post_id}")
async def get_board_media_for_owner(
  post_id: str,
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Serve a community-board image, caching remote hosts for 24 hours."""
  _require_owner_or_common_app(db, principal)
  if not _valid_id(post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  host = _browse_community_host(community_host)
  if host == _own_host():
    return _serve_image(_find_image(_board_media_dir(), post_id))

  cache_dir = _peer_board_media_dir()
  stem = _peer_board_media_name(host, post_id)
  cached = _find_image(cache_dir, stem)
  if (
    cached is not None
    and time.time() - cached[0].stat().st_mtime < BOARD_MEDIA_CACHE_TTL_S
  ):
    return _serve_image(cached)
  try:
    mime, data = await _download_board_media(
      f"{_peer_base_url(host)}/api/common/board/media/{post_id}"
    )
    target = cache_dir / f"{stem}.{_ATTACHMENT_MIME_EXT[mime]}"
    atomic_write(target, data)
    for _old_mime, ext in _ATTACHMENT_MIME_EXT.items():
      old = cache_dir / f"{stem}.{ext}"
      if old != target and old.is_file():
        old.unlink()
    cached = (target, mime)
  except Exception:
    if cached is None:
      raise HTTPException(status_code=404, detail="Board image not found.")
  return _serve_image(cached)


@router.get("/feed")
async def get_feed(
  limit: int = 30,
  before: float | None = None,
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """The community host's board, proxied for the app UI."""
  _require_owner_or_common_app(db, principal)
  host = _browse_community_host(community_host)
  if host == _own_host():
    posts = _read_board(min(max(limit, 1), BOARD_PAGE_LIMIT), before, _own_host())
    return {"host": host, "posts": posts}
  try:
    response = await federation_request(
      "GET", f"{_peer_base_url(host)}/api/common/board",
      params={
        "limit": limit, "viewer": _own_host(),
        **({"before": before} if before else {}),
      },
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return {"host": host, **response.json()}
  except HTTPException:
    raise
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail="Community host could not be reached."
    ) from exc


class LikePost(BaseModel):
  post_id: str


class ReplyPost(BaseModel):
  post_id: str
  text: str


@router.post("/like")
async def like_post(
  body: LikePost,
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Toggle a like on a community-board post, signed as this instance."""
  require_nondelegated_owner_control(principal)
  _require_owner_or_common_app(db, principal)
  post_id = body.post_id.strip()
  if not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  identity = _load_identity()
  host = _browse_community_host(community_host)
  if host == _own_host():
    return _toggle_board_like(post_id, _own_host())
  envelope = {
    "v": 0,
    "type": "board_react",
    "post_id": post_id,
    "from": _own_host(),
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    response = await federation_request(
      "POST", f"{_peer_base_url(host)}/api/common/board/react", json=envelope,
      max_response_bytes=MAX_ENVELOPE_BYTES,
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail="Community host could not be reached."
    ) from exc


@router.post("/reply")
async def reply_to_post(
  body: ReplyPost,
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Reply to a community-board post, signed as this instance."""
  require_nondelegated_owner_control(principal)
  _require_owner_or_common_app(db, principal)
  post_id = body.post_id.strip()
  if not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  text = body.text.strip()
  if not text or len(text) > MAX_REPLY_TEXT_CHARS:
    raise HTTPException(status_code=400, detail="Reply text is invalid.")
  identity = _load_identity()
  host = _browse_community_host(community_host)
  reply_id = str(uuid.uuid4())
  sent_at = time.time()
  if host == _own_host():
    return _add_board_reply(
      post_id, reply_id, _own_host(), identity.get("handle") or "",
      text, sent_at,
    )
  envelope = {
    "v": 0,
    "type": "board_reply",
    "post_id": post_id,
    "id": reply_id,
    "text": text,
    "from": _own_host(),
    "sent_at": sent_at,
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    response = await federation_request(
      "POST", f"{_peer_base_url(host)}/api/common/board/reply", json=envelope,
      max_response_bytes=MAX_ENVELOPE_BYTES,
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail="Community host could not be reached."
    ) from exc


@router.get("/people")
async def search_people(
  q: str = "",
  community_host: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Search the community host's user directory, proxied for the app UI."""
  _require_owner_or_common_app(db, principal)
  host = _browse_community_host(community_host)
  if host == _own_host():
    return {"host": host, **_public_store.search_directory(q)}
  try:
    response = await federation_request(
      "GET", f"{_peer_base_url(host)}/api/common/directory", params={"q": q},
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return {"host": host, **response.json()}
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail="Community host could not be reached."
    ) from exc


@router.get("/peer/{host}")
async def get_peer(
  host: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """A peer's public actor card, for profile views in the app UI."""
  _require_owner_or_common_app(db, principal)
  actor = await _fetch_actor(host.strip().lower())
  return actor


@router.get("/peer-avatar/{host}")
async def get_peer_avatar(
  host: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """A peer's profile avatar, cached locally for up to 24 hours."""
  _require_owner_or_common_app(db, principal)
  host = host.strip().lower()
  if not _valid_host(host):
    raise HTTPException(status_code=400, detail="Invalid peer host.")
  if host == _own_host():
    return _serve_avatar(_avatar_path())
  cache = _peer_avatar_path(host)
  try:
    actor = await _fetch_actor(host)
  except Exception:
    if cache.is_file():
      return _serve_avatar(cache)
    raise HTTPException(status_code=404, detail="Peer avatar not found.")
  if actor.get("avatar") is not True:
    raise HTTPException(status_code=404, detail="Peer avatar not found.")
  if (
    cache.is_file()
    and time.time() - cache.stat().st_mtime < PEER_AVATAR_CACHE_TTL_S
  ):
    return _serve_avatar(cache)
  try:
    avatar = await _download_avatar(
      f"{_peer_base_url(host)}/api/common/avatar"
    )
    atomic_write(cache, avatar)
  except Exception:
    if not cache.is_file():
      raise HTTPException(status_code=404, detail="Peer avatar not found.")
  return _serve_avatar(cache)
