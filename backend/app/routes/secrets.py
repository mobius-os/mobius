"""Encrypted, app-scoped secret storage."""

import base64
import hashlib
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import fs_locks, models
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site

router = APIRouter(prefix="/api/apps", tags=["app-secrets"])

_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_SECRETS_PER_APP = 16


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
