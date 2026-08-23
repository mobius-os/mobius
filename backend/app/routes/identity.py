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
from urllib.parse import urlencode, urlparse

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
from app.deps import (
  get_owner_or_app_with_identity_manage,
  get_owner_or_app_with_railway_manage,
  reject_cross_site,
)
from app.timeutil import now_naive_utc

router = APIRouter(
  prefix="/api/identity",
  tags=["identity"],
  dependencies=[Depends(reject_cross_site)],
)

_REMOTE_PATH = "/api/instance/v1/identity"
_ACCOUNT_PATH = "/api/account/v1/identity"
_LINK_SCOPES = {
  "deployments:delete",
  "deployments:read",
  "identity:read",
  "identity:write",
  "railway:read",
  "railway:write",
}
_LINK_SCOPE = " ".join(sorted(_LINK_SCOPES))
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


class RailwayCreate(BaseModel):
  name: str
  managed_auth: bool = True
  cpu: int | None = None
  memory_mb: int | None = None
  volume_mb: int | None = None


class RailwayCompute(BaseModel):
  cpu: int | None = None
  memory_mb: int | None = None


class RailwayStorage(BaseModel):
  volume_mb: int


class RailwaySelectWorkspace(BaseModel):
  workspace_id: str


class RailwayConnectStart(BaseModel):
  replace: bool = False


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


def _railway_contract(payload: object) -> dict:
  if not isinstance(payload, dict) or set(payload) != {"connection", "instances"}:
    raise HTTPException(502, "The Möbius account service returned invalid Railway state.")
  connection = payload.get("connection")
  instances = payload.get("instances")
  if connection is not None and not isinstance(connection, dict):
    raise HTTPException(502, "The Möbius account service returned an invalid Railway connection.")
  if (
    not isinstance(instances, list)
    or len(instances) > 100
    or not all(isinstance(item, dict) for item in instances)
  ):
    raise HTTPException(502, "The Möbius account service returned invalid Railway deployments.")
  return payload


def _remote_avatar_url(payload: dict | None) -> str | None:
  """Return one safe HTTPS avatar URL from the trusted account response.

  Mini-app CSP intentionally disallows arbitrary external images. The bridge
  fetches the account host's selected avatar server-side instead, so moving the
  account service does not require weakening every app frame's browser policy.
  """
  profile = payload.get("profile") if isinstance(payload, dict) else None
  value = profile.get("avatar_url") if isinstance(profile, dict) else None
  if not isinstance(value, str) or len(value) > 2048:
    return None
  try:
    parsed = urlparse(value)
    port = parsed.port
  except ValueError:
    return None
  if (
    parsed.scheme != "https"
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or port not in (None, 443)
    or parsed.fragment
  ):
    return None
  origin = f"https://{parsed.hostname}"
  account_origins = {
    str(get_settings().mobius_account_origin).rstrip("/"),
    str(get_settings().mobius_sso_issuer).rstrip("/"),
  }
  google_avatar = parsed.hostname == "googleusercontent.com" or (
    parsed.hostname.endswith(".googleusercontent.com")
  )
  if origin not in account_origins and not google_avatar:
    return None
  return value


async def _avatar_bytes(url: str) -> tuple[bytes, str]:
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      async with client.stream(
        "GET", url, headers={"Accept": ", ".join(sorted(_AVATAR_TYPES))},
      ) as response:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if response.status_code != 200 or content_type not in _AVATAR_TYPES:
          raise HTTPException(502, "The Möbius account avatar is unavailable.")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
          size += len(chunk)
          if size > _AVATAR_MAX_BYTES:
            raise HTTPException(502, "The Möbius account avatar is too large.")
          chunks.append(chunk)
  except HTTPException:
    raise
  except httpx.HTTPError:
    raise HTTPException(502, "The Möbius account avatar could not be reached.")
  if not chunks:
    raise HTTPException(502, "The Möbius account avatar is empty.")
  return b"".join(chunks), content_type


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


@router.get("/avatar")
async def read_avatar(
  owner: models.Owner = Depends(get_owner_or_app_with_identity_manage),
  db: Session = Depends(get_db),
):
  if get_settings().mobius_sso_enabled:
    remote = await _managed_remote("GET")
  else:
    remote = await _linked_remote(db, owner.id, "GET")
  url = _remote_avatar_url(remote)
  if url is None:
    raise HTTPException(404, "This Möbius account has no profile picture.")
  content, content_type = await _avatar_bytes(url)
  return Response(
    content=content,
    media_type=content_type,
    headers={
      "Cache-Control": "private, max-age=300",
      "X-Content-Type-Options": "nosniff",
    },
  )


async def _managed_railway_remote() -> dict:
  settings = get_settings()
  if not settings.mobius_sso_enabled:
    raise HTTPException(409, "This Möbius uses a linked account.")
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.get(
        settings.mobius_sso_issuer + "/api/instance/v1/railway",
        headers=_remote_headers(),
      )
  except httpx.HTTPError:
    raise HTTPException(502, "The Möbius account service could not be reached.")
  if response.status_code != 200:
    raise HTTPException(502, "The Möbius account service could not read Railway state.")
  try:
    return _railway_contract(response.json())
  except ValueError:
    raise HTTPException(502, "The Möbius account service returned invalid Railway state.")


async def _linked_railway_remote(
  db: Session, owner_id: int,
) -> tuple[str, dict | None]:
  link = _linked_row(db, owner_id)
  if link is None:
    return "signed_out", None
  if not _LINK_SCOPES.issubset(set(link.scopes_json or [])):
    return "reconnect", None
  try:
    token = _open(link.access_token_encrypted)
  except HTTPException:
    db.delete(link)
    db.commit()
    return "signed_out", None
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.get(
        get_settings().mobius_account_origin + "/api/account/v1/railway",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
      )
  except httpx.HTTPError:
    return "unavailable", None
  if response.status_code == 401:
    db.delete(link)
    db.commit()
    return "signed_out", None
  if response.status_code == 403:
    return "reconnect", None
  if response.status_code != 200:
    return "unavailable", None
  try:
    return "available", _railway_contract(response.json())
  except ValueError:
    raise HTTPException(502, "The Möbius account service returned invalid Railway state.")


@router.get("/railway")
async def read_railway(
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  if get_settings().mobius_sso_enabled:
    try:
      payload = await _managed_railway_remote()
    except HTTPException as exc:
      if exc.status_code == 502:
        return {"railway_access": "unavailable", "connection": None, "instances": []}
      raise
    return {"railway_access": "available", **payload}
  access, payload = await _linked_railway_remote(db, owner.id)
  return {
    "railway_access": access,
    "connection": payload.get("connection") if payload else None,
    "instances": payload.get("instances", []) if payload else [],
  }


def _railway_instance_id(value: str) -> str:
  if not re.fullmatch(r"mob_[A-Za-z0-9_-]{3,80}", value):
    raise HTTPException(404, "That deployment was not found.")
  return value


async def _railway_proxy(
  db: Session,
  owner_id: int,
  method: str,
  suffix: str,
  *,
  json: dict | None = None,
) -> dict:
  """Proxy one Railway call to the account/instance host and return its JSON.

  Shared by every Railway mutation and connection-management route so the auth
  routing, scope gate, dropped-link recovery, and error mapping live in one
  place. Callers that expect a specific shape validate the returned dict.
  """
  settings = get_settings()
  if settings.mobius_sso_enabled:
    url = settings.mobius_sso_issuer + "/api/instance/v1/railway" + suffix
    headers = _remote_headers()
  else:
    link = _linked_row(db, owner_id)
    if link is None:
      raise HTTPException(409, "Sign in to manage Railway deployments.")
    if not _LINK_SCOPES.issubset(set(link.scopes_json or [])):
      raise HTTPException(
        409, "Reconnect this Möbius to approve Railway deployment access."
      )
    try:
      token = _open(link.access_token_encrypted)
    except HTTPException:
      # A link whose token can no longer be decrypted (e.g. SECRET_KEY drift) is
      # unusable; drop it so write paths self-heal like the read/401 paths do.
      db.delete(link)
      db.commit()
      raise HTTPException(409, "Sign in again to manage Railway deployments.")
    url = settings.mobius_account_origin + "/api/account/v1/railway" + suffix
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
  try:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
      response = await client.request(method, url, headers=headers, json=json)
  except httpx.HTTPError:
    raise HTTPException(502, "The Möbius account service could not be reached.")
  if response.status_code == 401 and not settings.mobius_sso_enabled:
    link = _linked_row(db, owner_id)
    if link is not None:
      db.delete(link)
      db.commit()
    raise HTTPException(409, "Sign in again to manage Railway deployments.")
  detail = "The Möbius account service could not complete that Railway action."
  try:
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
      detail = payload["detail"]
  except ValueError:
    payload = None
  if response.status_code not in (200, 202):
    status = response.status_code if response.status_code in {404, 409, 422} else 502
    raise HTTPException(status, detail)
  if not isinstance(payload, dict):
    raise HTTPException(502, "The Möbius account service returned invalid Railway state.")
  return payload


async def _railway_mutation(
  db: Session,
  owner_id: int,
  method: str,
  suffix: str,
  *,
  json: dict | None = None,
) -> dict:
  payload = await _railway_proxy(db, owner_id, method, suffix, json=json)
  instance = payload.get("instance")
  if not isinstance(instance, dict):
    raise HTTPException(502, "The Möbius account service returned invalid Railway state.")
  return {"instance": instance}


def _railway_workspaces_contract(payload: object) -> dict:
  if not isinstance(payload, dict) or set(payload) != {"workspaces", "current"}:
    raise HTTPException(502, "The Möbius account service returned invalid workspaces.")
  workspaces = payload.get("workspaces")
  current = payload.get("current")
  if (
    not isinstance(workspaces, list)
    or len(workspaces) > 100
    or any(
      not isinstance(item, dict)
      or set(item) != {"id", "name"}
      or not isinstance(item.get("id"), str)
      or not isinstance(item.get("name"), str)
      or len(item["id"]) > 128
      or len(item["name"]) > 128
      for item in workspaces
    )
    or (current is not None and (not isinstance(current, str) or len(current) > 128))
  ):
    raise HTTPException(502, "The Möbius account service returned invalid workspaces.")
  return payload


def _railway_metrics_contract(payload: object) -> dict:
  invalid = HTTPException(502, "The Möbius account service returned invalid Railway metrics.")
  if not isinstance(payload, dict) or set(payload) != {
    "runtime", "cpu", "memory", "volume", "network"
  }:
    raise invalid

  def _strings(source: object, keys: set[str], limit: int) -> None:
    if (
      not isinstance(source, dict)
      or set(source) != keys
      or any(not isinstance(source[key], str) or len(source[key]) > limit for key in keys)
    ):
      raise invalid

  _strings(
    payload["runtime"],
    {"status_label", "region_label", "latest_deployment_at", "data_status"},
    200,
  )
  _strings(payload["cpu"], {"label", "limit_label", "percent"}, 40)
  _strings(payload["memory"], {"label", "limit_label", "percent"}, 40)
  _strings(payload["volume"], {"used_label", "allocated_label", "percent"}, 40)
  _strings(payload["network"], {"rx_label", "tx_label", "percent"}, 40)
  return payload


def _railway_recovery_status_contract(payload: object) -> dict:
  invalid = HTTPException(502, "The Möbius account service returned invalid Recovery state.")
  if not isinstance(payload, dict) or set(payload) - {"state", "message", "error", "open_url"}:
    raise invalid
  state = payload.get("state")
  message = payload.get("message")
  error = payload.get("error")
  if (
    not isinstance(state, str) or len(state) > 32
    or not isinstance(message, str) or len(message) > 200
    or not isinstance(error, str) or len(error) > 360
  ):
    raise invalid
  open_url = payload.get("open_url")
  if open_url is not None:
    # The app opens this in a popup, so pin it to the account origin's exact
    # recovery-open path — never let the account host redirect us elsewhere.
    if not isinstance(open_url, str) or len(open_url) > 2048:
      raise invalid
    parsed = urlparse(open_url)
    if (
      parsed.scheme != "https"
      or parsed.fragment
      or f"{parsed.scheme}://{parsed.netloc}" != get_settings().mobius_account_origin
      or not parsed.path.endswith("/account/recovery/open")
    ):
      raise invalid
  return payload


async def _railway_connect_start(
  db: Session, owner_id: int, *, replace: bool = False
) -> dict:
  settings = get_settings()
  if settings.mobius_sso_enabled:
    url = settings.mobius_sso_issuer + "/api/instance/v1/railway/connect/start"
    headers = _remote_headers()
  else:
    link = _linked_row(db, owner_id)
    if link is None or not _LINK_SCOPES.issubset(set(link.scopes_json or [])):
      raise HTTPException(
        409, "Reconnect this Möbius to approve Railway deployment access."
      )
    try:
      token = _open(link.access_token_encrypted)
    except HTTPException:
      db.delete(link)
      db.commit()
      raise HTTPException(409, "Sign in again to reconnect Railway.")
    url = settings.mobius_account_origin + "/api/account/v1/railway/connect/start"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
  try:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
      response = await client.post(url, headers=headers, json={"replace": replace})
  except httpx.HTTPError:
    raise HTTPException(502, "The Möbius account service could not be reached.")
  if response.status_code != 200:
    raise HTTPException(502, "The Möbius account service could not start Railway sign-in.")
  try:
    authorization_url = response.json().get("authorization_url")
    parsed = urlparse(authorization_url)
  except (AttributeError, TypeError, ValueError):
    raise HTTPException(502, "The Möbius account service returned an invalid Railway address.")
  if (
    parsed.scheme != "https"
    or parsed.fragment
    or f"{parsed.scheme}://{parsed.netloc}" != settings.mobius_account_origin
    or not parsed.path.endswith("/railway/connect")
  ):
    raise HTTPException(502, "The Möbius account service returned an invalid Railway address.")
  return {"authorization_url": authorization_url}


@router.post("/railway/deployments", status_code=202)
async def create_railway_deployment(
  body: RailwayCreate,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  name = body.name.strip()
  if not name or len(name) > 80:
    raise HTTPException(422, "Use a deployment name between 1 and 80 characters.")
  return await _railway_mutation(
    db,
    owner.id,
    "POST",
    "/instances",
    json={
      "name": name,
      "managed_auth": body.managed_auth,
      "cpu": body.cpu,
      "memory_mb": body.memory_mb,
      "volume_mb": body.volume_mb,
    },
  )


@router.post("/railway/connect/start")
async def start_railway_connection(
  body: RailwayConnectStart | None = None,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return await _railway_connect_start(
    db, owner.id, replace=bool(body and body.replace)
  )


@router.get("/railway/workspaces")
async def read_railway_workspaces(
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return _railway_workspaces_contract(
    await _railway_proxy(db, owner.id, "GET", "/workspaces")
  )


@router.post("/railway/workspace")
async def select_railway_workspace(
  body: RailwaySelectWorkspace,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  workspace_id = body.workspace_id.strip()
  if not workspace_id or len(workspace_id) > 128:
    raise HTTPException(422, "Choose a Railway workspace.")
  await _railway_proxy(
    db, owner.id, "POST", "/workspace", json={"workspace_id": workspace_id}
  )
  return {"ok": True}


@router.post("/railway/plan/refresh")
async def refresh_railway_plan(
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  await _railway_proxy(db, owner.id, "POST", "/plan/refresh")
  return {"ok": True}


@router.post("/railway/disconnect")
async def disconnect_railway(
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  await _railway_proxy(db, owner.id, "POST", "/disconnect")
  return {"ok": True}


@router.get("/railway/deployments/{instance_id}/metrics")
async def read_railway_metrics(
  instance_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return _railway_metrics_contract(
    await _railway_proxy(
      db, owner.id, "GET", f"/instances/{_railway_instance_id(instance_id)}/metrics"
    )
  )


@router.post("/railway/deployments/{instance_id}/recovery", status_code=202)
async def open_railway_recovery(
  instance_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  payload = await _railway_proxy(
    db, owner.id, "POST", f"/instances/{_railway_instance_id(instance_id)}/recovery"
  )
  return {"state": str(payload.get("state") or "starting")}


@router.get("/railway/deployments/{instance_id}/recovery/status")
async def read_railway_recovery_status(
  instance_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return _railway_recovery_status_contract(
    await _railway_proxy(
      db, owner.id, "GET", f"/instances/{_railway_instance_id(instance_id)}/recovery/status"
    )
  )


@router.post("/railway/deployments/{instance_id}/retry", status_code=202)
async def retry_railway_deployment(
  instance_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return await _railway_mutation(
    db, owner.id, "POST", f"/instances/{_railway_instance_id(instance_id)}/retry"
  )


@router.patch("/railway/deployments/{instance_id}/compute")
async def update_railway_compute(
  instance_id: str,
  body: RailwayCompute,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return await _railway_mutation(
    db,
    owner.id,
    "PATCH",
    f"/instances/{_railway_instance_id(instance_id)}/compute",
    json={"cpu": body.cpu, "memory_mb": body.memory_mb},
  )


@router.patch("/railway/deployments/{instance_id}/storage")
async def update_railway_storage(
  instance_id: str,
  body: RailwayStorage,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return await _railway_mutation(
    db,
    owner.id,
    "PATCH",
    f"/instances/{_railway_instance_id(instance_id)}/storage",
    json={"volume_mb": body.volume_mb},
  )


@router.delete("/railway/deployments/{instance_id}", status_code=202)
async def delete_railway_deployment(
  instance_id: str,
  owner: models.Owner = Depends(get_owner_or_app_with_railway_manage),
  db: Session = Depends(get_db),
):
  return await _railway_mutation(
    db, owner.id, "DELETE", f"/instances/{_railway_instance_id(instance_id)}"
  )


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
