"""Server-authorized capability exchange for opaque embedded chat frames.

The navigated document is intentionally inert and carries no URL credential.
An app-scoped caller mints a random one-time grant for one app-owned chat and
embed instance. The nested frame receives that grant only through postMessage,
exchanges it once, and uses the returned short-lived ``chat_embed`` JWT from
memory. Browser frame metadata is never an authorization input.
"""

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import auth, models
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, chat_embed_grant_is_latest_consumed, get_principal,
  reject_cross_site,
)
from app.resource_access import get_active_chat_for_principal
from app.timeutil import now_naive_utc
from app.theme import theme_data

router = APIRouter()
app_router = APIRouter(prefix="/api/app-chats", tags=["app-chat-embed"])
session_router = APIRouter(prefix="/api/app-chat-embeds", tags=["app-chat-embed"])

BOOTSTRAP_TTL = timedelta(seconds=60)
SESSION_TTL = timedelta(minutes=15)
PARTICIPANT_OPERATIONS = (
  "chat:read",
  "chat:send",
  "chat:stream",
  "chat:stop",
  "chat:settings",
  "chat:uploads",
  "chat:media",
  "models:read",
)


class EmbedInstanceBody(BaseModel):
  instance_id: str = Field(min_length=16, max_length=160)

  @field_validator("instance_id")
  @classmethod
  def validate_instance_id(cls, value: str) -> str:
    value = value.strip()
    if not value or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
      raise ValueError("instance_id must contain visible ASCII characters")
    return value


def _secret_hash(secret: str) -> str:
  return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _bearer_secret(authorization: str | None) -> str:
  scheme, separator, value = (authorization or "").partition(" ")
  if not separator or scheme.lower() != "bearer" or not value.strip():
    raise HTTPException(status_code=401, detail="Missing embed bootstrap grant.")
  return value.strip()


@app_router.post(
  "/{chat_id}/embed-capability",
  dependencies=[Depends(reject_cross_site)],
)
def mint_embed_capability(
  chat_id: str,
  body: EmbedInstanceBody,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Mint a one-use bootstrap grant for one app-owned chat and frame."""
  if principal.scope != "app" or principal.app_id is None:
    raise HTTPException(
      status_code=403,
      detail="Only an app token may mint an embedded-chat capability.",
    )
  get_active_chat_for_principal(db, chat_id, principal)
  app = db.query(models.App).filter(
    models.App.id == principal.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None or not app.token_nonce:
    raise HTTPException(status_code=401, detail="App installation is not valid.")

  now = now_naive_utc()
  db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.session_expires_at.isnot(None),
    models.ChatEmbedGrant.session_expires_at < now - timedelta(days=1),
  ).delete(synchronize_session=False)
  db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.session_id.is_(None),
    models.ChatEmbedGrant.expires_at < now - timedelta(days=1),
  ).delete(synchronize_session=False)

  secret = secrets.token_urlsafe(32)
  expires_at = now + BOOTSTRAP_TTL
  db.add(models.ChatEmbedGrant(
    token_hash=_secret_hash(secret),
    app_id=principal.app_id,
    app_nonce=app.token_nonce,
    chat_id=chat_id,
    instance_id=body.instance_id,
    owner_epoch=principal.owner.token_epoch,
    role="participant",
    operations_json=list(PARTICIPANT_OPERATIONS),
    expires_at=expires_at,
  ))
  db.commit()
  return {
    "capability": secret,
    "expires_at": expires_at.isoformat() + "Z",
    "role": "participant",
  }


@session_router.post(
  "/session",
)
def exchange_embed_capability(
  body: EmbedInstanceBody,
  authorization: str | None = Header(default=None),
  db: Session = Depends(get_db),
):
  """Atomically consume a bootstrap grant and return a chat-only session."""
  secret = _bearer_secret(authorization)
  token_hash = _secret_hash(secret)
  now = now_naive_utc()
  grant = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.token_hash == token_hash,
  ).first()
  if (
    grant is None
    or grant.revoked_at is not None
    or grant.consumed_at is not None
    or grant.expires_at <= now
    or grant.instance_id != body.instance_id
  ):
    raise HTTPException(status_code=401, detail="Embed bootstrap grant is invalid.")
  owner = db.query(models.Owner).first()
  app = db.query(models.App).filter(
    models.App.id == grant.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  chat = db.query(models.Chat).filter(
    models.Chat.id == grant.chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if (
    owner is None
    or owner.token_epoch != grant.owner_epoch
    or app is None
    or app.token_nonce != grant.app_nonce
    or chat is None
    or chat.created_by_app_id != grant.app_id
  ):
    raise HTTPException(status_code=401, detail="Embed bootstrap grant was revoked.")

  # A lost/slow response can make the parent mint and exchange a replacement
  # while this older request is still in flight. Grant creation order is the
  # handoff order: once a newer grant for the exact frame has been consumed,
  # an older grant may never finish later and revoke/replace it. A merely
  # minted replacement intentionally does not supersede the still-valid session.
  if not chat_embed_grant_is_latest_consumed(db, grant):
    grant.revoked_at = now
    db.commit()
    raise HTTPException(status_code=401, detail="Embed bootstrap grant was superseded.")

  session_id = secrets.token_hex(24)
  session_expires_at = now + SESSION_TTL
  consumed = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.token_hash == token_hash,
    models.ChatEmbedGrant.consumed_at.is_(None),
    models.ChatEmbedGrant.revoked_at.is_(None),
    models.ChatEmbedGrant.expires_at > now,
  ).update({
    models.ChatEmbedGrant.consumed_at: now,
    models.ChatEmbedGrant.session_id: session_id,
    models.ChatEmbedGrant.session_expires_at: session_expires_at,
  }, synchronize_session=False)
  if consumed != 1:
    db.rollback()
    raise HTTPException(status_code=401, detail="Embed bootstrap grant was replayed.")
  # The conditional consume and the eager older-row revoke are separate SQL
  # statements. Re-read after stamping so an older transaction that overlapped
  # a newer successful exchange returns 401 instead of a stale session token.
  db.expire(grant)
  grant = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.token_hash == token_hash,
  ).one()
  if not chat_embed_grant_is_latest_consumed(db, grant):
    grant.revoked_at = now
    db.commit()
    raise HTTPException(status_code=401, detail="Embed bootstrap grant was superseded.")
  # A successful refresh supersedes every older session for this exact embed
  # instance. The old session remains valid until this atomic handoff succeeds,
  # then becomes unusable on its very next server request.
  db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.app_id == grant.app_id,
    models.ChatEmbedGrant.chat_id == grant.chat_id,
    models.ChatEmbedGrant.instance_id == grant.instance_id,
    models.ChatEmbedGrant.session_id.isnot(None),
    models.ChatEmbedGrant.id < grant.id,
    models.ChatEmbedGrant.session_id != session_id,
    models.ChatEmbedGrant.revoked_at.is_(None),
  ).update({models.ChatEmbedGrant.revoked_at: now}, synchronize_session=False)
  # This endpoint deliberately does not use the generic Sec-Fetch/Origin
  # guard. The caller is an opaque sandbox (Origin: null), and browser request
  # metadata is not an authorization boundary. The unguessable, one-use
  # server-stored bearer is the authority; CORS still prevents response reads
  # by unrelated web origins.
  claims = {
    "app_id": grant.app_id,
    "app_nonce": grant.app_nonce,
    "chat_id": grant.chat_id,
    "instance_id": grant.instance_id,
    "role": grant.role,
    "operations": list(grant.operations_json or []),
  }
  db.commit()

  token = auth.create_chat_embed_session_token(
    owner_username=owner.username,
    token_epoch=owner.token_epoch,
    app_id=claims["app_id"],
    app_nonce=claims["app_nonce"],
    chat_id=claims["chat_id"],
    instance_id=claims["instance_id"],
    session_id=session_id,
    role=claims["role"],
    operations=claims["operations"],
    expires_delta=SESSION_TTL,
  )
  return {
    "token": token,
    "chat_id": claims["chat_id"],
    "app_id": claims["app_id"],
    "instance_id": claims["instance_id"],
    "role": claims["role"],
    "operations": claims["operations"],
    "expires_at": session_expires_at.isoformat() + "Z",
    # Theme data is already visible to the owning app frame. Returning it only
    # after the one-use exchange lets the nested renderer match the shell
    # without granting its narrow session access to the generic theme route.
    "theme": theme_data(get_settings().data_dir),
  }


@app_router.delete(
  "/{chat_id}/embed-sessions/{instance_id}",
  dependencies=[Depends(reject_cross_site)],
)
def revoke_embed_sessions(
  chat_id: str,
  instance_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Revoke active/pending sessions for an app-owned frame instance."""
  if principal.scope != "app" or principal.app_id is None:
    raise HTTPException(status_code=403, detail="Only an app token may revoke embeds.")
  get_active_chat_for_principal(db, chat_id, principal)
  now = now_naive_utc()
  count = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.app_id == principal.app_id,
    models.ChatEmbedGrant.chat_id == chat_id,
    models.ChatEmbedGrant.instance_id == instance_id,
    models.ChatEmbedGrant.revoked_at.is_(None),
  ).update({models.ChatEmbedGrant.revoked_at: now}, synchronize_session=False)
  db.commit()
  return {"revoked": count}


router.include_router(app_router)
router.include_router(session_router)
