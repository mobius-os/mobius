"""Encrypted, app-scoped secret storage."""

import base64
import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import fs_locks, models
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site
from app.net_utils import validate_url_safe

router = APIRouter(prefix="/api/apps", tags=["app-secrets"])

_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_SECRETS_PER_APP = 16
_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_CREDENTIAL_RESPONSE_LIMIT = 2 * 1024 * 1024


class SecretWrite(BaseModel):
  value: str = Field(min_length=1, max_length=8192)


def _authorize_app(db: Session, principal: Principal, app_id: int) -> models.App:
  # Generic authentication emits only the explicit owner/app principals. The
  # narrow chat/media scopes are denied before they can touch the secret store.
  if principal.scope not in ("owner", "app"):
    raise HTTPException(
      status_code=403, detail="This token cannot access app secrets."
    )
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(status_code=403, detail="Apps can access only their own secrets.")
  app = db.query(models.App).filter(
    models.App.id == app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


def _secret_path(app_id: int, name: str) -> Path:
  if not _SECRET_NAME_RE.fullmatch(name):
    raise HTTPException(status_code=400, detail="Invalid secret name.")
  return Path(get_settings().data_dir) / "app-secrets" / str(app_id) / name


def _fernet() -> Fernet:
  material = f"mobius-app-secret-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def _write_secret(path: Path, value: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  os.chmod(path.parent, 0o700)
  payload = _fernet().encrypt(value.encode())
  fd, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
  )
  try:
    with os.fdopen(fd, "wb") as file:
      file.write(payload)
      file.flush()
      os.fsync(file.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
  except BaseException:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise


def _secret_count(directory: Path) -> int:
  if not directory.is_dir():
    return 0
  return sum(
    1 for child in directory.iterdir()
    if child.is_file() and _SECRET_NAME_RE.fullmatch(child.name)
  )


def _credentialed_fetch_config(app: models.App, provider: str) -> dict:
  if not _PROVIDER_NAME_RE.fullmatch(provider):
    raise HTTPException(status_code=404, detail="Credentialed provider not found.")
  contract = app.capability_contract
  data = contract.get("data") if isinstance(contract, dict) else None
  providers = data.get("credentialed_fetch") if isinstance(data, dict) else None
  config = providers.get(provider) if isinstance(providers, dict) else None
  if not isinstance(config, dict):
    raise HTTPException(status_code=404, detail="Credentialed provider not found.")
  return config


def _path_is_allowed(path: str, allowed: list[str]) -> bool:
  for prefix in allowed:
    if prefix.endswith("/") and path.startswith(prefix):
      return True
    if path == prefix or path.startswith(prefix + "/"):
      return True
  return False


def _path_has_ambiguous_segments(path: str) -> bool:
  """Reject path spellings an upstream service could normalize later.

  The reviewed prefix check operates on URL text, while providers commonly
  percent-decode and remove dot segments before routing. Inspect every bounded
  decoding layer and reject any spelling that can become a dot segment or add
  a new separator after Möbius has attached the credential.
  """
  decoded = path
  for _ in range(len(path) + 1):
    if "\\" in decoded or any(
      segment in (".", "..") for segment in decoded.split("/")
    ):
      return True
    next_decoded = unquote(decoded)
    if next_decoded == decoded:
      return False
    if next_decoded.count("/") != decoded.count("/"):
      return True
    decoded = next_decoded
  return True


async def _credentialed_response(
  client: httpx.AsyncClient, request: httpx.Request,
) -> Response:
  try:
    upstream = await client.send(request, stream=True)
  except Exception:
    # Never include the upstream exception: httpx errors contain the complete
    # URL, including the injected credential.
    raise HTTPException(status_code=502, detail="Credentialed provider request failed.")
  try:
    body = bytearray()
    async for chunk in upstream.aiter_bytes():
      if len(body) + len(chunk) > _CREDENTIAL_RESPONSE_LIMIT:
        raise HTTPException(
          status_code=413, detail="Credentialed provider response is too large."
        )
      body.extend(chunk)
    return Response(
      content=bytes(body),
      status_code=upstream.status_code,
      media_type=upstream.headers.get("content-type", "application/octet-stream"),
      headers={"Cache-Control": "no-store"},
    )
  finally:
    await upstream.aclose()


@router.put(
  "/{app_id}/secrets/{name}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def put_secret(
  app_id: int,
  name: str,
  body: SecretWrite,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Creates or replaces one encrypted secret for an app."""
  app = _authorize_app(db, principal, app_id)
  expected_nonce = app.token_nonce
  path = _secret_path(app_id, name)
  async with fs_locks.app_storage_lock(app_id):
    db.expire_all()
    current = _authorize_app(db, principal, app_id)
    if current.token_nonce != expected_nonce:
      raise HTTPException(status_code=404, detail="App not found.")
    if not path.exists() and _secret_count(path.parent) >= _MAX_SECRETS_PER_APP:
      raise HTTPException(
        status_code=413,
        detail=f"An app may store at most {_MAX_SECRETS_PER_APP} secrets.",
      )
    _write_secret(path, body.value)
  return Response(status_code=204)


@router.head("/{app_id}/secrets/{name}")
async def secret_exists(
  app_id: int,
  name: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Reports whether a named secret exists without exposing its value."""
  app = _authorize_app(db, principal, app_id)
  expected_nonce = app.token_nonce
  path = _secret_path(app_id, name)
  async with fs_locks.app_storage_lock(app_id):
    db.expire_all()
    current = _authorize_app(db, principal, app_id)
    if current.token_nonce != expected_nonce:
      raise HTTPException(status_code=404, detail="App not found.")
    if not path.is_file():
      raise HTTPException(status_code=404, detail="Secret not found.")
  return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/{app_id}/secrets/{name}")
async def get_secret(
  app_id: int,
  name: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Returns one decrypted secret to the owner or an owner-scoped agent."""
  if principal.app_id is not None:
    raise HTTPException(
      status_code=403,
      detail="Apps may check or replace secrets but cannot read them back.",
    )
  app = _authorize_app(db, principal, app_id)
  expected_nonce = app.token_nonce
  path = _secret_path(app_id, name)
  async with fs_locks.app_storage_lock(app_id):
    db.expire_all()
    current = _authorize_app(db, principal, app_id)
    if current.token_nonce != expected_nonce:
      raise HTTPException(status_code=404, detail="App not found.")
    if not path.is_file():
      raise HTTPException(status_code=404, detail="Secret not found.")
    try:
      value = _fernet().decrypt(path.read_bytes()).decode()
    except (InvalidToken, OSError, UnicodeDecodeError):
      raise HTTPException(status_code=500, detail="Secret could not be read.")
  return Response(
    content=value,
    media_type="text/plain",
    headers={"Cache-Control": "no-store"},
  )


@router.get("/{app_id}/credentialed-fetch/{provider}")
async def credentialed_fetch(
  app_id: int,
  provider: str,
  url: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Fetch a manifest-allowlisted provider URL with an encrypted app secret.

  The caller supplies the provider URL without the credential. The server
  requires an exact HTTPS origin and an allowlisted path from mobius.json,
  injects the named secret into one declared query parameter or path prefix,
  and never returns or logs the credential. App tokens remain self-scoped.
  """
  app = _authorize_app(db, principal, app_id)
  config = _credentialed_fetch_config(app, provider)
  try:
    declared_origin = str(config["origin"])
    allowed_paths = list(config["paths"])
    secret_name = str(config["secret"])
  except (KeyError, TypeError, ValueError):
    raise HTTPException(status_code=404, detail="Credentialed provider not found.")

  query_parameter = config.get("query_parameter")
  path_prefix = config.get("path_prefix")
  if (query_parameter is None) == (path_prefix is None):
    raise HTTPException(status_code=404, detail="Credentialed provider not found.")
  if query_parameter is not None:
    query_parameter = str(query_parameter)
  if path_prefix is not None:
    path_prefix = str(path_prefix)

  parsed = urlparse(url)
  request_origin = f"{parsed.scheme}://{parsed.netloc}"
  if (
    parsed.scheme != "https"
    or parsed.username is not None
    or parsed.password is not None
    or request_origin != declared_origin
    or _path_has_ambiguous_segments(parsed.path)
    or not _path_is_allowed(parsed.path, allowed_paths)
  ):
    raise HTTPException(status_code=403, detail="Provider URL is not allowlisted.")
  query = parse_qsl(parsed.query, keep_blank_values=True)
  if query_parameter is not None and any(
    name == query_parameter for name, _ in query
  ):
    raise HTTPException(status_code=400, detail="Credential parameter must be omitted.")
  if path_prefix is not None and not parsed.path.startswith(path_prefix + "/"):
    raise HTTPException(status_code=400, detail="Credential path prefix is missing.")

  expected_nonce = app.token_nonce
  path = _secret_path(app_id, secret_name)
  async with fs_locks.app_storage_lock(app_id):
    db.expire_all()
    current = _authorize_app(db, principal, app_id)
    if current.token_nonce != expected_nonce:
      raise HTTPException(status_code=404, detail="App not found.")
    if not path.is_file():
      raise HTTPException(status_code=409, detail="Provider credential is not configured.")
    try:
      secret = _fernet().decrypt(path.read_bytes()).decode()
    except (InvalidToken, OSError, UnicodeDecodeError):
      raise HTTPException(status_code=500, detail="Provider credential could not be read.")

  if query_parameter is not None:
    credentialed_url = urlunparse(parsed._replace(query=urlencode([
      *query, (query_parameter, secret),
    ])))
  else:
    # Treat the secret as exactly one path-segment suffix. Encoding slash and
    # every other delimiter prevents a credential from changing the reviewed
    # origin/path shape; colon remains literal for provider tokens that use it.
    encoded_secret = quote(secret, safe="-._~:")
    injected_path = path_prefix + encoded_secret + parsed.path[len(path_prefix):]
    credentialed_url = urlunparse(parsed._replace(path=injected_path))
  pinned_url, host_header, sni_host = validate_url_safe(credentialed_url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    request = client.build_request("GET", pinned_url)
    request.headers["host"] = host_header
    request.extensions["sni_hostname"] = sni_host
    return await _credentialed_response(client, request)


@router.delete(
  "/{app_id}/secrets/{name}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def delete_secret(
  app_id: int,
  name: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Deletes one app secret."""
  app = _authorize_app(db, principal, app_id)
  expected_nonce = app.token_nonce
  path = _secret_path(app_id, name)
  async with fs_locks.app_storage_lock(app_id):
    db.expire_all()
    current = _authorize_app(db, principal, app_id)
    if current.token_nonce != expected_nonce:
      raise HTTPException(status_code=404, detail="App not found.")
    if not path.is_file():
      raise HTTPException(status_code=404, detail="Secret not found.")
    path.unlink()
  return Response(status_code=204)
