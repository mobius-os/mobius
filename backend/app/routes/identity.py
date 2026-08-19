"""Narrow deployment-to-launcher bridge for the Identity system app.

The mini-app receives only its ordinary scoped bearer. This route owns the
managed instance credential and forwards a deliberately small profile contract
to Möbius Launch, which remains authoritative for globally unique handles,
avatar storage, and the cross-deployment inventory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import get_db
from app.deps import get_owner_or_app_with_identity_manage, reject_cross_site
from app.timeutil import now_naive_utc

router = APIRouter(
  prefix="/api/identity",
  tags=["identity"],
  dependencies=[Depends(reject_cross_site)],
)

_REMOTE_PATH = "/api/instance/v1/identity"
_ACCOUNT_ORIGIN = "https://www.mobius.you"
_ACCOUNT_PATH = "/api/account/v1/identity"
_LINK_SCOPE = "identity:read identity:write deployments:read"
_LINK_TTL = timedelta(minutes=10)
_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProfilePatch(BaseModel):
  handle: str


class LinkStart(BaseModel):
  provider: str


class LinkComplete(BaseModel):
  code: str
  state: str
  attempt: str


def _link_fernet() -> Fernet:
  material = f"mobius-identity-link-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def _seal(value: str) -> str:
  return _link_fernet().encrypt(value.encode()).decode()


def _open(value: str) -> str:
  try:
    return _link_fernet().decrypt(value.encode()).decode()
  except (InvalidToken, ValueError) as exc:
    raise HTTPException(409, "Sign in again to reconnect your account.") from exc


def _digest(value: str) -> str:
  return hashlib.sha256(value.encode()).hexdigest()


def _local_payload() -> dict:
  settings = get_settings()
  current = {
    "id": settings.mobius_sso_instance_id or "local",
    "name": "This Möbius",
    "status": "Active",
    "current": True,
    "url": settings.frontend_origin.rstrip("/"),
  }
  return {
    "managed": settings.mobius_sso_enabled,
    "instance_id": settings.mobius_sso_instance_id or None,
    "profile": {
      # A local owner is not a mobius.you identity. Keep the shape stable for
      # the app without leaking the installation login name into its signed-out
      # account surface. Managed identity below replaces these nulls only after
      # the SSO binding has been authenticated server-side.
      "user_id": None,
      "email": None,
      "display_name": None,
      "username": None,
      "handle": None,
      "avatar_url": None,
    },
    "deployments": [current],
  }


def _remote_headers() -> dict[str, str]:
  settings = get_settings()
  return {
    "Authorization": f"Bearer {settings.mobius_sso_client_secret}",
    "X-Mobius-Instance-Id": settings.mobius_sso_instance_id,
    "Accept": "application/json",
  }


async def _managed_remote(method: str, suffix: str = "", **kwargs) -> dict:
  settings = get_settings()
  if not settings.mobius_sso_enabled:
    raise HTTPException(409, "This Möbius uses a local account.")
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.request(
        method,
        settings.mobius_sso_issuer + _REMOTE_PATH + suffix,
        headers=_remote_headers(),
        **kwargs,
      )
  except httpx.HTTPError:
    raise HTTPException(502, "mobius.you could not be reached.")
  if response.status_code == 409:
    detail = "That handle is already taken."
    try:
      detail = response.json().get("detail") or detail
    except ValueError:
      pass
    raise HTTPException(409, detail)
  if response.status_code not in (200, 201):
    raise HTTPException(502, "mobius.you could not complete that request.")
  try:
    payload = response.json()
  except ValueError:
    raise HTTPException(502, "mobius.you returned an invalid response.")
  if not isinstance(payload, dict):
    raise HTTPException(502, "mobius.you returned an invalid response.")
  return payload


def _linked_row(db: Session, owner_id: int) -> models.IdentityAccountLink | None:
  return db.query(models.IdentityAccountLink).filter(
    models.IdentityAccountLink.owner_id == owner_id,
  ).one_or_none()


async def _linked_remote(
  db: Session,
  owner_id: int,
  method: str,
  suffix: str = "",
  **kwargs,
) -> dict | None:
  link = _linked_row(db, owner_id)
  if link is None:
    return None
  token = _open(link.access_token_encrypted)
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.request(
        method,
        _ACCOUNT_ORIGIN + _ACCOUNT_PATH + suffix,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        **kwargs,
      )
  except httpx.HTTPError:
    raise HTTPException(502, "mobius.you could not be reached.")
  if response.status_code == 401:
    db.delete(link)
    db.commit()
    return None
  if response.status_code == 409:
    detail = "That handle is already taken."
    try:
      detail = response.json().get("detail") or detail
    except ValueError:
      pass
    raise HTTPException(409, detail)
  if response.status_code not in (200, 201):
    raise HTTPException(502, "mobius.you could not complete that request.")
  try:
    payload = response.json()
  except ValueError:
    raise HTTPException(502, "mobius.you returned an invalid response.")
  if not isinstance(payload, dict):
    raise HTTPException(502, "mobius.you returned an invalid response.")
  return payload


def _merge_local_deployment(payload: dict) -> dict:
  local = _local_payload()
  remote_deployments = payload.get("deployments")
  deployments = list(remote_deployments) if isinstance(remote_deployments, list) else []
  deployments = [item for item in deployments if isinstance(item, dict)]
  deployments.append(local["deployments"][0])
  return {
    "managed": False,
    "instance_id": None,
    "profile": payload.get("profile") if isinstance(payload.get("profile"), dict) else {},
    "deployments": deployments,
  }


@router.get("")
async def read_identity(
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  local = _local_payload()
  if not get_settings().mobius_sso_enabled:
    linked = await _linked_remote(db, owner.id, "GET")
    return _merge_local_deployment(linked) if linked is not None else local
  remote = await _managed_remote("GET")
  # The local binding is the authority for which user/instance this deployment
  # represents. The launcher supplies editable/global fields and inventory.
  profile = remote.get("profile") if isinstance(remote.get("profile"), dict) else {}
  profile["user_id"] = owner.sso_subject
  profile["email"] = owner.sso_email
  return {
    "managed": True,
    "instance_id": get_settings().mobius_sso_instance_id,
    "profile": profile,
    "deployments": remote.get("deployments") if isinstance(remote.get("deployments"), list) else [],
  }


@router.patch("/profile")
async def update_profile(
  body: ProfilePatch,
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  handle = body.handle.strip().lower()
  if not re.fullmatch(r"[a-z0-9_]{3,30}", handle):
    raise HTTPException(422, "Use 3–30 letters, numbers, or underscores.")
  if get_settings().mobius_sso_enabled:
    await _managed_remote("PATCH", "/profile", json={"handle": handle})
  elif await _linked_remote(db, owner.id, "PATCH", "/profile", json={"handle": handle}) is None:
    raise HTTPException(409, "Sign in to edit your Möbius profile.")
  return await read_identity(owner, db)


@router.post("/avatar")
async def update_avatar(
  avatar: UploadFile = File(...),
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  content_type = (avatar.content_type or "").lower()
  if content_type not in _AVATAR_TYPES:
    raise HTTPException(415, "Choose a JPEG, PNG, or WebP image.")
  content = await avatar.read(_AVATAR_MAX_BYTES + 1)
  if not content or len(content) > _AVATAR_MAX_BYTES:
    raise HTTPException(413, "Profile pictures must be 5 MB or smaller.")
  files = {"avatar": (avatar.filename or "avatar", content, content_type)}
  if get_settings().mobius_sso_enabled:
    await _managed_remote("POST", "/avatar", files=files)
  elif await _linked_remote(db, owner.id, "POST", "/avatar", files=files) is None:
    raise HTTPException(409, "Sign in to edit your Möbius profile.")
  return await read_identity(owner, db)


@router.post("/link/start")
async def start_link(
  body: LinkStart,
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  if get_settings().mobius_sso_enabled:
    raise HTTPException(409, "This Möbius is already connected.")
  provider = body.provider.strip().lower()
  if provider not in {"google", "apple"}:
    raise HTTPException(422, "Choose Google or Apple.")
  attempt_id = secrets.token_urlsafe(24)
  state = secrets.token_urlsafe(32)
  verifier = secrets.token_urlsafe(64)
  challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest(),
  ).rstrip(b"=").decode()
  expires_at = now_naive_utc() + _LINK_TTL
  query = urlencode({
    "state": state,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "provider": provider,
  })
  authorization_url = f"{_ACCOUNT_ORIGIN}/connect/mobius?{query}"
  # Do not send the owner into a broken cross-origin popup while the matching
  # mobius.you flow is unavailable. A real authorization page may render
  # directly or redirect to its provider; both are valid readiness outcomes.
  try:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
      authorization = await client.get(
        authorization_url,
        headers={"Accept": "text/html"},
      )
  except httpx.HTTPError:
    raise HTTPException(
      503, "mobius.you sign-in is temporarily unavailable. Try again later."
    )
  if authorization.status_code < 200 or authorization.status_code >= 400:
    raise HTTPException(
      503, "mobius.you sign-in is not available yet. Try again later."
    )
  existing = db.query(models.IdentityLinkAttempt).filter(
    models.IdentityLinkAttempt.owner_id == owner.id,
  ).one_or_none()
  if existing is not None:
    db.delete(existing)
    db.flush()
  db.add(models.IdentityLinkAttempt(
    owner_id=owner.id,
    attempt_id=attempt_id,
    state_digest=_digest(state),
    verifier_encrypted=_seal(verifier),
    expires_at=expires_at,
  ))
  db.commit()
  return {
    "authorization_url": authorization_url,
    "attempt": attempt_id,
    "state": state,
    "expires_at": expires_at.isoformat() + "Z",
  }


@router.post("/link/complete")
async def complete_link(
  body: LinkComplete,
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  if get_settings().mobius_sso_enabled:
    raise HTTPException(409, "This Möbius is already connected.")
  attempt = db.query(models.IdentityLinkAttempt).filter(
    models.IdentityLinkAttempt.owner_id == owner.id,
    models.IdentityLinkAttempt.attempt_id == body.attempt,
  ).one_or_none()
  valid = bool(
    attempt
    and attempt.expires_at >= now_naive_utc()
    and hmac.compare_digest(attempt.state_digest, _digest(body.state))
  )
  if not valid:
    raise HTTPException(400, "That sign-in attempt expired. Please try again.")
  # The conditional DELETE is the consume operation. Two concurrent requests
  # may both have observed the row above, but only one can receive its verifier
  # and proceed to the host exchange.
  verifier_encrypted = db.execute(
    delete(models.IdentityLinkAttempt).where(
      models.IdentityLinkAttempt.owner_id == owner.id,
      models.IdentityLinkAttempt.attempt_id == body.attempt,
      models.IdentityLinkAttempt.state_digest == _digest(body.state),
      models.IdentityLinkAttempt.expires_at >= now_naive_utc(),
    ).returning(models.IdentityLinkAttempt.verifier_encrypted)
  ).scalar_one_or_none()
  db.commit()
  if verifier_encrypted is None:
    raise HTTPException(400, "That sign-in attempt expired. Please try again.")
  verifier = _open(verifier_encrypted)
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.post(
        _ACCOUNT_ORIGIN + "/api/account-links/token",
        json={"code": body.code, "code_verifier": verifier},
        headers={"Accept": "application/json"},
      )
  except httpx.HTTPError:
    raise HTTPException(502, "mobius.you could not be reached. Start sign-in again.")
  if response.status_code != 200:
    raise HTTPException(400, "That sign-in could not be completed. Please try again.")
  try:
    grant = response.json()
  except ValueError:
    raise HTTPException(502, "mobius.you returned an invalid response.")
  token = grant.get("access_token") if isinstance(grant, dict) else None
  scope = grant.get("scope") if isinstance(grant, dict) else None
  if not isinstance(token, str) or len(token) < 32 or scope != _LINK_SCOPE:
    raise HTTPException(502, "mobius.you returned an invalid account grant.")
  link = _linked_row(db, owner.id)
  if link is None:
    link = models.IdentityAccountLink(owner_id=owner.id)
    db.add(link)
  link.access_token_encrypted = _seal(token)
  link.scopes_json = scope.split()
  link.linked_at = now_naive_utc()
  db.commit()
  linked = await _linked_remote(db, owner.id, "GET")
  if linked is None:
    raise HTTPException(502, "The new account link could not be verified.")
  return _merge_local_deployment(linked)


@router.delete("/link", status_code=204)
async def delete_link(
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  if get_settings().mobius_sso_enabled:
    raise HTTPException(409, "Managed Möbius accounts cannot be unlinked here.")
  link = _linked_row(db, owner.id)
  if link is None:
    return Response(status_code=204)
  token = _open(link.access_token_encrypted)
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.post(
        _ACCOUNT_ORIGIN + "/api/account-links/revoke",
        headers={"Authorization": f"Bearer {token}"},
      )
  except httpx.HTTPError:
    raise HTTPException(502, "mobius.you could not be reached; your link was kept.")
  if response.status_code != 204:
    raise HTTPException(502, "mobius.you could not revoke this account link.")
  db.delete(link)
  db.commit()
  return Response(status_code=204)
