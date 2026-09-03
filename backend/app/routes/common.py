"""Common federation — the Möbius-to-Möbius social layer (protocol v0).

Every Möbius instance is one user's server. This router gives an instance
three federated capabilities, all instance-to-instance over HTTPS with
Ed25519-signed envelopes and no third-party storage:

1. **Identity** — a public actor card (`GET /actor`) naming the owner and
   publishing the instance's Ed25519 public key. Peers verify every envelope
   against the claimed sender's fetched (and cached) actor card.
2. **Direct messages** — a signed envelope POSTed straight to the recipient
   instance's `/inbox`. Each side stores only its own copy (in the Common
   mini-app's per-app storage), so a conversation lives exclusively on the
   two participants' servers.
3. **Community host role** — any instance can host the shared, public parts:
   an opt-in user directory (search) and a message board. Peers register and
   post with the same signed-envelope scheme. Which host to use is the
   owner's choice (default: their own instance).

Public peer surface (no owner auth; envelope signatures are the authority):
  GET  /api/common/actor        instance identity card
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
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal
from app.routes import identity as identity_routes
from app.storage_io import atomic_write, read_capped_body

router = APIRouter(prefix="/api/common", tags=["common"])

PROTOCOL = "common/0"
APP_SLUG = "common"
MAX_TEXT_CHARS = 4000
MAX_REPLY_TEXT_CHARS = 1000
MAX_NAME_CHARS = 80
MAX_BIO_CHARS = 400
MAX_ENVELOPE_BYTES = 32_768
MAX_ATTACHMENT_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 1024 * 1024
MAX_ATTACHMENT_DIMENSION = 8192
MAX_REPLY_AUTHOR_CHARS = 80
MAX_REPLY_EXCERPT_CHARS = 140
MAX_AVATAR_BYTES = 512 * 1024
ACTOR_CACHE_TTL_S = 3600
PEER_AVATAR_CACHE_TTL_S = 24 * 3600
BOARD_MEDIA_CACHE_TTL_S = 24 * 3600
CLOCK_SKEW_S = 600
OUTBOUND_TIMEOUT_S = 10.0
BOARD_PAGE_LIMIT = 50
BOARD_REPLY_LIMIT = 200
DIRECTORY_LIMIT = 2000

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,250})(:\d{1,5})?$")
_ID_RE = re.compile(r"^[a-f0-9-]{8,64}$")
_ATTACHMENT_MIME_EXT = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
}
_ATTACHMENT_ENVELOPE_TYPES = {
  "message", "group_post", "group_message", "board_post",
}


# ── paths ────────────────────────────────────────────────────────────────────

def _common_dir() -> Path:
  d = Path(get_settings().data_dir) / "common"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _identity_path() -> Path:
  return _common_dir() / "identity.json"


def _avatar_path() -> Path:
  return _common_dir() / "avatar.png"


def _peers_dir() -> Path:
  d = _common_dir() / "peers"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _peer_avatar_path(host: str) -> Path:
  safe = re.sub(r"[^a-z0-9.-]", "_", host)
  d = _peers_dir() / "avatars"
  d.mkdir(parents=True, exist_ok=True)
  return d / f"{safe}.png"


def _directory_path() -> Path:
  return _common_dir() / "directory.json"


def _board_dir() -> Path:
  d = _common_dir() / "board"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _board_media_dir() -> Path:
  d = _common_dir() / "board-media"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _peer_board_media_dir() -> Path:
  d = _peers_dir() / "board-media"
  d.mkdir(parents=True, exist_ok=True)
  return d


# ── host + crypto primitives ────────────────────────────────────────────────

def _own_host() -> str:
  return get_settings().domain


def _valid_host(host: str) -> bool:
  return isinstance(host, str) and bool(_HOST_RE.match(host))


def _is_local_host(host: str) -> bool:
  bare = host.split(":", 1)[0]
  return bare in ("localhost", "127.0.0.1")


def _peer_base_url(host: str) -> str:
  """HTTPS for real peers; plain HTTP only for loopback development hosts."""
  scheme = "http" if _is_local_host(host) else "https"
  return f"{scheme}://{host}"


async def _download_avatar(url: str) -> bytes:
  """Fetch one image while bounding the response body before buffering it."""
  async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
    async with client.stream("GET", url) as response:
      response.raise_for_status()
      content_type = response.headers.get("content-type", "").split(";", 1)[0]
      if not content_type.strip().lower().startswith("image/"):
        raise ValueError("Avatar response is not an image.")
      body = bytearray()
      async for chunk in response.aiter_bytes():
        room = MAX_AVATAR_BYTES + 1 - len(body)
        if room <= 0:
          break
        body.extend(chunk[:room])
        if len(body) > MAX_AVATAR_BYTES:
          raise ValueError("Avatar response is too large.")
  if not body:
    raise ValueError("Avatar response is empty.")
  return bytes(body)


async def _download_board_media(url: str) -> tuple[str, bytes]:
  """Fetch one hosted board image without buffering more than the wire cap."""
  async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
    async with client.stream("GET", url) as response:
      response.raise_for_status()
      mime = (
        response.headers.get("content-type", "")
        .split(";", 1)[0].strip().lower()
      )
      if mime not in _ATTACHMENT_MIME_EXT:
        raise ValueError("Board media response is not a supported image.")
      body = bytearray()
      async for chunk in response.aiter_bytes():
        room = MAX_ATTACHMENT_BYTES + 1 - len(body)
        if room <= 0:
          break
        body.extend(chunk[:room])
        if len(body) > MAX_ATTACHMENT_BYTES:
          raise ValueError("Board media response is too large.")
  if not body:
    raise ValueError("Board media response is empty.")
  return mime, bytes(body)


def _canonical(payload: dict) -> bytes:
  return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _sign(payload: dict, private_key_b64: str) -> str:
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
  return base64.b64encode(key.sign(_canonical(payload))).decode()


def _verify(payload: dict, sig_b64: str, public_key_b64: str) -> bool:
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
  try:
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    key.verify(base64.b64decode(sig_b64), _canonical(payload))
    return True
  except Exception:
    return False


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


def _validate_attachment(value: Any) -> tuple[dict, bytes] | None:
  """Validate and decode the protocol's one supported attachment shape."""
  if value is None:
    return None
  if (
    not isinstance(value, dict)
    or set(value) != {"mime", "data_b64", "w", "h"}
  ):
    raise HTTPException(status_code=400, detail="Attachment is invalid.")
  mime = value.get("mime")
  data_b64 = value.get("data_b64")
  width = value.get("w")
  height = value.get("h")
  if (
    not isinstance(mime, str)
    or mime not in _ATTACHMENT_MIME_EXT
    or not isinstance(data_b64, str)
    or not isinstance(width, int) or isinstance(width, bool)
    or not isinstance(height, int) or isinstance(height, bool)
    or not 1 <= width <= MAX_ATTACHMENT_DIMENSION
    or not 1 <= height <= MAX_ATTACHMENT_DIMENSION
  ):
    raise HTTPException(status_code=400, detail="Attachment is invalid.")
  max_b64_chars = 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3)
  if len(data_b64) > max_b64_chars:
    raise HTTPException(status_code=413, detail="Attachment is too large.")
  try:
    data = base64.b64decode(data_b64, validate=True)
  except Exception as exc:
    raise HTTPException(status_code=400, detail="Attachment data is invalid.") from exc
  if len(data) > MAX_ATTACHMENT_BYTES:
    raise HTTPException(status_code=413, detail="Attachment is too large.")
  if not data:
    raise HTTPException(status_code=400, detail="Attachment data is empty.")
  return value, data


def _validate_reply_to(value: Any) -> dict | None:
  """Validate a self-contained quoted reply without resolving its target."""
  if value is None:
    return None
  if not isinstance(value, dict) or set(value) != {
    "id", "author_handle", "excerpt",
  }:
    raise HTTPException(status_code=400, detail="Quoted reply is invalid.")
  message_id = value.get("id")
  author_handle = value.get("author_handle")
  excerpt = value.get("excerpt")
  if (
    not isinstance(message_id, str) or not _ID_RE.fullmatch(message_id)
    or not isinstance(author_handle, str)
    or len(author_handle) > MAX_REPLY_AUTHOR_CHARS
    or not isinstance(excerpt, str)
    or len(excerpt) > MAX_REPLY_EXCERPT_CHARS
  ):
    raise HTTPException(status_code=400, detail="Quoted reply is invalid.")
  return value


def _validate_text_or_attachment(
  text: Any, attachment: tuple[dict, bytes] | None, detail: str
) -> None:
  if (
    not isinstance(text, str)
    or len(text) > MAX_TEXT_CHARS
    or (not text.strip() and attachment is None)
  ):
    raise HTTPException(status_code=400, detail=detail)


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
    "community_host": _own_host(),
    "created_at": int(time.time()),
  }
  atomic_write(path, json.dumps(identity, indent=2))
  path.chmod(0o600)
  return identity


def _save_identity(identity: dict) -> None:
  path = _identity_path()
  atomic_write(path, json.dumps(identity, indent=2))
  path.chmod(0o600)


def _actor_doc(identity: dict, db: Session) -> dict:
  """The public identity card. Privacy contract: only the handle is public —
  the owner's display name never leaves their instance."""
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
    "protocol": PROTOCOL,
    "host": host,
    "address": f"{identity.get('handle') or 'someone'}@{host}",
    "handle": identity.get("handle") or "",
    "bio": identity.get("bio") or "",
    "avatar": _avatar_path().is_file(),
    "public_key": {"alg": "ed25519", "key_b64": identity["public_key_b64"]},
    "encryption_key": {
      "alg": "x25519", "key_b64": identity["enc_public_key_b64"],
    },
    "joined_at": identity.get("joined_at") or None,
    "member_since": member_since,
    "apps": [
      {"name": app.name, "description": (app.description or "")[:140]}
      for app in public_apps
    ],
    "inbox": "/api/common/inbox",
  }


# ── peer actor cache ────────────────────────────────────────────────────────

def _peer_cache_path(host: str) -> Path:
  safe = re.sub(r"[^a-z0-9.-]", "_", host)
  return _peers_dir() / f"{safe}.json"


async def _fetch_actor(host: str, *, force: bool = False) -> dict:
  """Fetch a peer's actor card, with an on-disk TTL cache."""
  if not _valid_host(host):
    raise HTTPException(status_code=400, detail="Invalid peer host.")
  cache = _peer_cache_path(host)
  if not force and cache.is_file():
    cached = json.loads(cache.read_text())
    if time.time() - cached.get("fetched_at", 0) < ACTOR_CACHE_TTL_S:
      return cached["actor"]
  url = f"{_peer_base_url(host)}/api/common/actor"
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.get(url)
      response.raise_for_status()
      actor = response.json()
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"Could not reach {host}: {exc}"
    ) from exc
  if actor.get("protocol") != PROTOCOL or actor.get("host") != host:
    raise HTTPException(status_code=502, detail=f"{host} is not a valid Common peer.")
  key = (actor.get("public_key") or {}).get("key_b64")
  if not isinstance(key, str) or not key:
    raise HTTPException(status_code=502, detail=f"{host} published no valid key.")
  atomic_write(cache, json.dumps({"fetched_at": time.time(), "actor": actor}))
  return actor


async def _verify_peer_envelope(envelope: dict) -> dict:
  """Verify a signed peer envelope; returns the sender's actor card."""
  sender = envelope.get("from")
  sig = envelope.get("sig")
  if not _valid_host(sender or "") or not isinstance(sig, str):
    raise HTTPException(status_code=400, detail="Malformed envelope.")
  sent_at = envelope.get("sent_at")
  if not isinstance(sent_at, (int, float)) or abs(time.time() - sent_at) > CLOCK_SKEW_S:
    raise HTTPException(status_code=400, detail="Envelope timestamp out of range.")
  payload = {k: v for k, v in envelope.items() if k != "sig"}
  actor = await _fetch_actor(sender)
  if not _verify(payload, sig, actor["public_key"]["key_b64"]):
    # The peer may have rotated keys; refetch once before rejecting. An
    # unreachable peer during the refetch still means the envelope could not
    # be validated — reject it as unsigned rather than surfacing a gateway
    # error for what is, from the sender's perspective, a bad signature.
    try:
      actor = await _fetch_actor(sender, force=True)
    except HTTPException:
      raise HTTPException(status_code=403, detail="Envelope signature is invalid.")
    if not _verify(payload, sig, actor["public_key"]["key_b64"]):
      raise HTTPException(status_code=403, detail="Envelope signature is invalid.")
  return actor


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
) -> None:
  """Store one message record and bump the app's change counter."""
  async with fs_locks.app_storage_lock(app.id):
    convo = _conversation_dir(app, peer_host)
    if attachment is not None:
      record["attachment"] = _write_app_attachment(
        convo, record["id"], attachment
      )
    msgs = convo / "msgs"
    msgs.mkdir(parents=True, exist_ok=True)
    atomic_write(msgs / f"{record['id']}.json", json.dumps(record))
    meta_path = convo / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    meta.update(
      peer=peer_host,
      last_text=_message_preview(record["text"]),
      last_at=record["sent_at"],
      last_dir=record["dir"],
    )
    if record.get("peer_handle"):
      meta["peer_handle"] = record["peer_handle"]
    if record["dir"] == "in":
      meta["unread"] = int(meta.get("unread") or 0) + 1
    atomic_write(meta_path, json.dumps(meta))
    _bump_version(app)


# ── public peer surface ─────────────────────────────────────────────────────

@router.get("/actor")
def get_actor(db: Session = Depends(get_db)):
  """This instance's public identity card. Public by design."""
  return _actor_doc(_load_identity(), db)


def _serve_avatar(path: Path) -> FileResponse:
  if not path.is_file():
    raise HTTPException(status_code=404, detail="Avatar not found.")
  return FileResponse(str(path), media_type="image/png")


@router.get("/avatar")
def get_avatar():
  """This instance's public profile avatar. Public by design."""
  return _serve_avatar(_avatar_path())


async def _read_envelope(request: Request) -> dict:
  body = await read_capped_body(request, MAX_ATTACHMENT_ENVELOPE_BYTES)
  try:
    envelope = json.loads(body)
  except Exception as exc:
    raise HTTPException(status_code=400, detail="Envelope is not JSON.") from exc
  if not isinstance(envelope, dict):
    raise HTTPException(status_code=400, detail="Envelope is not an object.")
  supports_large_payload = (
    envelope.get("type") in _ATTACHMENT_ENVELOPE_TYPES
    and envelope.get("attachment") is not None
  ) or (
    envelope.get("type") == "message" and envelope.get("enc") is not None
  )
  if len(body) > MAX_ENVELOPE_BYTES and not supports_large_payload:
    raise HTTPException(status_code=413, detail="Envelope too large.")
  return envelope


@router.post("/inbox")
async def receive_message(request: Request, db: Session = Depends(get_db)):
  """Accept one signed direct message from a peer instance."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0 or envelope.get("type") != "message":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  if envelope.get("to") != _own_host():
    raise HTTPException(status_code=400, detail="Envelope is addressed elsewhere.")
  message_id = envelope.get("id")
  if not isinstance(message_id, str) or not _ID_RE.fullmatch(message_id):
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
  # Idempotent delivery: a redelivered envelope id is acknowledged, not duplicated.
  existing = _conversation_dir(app, sender) / "msgs" / f"{message_id}.json"
  if existing.is_file():
    return {"status": "duplicate"}
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
  await _store_message(db, app, sender, record, attachment)
  owner = db.query(models.Owner).first()
  if owner is not None:
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
  return {"status": "delivered"}


@router.get("/directory")
def search_directory(q: str = ""):
  """Search users registered with this community host. Public data."""
  path = _directory_path()
  entries = json.loads(path.read_text()) if path.is_file() else {}
  needle = q.strip().lower()
  results = []
  for host, entry in entries.items():
    haystack = f"{entry.get('handle', '')} {host} {entry.get('bio', '')}".lower()
    if not needle or needle in haystack:
      results.append({
        "host": host,
        **{k: entry[k] for k in ("handle", "bio") if k in entry},
      })
  results.sort(key=lambda e: (e.get("handle") or e["host"]).lower())
  return {"users": results[:200]}


@router.post("/directory")
async def register_in_directory(request: Request):
  """Accept a signed opt-in directory registration from a peer."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0 or envelope.get("type") != "register":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  actor = await _verify_peer_envelope(envelope)
  host = envelope["from"]
  handle = str(envelope.get("handle") or actor.get("handle") or "")[:MAX_NAME_CHARS]
  bio = str(envelope.get("bio") or "")[:MAX_BIO_CHARS]
  path = _directory_path()
  entries = json.loads(path.read_text()) if path.is_file() else {}
  if host not in entries and len(entries) >= DIRECTORY_LIMIT:
    raise HTTPException(status_code=507, detail="Directory is full.")
  entries[host] = {"handle": handle, "bio": bio, "registered_at": time.time()}
  atomic_write(path, json.dumps(entries, indent=2))
  return {"status": "registered"}


def _read_board(limit: int, before: float | None, viewer: str | None = None) -> list[dict]:
  """Board posts, newest first. Like details stay host-side: the feed carries
  only the count and whether the viewer liked it. Replies are read separately;
  the feed carries only their count."""
  posts = []
  for file in _board_dir().glob("*.json"):
    try:
      post = json.loads(file.read_text())
    except Exception:
      continue
    if before is not None and post.get("created_at", 0) >= before:
      continue
    likes = post.pop("likes", {})
    post["like_count"] = len(likes)
    if viewer is not None:
      post["liked"] = viewer in likes
    replies = post.pop("replies", [])
    post["reply_count"] = len(replies)
    posts.append(post)
  posts.sort(key=lambda p: p.get("created_at", 0), reverse=True)
  return posts[:limit]


def _board_media_path(post_id: str, mime: str) -> Path:
  return _board_media_dir() / f"{post_id}.{_ATTACHMENT_MIME_EXT[mime]}"


def _peer_board_media_name(host: str, post_id: str) -> str:
  safe_host = re.sub(r"[^a-z0-9.-]", "_", host)
  return f"{safe_host}-{post_id}"


def _find_image(directory: Path, stem: str) -> tuple[Path, str] | None:
  for mime, ext in _ATTACHMENT_MIME_EXT.items():
    path = directory / f"{stem}.{ext}"
    if path.is_file():
      return path, mime
  return None


def _serve_image(found: tuple[Path, str] | None) -> FileResponse:
  if found is None:
    raise HTTPException(status_code=404, detail="Board image not found.")
  path, mime = found
  return FileResponse(str(path), media_type=mime)


def _store_board_post(
  post: dict, attachment: tuple[dict, bytes] | None = None
) -> bool:
  """Store one hosted post and its image; return False for a duplicate id."""
  path = _board_dir() / f"{post['id']}.json"
  if path.is_file():
    return False
  if attachment is not None:
    wire, data = attachment
    atomic_write(_board_media_path(post["id"], wire["mime"]), data)
    post["attachment"] = {
      "mime": wire["mime"], "w": wire["w"], "h": wire["h"],
    }
  atomic_write(path, json.dumps(post))
  return True


@router.get("/board")
def get_board(limit: int = 30, before: float | None = None, viewer: str | None = None):
  """This host's public board feed, newest first. `viewer` (a peer host) marks
  which posts that instance liked; like counts are public either way."""
  if viewer is not None and not _valid_host(viewer):
    viewer = None
  return {"posts": _read_board(min(max(limit, 1), BOARD_PAGE_LIMIT), before, viewer)}


@router.get("/board/media/{post_id}")
def get_board_media(post_id: str):
  """Serve one image hosted alongside this instance's public board."""
  if not _ID_RE.fullmatch(post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  return _serve_image(_find_image(_board_media_dir(), post_id))


def _toggle_board_like(post_id: str, host: str) -> dict:
  """Toggle one instance's like on a hosted board post."""
  path = _board_dir() / f"{post_id}.json"
  if not path.is_file():
    raise HTTPException(status_code=404, detail="Unknown post.")
  post = json.loads(path.read_text())
  likes = post.setdefault("likes", {})
  if host in likes:
    del likes[host]
  else:
    likes[host] = time.time()
  atomic_write(path, json.dumps(post))
  return {"status": "ok", "likes": len(likes), "liked": host in likes}


def _add_board_reply(
  post_id: str, reply_id: str, host: str, handle: str, text: str, created_at: float
) -> dict:
  """Append one idempotent reply to a hosted board post."""
  path = _board_dir() / f"{post_id}.json"
  if not path.is_file():
    raise HTTPException(status_code=404, detail="Unknown post.")
  post = json.loads(path.read_text())
  replies = post.get("replies")
  if not isinstance(replies, list):
    replies = []
    post["replies"] = replies
  if any(reply.get("id") == reply_id for reply in replies):
    return {"status": "ok", "reply_count": len(replies)}
  if len(replies) >= BOARD_REPLY_LIMIT:
    raise HTTPException(status_code=507, detail="Post reply limit reached.")
  replies.append({
    "id": reply_id,
    "host": host,
    "handle": handle,
    "text": text,
    "created_at": created_at,
  })
  atomic_write(path, json.dumps(post))
  return {"status": "ok", "reply_count": len(replies)}


@router.get("/board/{post_id}/replies")
def get_board_replies(post_id: str):
  """A hosted post's public replies, oldest first."""
  if not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  path = _board_dir() / f"{post_id}.json"
  if not path.is_file():
    raise HTTPException(status_code=404, detail="Unknown post.")
  post = json.loads(path.read_text())
  replies = post.get("replies")
  if not isinstance(replies, list):
    replies = []
  replies.sort(key=lambda reply: reply.get("created_at", 0))
  return {"replies": replies}


@router.post("/board/react")
async def react_to_board(request: Request):
  """Accept a signed like toggle from a peer for a post hosted here."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0 or envelope.get("type") != "board_react":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  post_id = envelope.get("post_id")
  if not isinstance(post_id, str) or not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  await _verify_peer_envelope(envelope)
  return _toggle_board_like(post_id, envelope["from"])


@router.post("/board/reply")
async def reply_to_board(request: Request):
  """Accept one signed reply from a peer for a post hosted here."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0 or envelope.get("type") != "board_reply":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  post_id = envelope.get("post_id")
  if not isinstance(post_id, str) or not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  reply_id = envelope.get("id")
  if not isinstance(reply_id, str) or not re.fullmatch(r"[a-f0-9-]{8,64}", reply_id):
    raise HTTPException(status_code=400, detail="Reply id is invalid.")
  text = envelope.get("text")
  if (
    not isinstance(text, str)
    or not text.strip()
    or len(text) > MAX_REPLY_TEXT_CHARS
  ):
    raise HTTPException(status_code=400, detail="Reply text is invalid.")
  actor = await _verify_peer_envelope(envelope)
  return _add_board_reply(
    post_id, reply_id, envelope["from"], actor.get("handle") or "",
    text, envelope["sent_at"],
  )


@router.post("/board")
async def post_to_board(request: Request):
  """Accept a signed board post from a peer."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0 or envelope.get("type") != "board_post":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  text = envelope.get("text")
  attachment = _validate_attachment(envelope.get("attachment"))
  _validate_text_or_attachment(text, attachment, "Post text is invalid.")
  post_id = envelope.get("id")
  if not isinstance(post_id, str) or not _ID_RE.fullmatch(post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  actor = await _verify_peer_envelope(envelope)
  post = {
    "id": post_id,
    "host": envelope["from"],
    "handle": actor.get("handle") or "",
    "text": text,
    "created_at": envelope["sent_at"],
    "replies": [],
  }
  _store_board_post(post, attachment)
  return {"status": "posted"}


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
    "community_host": identity.get("community_host") or _own_host(),
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
  host = identity.get("community_host") or _own_host()
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
    path = _directory_path()
    entries = json.loads(path.read_text()) if path.is_file() else {}
    entries[_own_host()] = {
      "handle": envelope["handle"], "bio": envelope["bio"],
      "registered_at": time.time(),
    }
    atomic_write(path, json.dumps(entries, indent=2))
    return "registered"
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/directory", json=envelope
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


@router.post("/send")
async def send_message(
  message: SendMessage,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Sign a DM, deliver it to the peer instance, and store our own copy."""
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
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(to_host)}/api/common/inbox", json=envelope
      )
      response.raise_for_status()
  except httpx.HTTPStatusError as exc:
    status = "failed"
    try:
      detail = exc.response.json().get("detail")
    except Exception:
      detail = f"{to_host} rejected the message ({exc.response.status_code})."
  except Exception:
    status = "failed"
    detail = f"{to_host} could not be reached."
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
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Sign a board post and submit it to the community host."""
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
  host = identity.get("community_host") or _own_host()
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
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/board", json=envelope
      )
      response.raise_for_status()
    return {"status": "posted", "id": envelope["id"]}
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"Community host {host} could not be reached."
    ) from exc


@router.get("/board-media/{post_id}")
async def get_board_media_for_owner(
  post_id: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Serve a community-board image, caching remote hosts for 24 hours."""
  _require_owner_or_common_app(db, principal)
  if not _ID_RE.fullmatch(post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  identity = _load_identity()
  host = identity.get("community_host") or _own_host()
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
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """The community host's board, proxied for the app UI."""
  _require_owner_or_common_app(db, principal)
  identity = _load_identity()
  host = identity.get("community_host") or _own_host()
  if host == _own_host():
    posts = _read_board(min(max(limit, 1), BOARD_PAGE_LIMIT), before, _own_host())
    return {"host": host, "posts": posts}
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.get(
        f"{_peer_base_url(host)}/api/common/board",
        params={
          "limit": limit, "viewer": _own_host(),
          **({"before": before} if before else {}),
        },
      )
      response.raise_for_status()
      return {"host": host, **response.json()}
  except HTTPException:
    raise
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"Community host {host} could not be reached."
    ) from exc


class LikePost(BaseModel):
  post_id: str


class ReplyPost(BaseModel):
  post_id: str
  text: str


@router.post("/like")
async def like_post(
  body: LikePost,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Toggle a like on a community-board post, signed as this instance."""
  _require_owner_or_common_app(db, principal)
  post_id = body.post_id.strip()
  if not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  identity = _load_identity()
  host = identity.get("community_host") or _own_host()
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
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/board/react", json=envelope
      )
      response.raise_for_status()
      return response.json()
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"Community host {host} could not be reached."
    ) from exc


@router.post("/reply")
async def reply_to_post(
  body: ReplyPost,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Reply to a community-board post, signed as this instance."""
  _require_owner_or_common_app(db, principal)
  post_id = body.post_id.strip()
  if not re.fullmatch(r"[a-f0-9-]{8,64}", post_id):
    raise HTTPException(status_code=400, detail="Post id is invalid.")
  text = body.text.strip()
  if not text or len(text) > MAX_REPLY_TEXT_CHARS:
    raise HTTPException(status_code=400, detail="Reply text is invalid.")
  identity = _load_identity()
  host = identity.get("community_host") or _own_host()
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
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/board/reply", json=envelope
      )
      response.raise_for_status()
      return response.json()
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"Community host {host} could not be reached."
    ) from exc


@router.get("/people")
async def search_people(
  q: str = "",
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Search the community host's user directory, proxied for the app UI."""
  _require_owner_or_common_app(db, principal)
  identity = _load_identity()
  host = identity.get("community_host") or _own_host()
  if host == _own_host():
    return {"host": host, **search_directory(q)}
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.get(
        f"{_peer_base_url(host)}/api/common/directory", params={"q": q}
      )
      response.raise_for_status()
      return {"host": host, **response.json()}
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"Community host {host} could not be reached."
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
