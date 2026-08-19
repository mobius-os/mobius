"""Narrow deployment-to-launcher bridge for the Identity system app.

The mini-app receives only its ordinary scoped bearer. This route owns the
managed instance credential and forwards a deliberately small profile contract
to Möbius Launch, which remains authoritative for globally unique handles,
avatar storage, and the cross-deployment inventory.
"""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app import models
from app.config import get_settings
from app.deps import get_owner_or_app_with_identity_manage, reject_cross_site

router = APIRouter(
  prefix="/api/identity",
  tags=["identity"],
  dependencies=[Depends(reject_cross_site)],
)

_REMOTE_PATH = "/api/instance/v1/identity"
_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProfilePatch(BaseModel):
  handle: str


def _local_payload(owner: models.Owner) -> dict:
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
      "user_id": owner.sso_subject,
      "email": owner.sso_email,
      "display_name": owner.username,
      "username": owner.username,
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


async def _remote(method: str, suffix: str = "", **kwargs) -> dict:
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


@router.get("")
async def read_identity(
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
):
  local = _local_payload(owner)
  if not get_settings().mobius_sso_enabled:
    return local
  remote = await _remote("GET")
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
):
  handle = body.handle.strip().lower()
  if not re.fullmatch(r"[a-z0-9_]{3,30}", handle):
    raise HTTPException(422, "Use 3–30 letters, numbers, or underscores.")
  await _remote("PATCH", "/profile", json={"handle": handle})
  return await read_identity(owner)


@router.post("/avatar")
async def update_avatar(
  avatar: UploadFile = File(...),
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
):
  content_type = (avatar.content_type or "").lower()
  if content_type not in _AVATAR_TYPES:
    raise HTTPException(415, "Choose a JPEG, PNG, or WebP image.")
  content = await avatar.read(_AVATAR_MAX_BYTES + 1)
  if not content or len(content) > _AVATAR_MAX_BYTES:
    raise HTTPException(413, "Profile pictures must be 5 MB or smaller.")
  await _remote(
    "POST",
    "/avatar",
    files={"avatar": (avatar.filename or "avatar", content, content_type)},
  )
  return await read_identity(owner)
