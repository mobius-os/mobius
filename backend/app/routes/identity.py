"""Narrow deployment-to-account-service bridge for the Identity system app.

The mini-app receives only its ordinary scoped bearer. This route owns the
managed instance credential and forwards a deliberately small profile contract
to the configured account service, which remains authoritative for globally
unique handles, avatar storage, and the cross-deployment inventory.
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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
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


def _local_payload(
  *, account_mode: str = "signed_out", account_unavailable: bool = False,
) -> dict:
  settings = get_settings()
  current = {
    "id": settings.mobius_sso_instance_id or "local",
    "name": "This Möbius",
    "status": "Active",
    "current": True,
    "url": settings.frontend_origin.rstrip("/"),
  }
  return {
    "account_mode": account_mode,
    "account_unavailable": account_unavailable,
    "instance_id": settings.mobius_sso_instance_id or None,
    # A local owner is not a mobius.you identity. A null profile makes the
    # signed-out privacy boundary explicit instead of encoding it as null
    # fields that clients must infer.
    "profile": None,
    "deployments": [current],
  }


def _remote_headers() -> dict[str, str]:
  settings = get_settings()
  return {
    "Authorization": f"Bearer {settings.mobius_sso_client_secret}",
    "X-Mobius-Instance-Id": settings.mobius_sso_instance_id,
    "Accept": "application/json",
  }


def _identity_contract(payload: object) -> dict:
  if not isinstance(payload, dict):
    raise HTTPException(502, "The Möbius account service returned an invalid response.")
  if not isinstance(payload.get("profile"), dict):
    raise HTTPException(502, "The Möbius account service returned an invalid profile.")
  deployments = payload.get("deployments")
  if not isinstance(deployments, list) or not all(
    isinstance(item, dict) for item in deployments
  ):
    raise HTTPException(502, "The Möbius account service returned an invalid deployment list.")
  return payload


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
    raise HTTPException(502, "The Möbius account service could not be reached.")
  if response.status_code == 409:
    detail = "That handle is already taken."
    try:
      detail = response.json().get("detail") or detail
    except ValueError:
      pass
    raise HTTPException(409, detail)
  if response.status_code not in (200, 201):
    raise HTTPException(502, "The Möbius account service could not complete that request.")
  try:
    payload = response.json()
  except ValueError:
    raise HTTPException(502, "The Möbius account service returned an invalid response.")
  return _identity_contract(payload)


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
  try:
    token = _open(link.access_token_encrypted)
  except HTTPException:
    # A rotated SECRET_KEY or damaged ciphertext cannot be recovered and must
    # not strand the owner behind an unlink operation that also cannot decrypt.
    db.delete(link)
    db.commit()
    return None
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.request(
        method,
        get_settings().mobius_account_origin + _ACCOUNT_PATH + suffix,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        **kwargs,
      )
  except httpx.HTTPError:
    raise HTTPException(502, "The Möbius account service could not be reached.")
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
    raise HTTPException(502, "The Möbius account service could not complete that request.")
  try:
    payload = response.json()
  except ValueError:
    raise HTTPException(502, "The Möbius account service returned an invalid response.")
  return _identity_contract(payload)


def _merge_local_deployment(payload: dict) -> dict:
  local = _local_payload()
  remote_deployments = payload.get("deployments")
  deployments = list(remote_deployments) if isinstance(remote_deployments, list) else []
  deployments = [item for item in deployments if isinstance(item, dict)]
  local_deployment = local["deployments"][0]
  local_url = local_deployment["url"].rstrip("/")
  deployments = [
    item for item in deployments
    if str(item.get("url") or "").rstrip("/") != local_url
  ]
  deployments.append(local_deployment)
  return {
    "account_mode": "linked",
    "account_unavailable": False,
    "instance_id": None,
    "profile": (
      payload.get("profile") if isinstance(payload.get("profile"), dict) else None
    ),
    "deployments": deployments,
  }


def _managed_payload(payload: dict, owner: models.Owner) -> dict:
  # The local SSO binding is authoritative for the user and instance. Copy the
  # remote profile before adding those fields so response objects are never
  # mutated in place and can be reused safely by tests/adapters.
  profile = dict(payload["profile"])
  profile["user_id"] = owner.sso_subject
  profile["email"] = owner.sso_email
  return {
    "account_mode": "managed",
    "account_unavailable": False,
    "instance_id": get_settings().mobius_sso_instance_id,
    "profile": profile,
    "deployments": list(payload["deployments"]),
  }


@router.get("")
async def read_identity(
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  local = _local_payload()
  if not get_settings().mobius_sso_enabled:
    had_link = _linked_row(db, owner.id) is not None
    try:
      linked = await _linked_remote(db, owner.id, "GET")
    except HTTPException as exc:
      if exc.status_code == 502 and had_link:
        return _local_payload(
          account_mode="linked", account_unavailable=True,
        )
      raise
    return _merge_local_deployment(linked) if linked is not None else local
  try:
    remote = await _managed_remote("GET")
  except HTTPException as exc:
    if exc.status_code != 502:
      raise
    degraded = _local_payload(
      account_mode="managed", account_unavailable=True,
    )
    degraded["profile"] = {
      "user_id": owner.sso_subject,
      "email": owner.sso_email,
      "display_name": None,
      "handle": None,
      "avatar_url": None,
    }
    return degraded
  return _managed_payload(remote, owner)


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
    remote = await _managed_remote("PATCH", "/profile", json={"handle": handle})
    return _managed_payload(remote, owner)
  remote = await _linked_remote(
    db, owner.id, "PATCH", "/profile", json={"handle": handle},
  )
  if remote is None:
    raise HTTPException(409, "Sign in to edit your Möbius profile.")
  return _merge_local_deployment(remote)


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
    remote = await _managed_remote("POST", "/avatar", files=files)
    return _managed_payload(remote, owner)
  remote = await _linked_remote(db, owner.id, "POST", "/avatar", files=files)
  if remote is None:
    raise HTTPException(409, "Sign in to edit your Möbius profile.")
  return _merge_local_deployment(remote)


@router.post("/link/start")
async def start_link(
  body: LinkStart,
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  if get_settings().mobius_sso_enabled:
    raise HTTPException(409, "This Möbius is already connected.")
  if _linked_row(db, owner.id) is not None:
    raise HTTPException(409, "Disconnect the current account before linking another.")
  client_origin = get_settings().mobius_account_client_origin
  if not client_origin:
    raise HTTPException(
      409,
      "Set MOBIUS_ACCOUNT_CLIENT_ORIGIN to this Möbius's public HTTPS origin.",
    )
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
    "client_origin": client_origin,
  })
  authorization_url = f"{get_settings().mobius_account_origin}/connect/mobius?{query}"
  verifier_encrypted = _seal(verifier)
  created_at = now_naive_utc()
  statement = sqlite_insert(models.IdentityLinkAttempt).values(
    owner_id=owner.id,
    attempt_id=attempt_id,
    state_digest=_digest(state),
    verifier_encrypted=verifier_encrypted,
    expires_at=expires_at,
    created_at=created_at,
  ).on_conflict_do_update(
    index_elements=[models.IdentityLinkAttempt.owner_id],
    set_={
      "attempt_id": attempt_id,
      "state_digest": _digest(state),
      "verifier_encrypted": verifier_encrypted,
      "expires_at": expires_at,
      "created_at": created_at,
    },
  )
  db.execute(statement)
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
  if _linked_row(db, owner.id) is not None:
    raise HTTPException(409, "Disconnect the current account before linking another.")
  try:
    verifier = _open(attempt.verifier_encrypted)
  except HTTPException:
    db.delete(attempt)
    db.commit()
    raise
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.post(
        get_settings().mobius_account_origin + "/api/account-links/token",
        json={"code": body.code, "code_verifier": verifier},
        headers={"Accept": "application/json"},
      )
  except httpx.HTTPError:
    raise HTTPException(
      502, "The Möbius account service could not be reached. Try completion again."
    )
  if response.status_code != 200:
    if response.status_code == 400:
      db.delete(attempt)
      db.commit()
    raise HTTPException(400, "That sign-in could not be completed. Please try again.")
  try:
    grant = response.json()
  except ValueError:
    raise HTTPException(502, "The Möbius account service returned an invalid response.")
  token = grant.get("access_token") if isinstance(grant, dict) else None
  scope = grant.get("scope") if isinstance(grant, dict) else None
  identity = grant.get("identity") if isinstance(grant, dict) else None
  if (
    not isinstance(token, str)
    or len(token) < 32
    or scope != _LINK_SCOPE
  ):
    raise HTTPException(502, "The Möbius account service returned an invalid account grant.")
  identity = _identity_contract(identity)
  link = models.IdentityAccountLink(owner_id=owner.id)
  db.add(link)
  link.access_token_encrypted = _seal(token)
  link.scopes_json = scope.split()
  link.linked_at = now_naive_utc()
  db.delete(attempt)
  try:
    db.commit()
  except IntegrityError:
    # A duplicate completion can race after the remote idempotent exchange.
    # It is successful only when the winner stored the exact same grant.
    db.rollback()
    winner = _linked_row(db, owner.id)
    if winner is None or not hmac.compare_digest(
      _open(winner.access_token_encrypted), token,
    ):
      raise HTTPException(409, "Another account link completed first.")
  return _merge_local_deployment(identity)


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
  try:
    token = _open(link.access_token_encrypted)
  except HTTPException:
    db.delete(link)
    db.commit()
    return Response(status_code=204)
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.post(
        get_settings().mobius_account_origin + "/api/account-links/revoke",
        headers={"Authorization": f"Bearer {token}"},
      )
  except httpx.HTTPError:
    raise HTTPException(
      502, "The Möbius account service could not be reached; your link was kept."
    )
  if response.status_code not in (204, 401):
    raise HTTPException(502, "The Möbius account service could not revoke this link.")
  db.delete(link)
  db.commit()
  return Response(status_code=204)
