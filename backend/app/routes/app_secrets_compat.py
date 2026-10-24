"""Deprecated: the retired per-app secret routes, answered by the key vault.

Apps written for ``/api/apps/{id}/secrets/{name}`` keep working for one
release while they move to the vault handshake (``/api/keys``). These routes
see only the app's own keys: the vault keys created for that app's secret
names (by the one-time import or by these routes), recorded with that origin.
A key the owner created or merely lets the app borrow is never visible or
writable here.

What changed for callers: an app may still save, check and delete its own
values, but no app token reads one back any more, including the app's own
service and jobs. They borrow the key through ``/api/keys/use`` instead. The
owner and the owner's agents may still read, as before.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import key_vault, models
from app.database import get_db
from app.deps import (
  Principal,
  get_principal,
  reject_cross_site,
  require_nondelegated_owner_or_app_control,
)

router = APIRouter(prefix="/api/apps", tags=["app-secrets (deprecated)"])

_DEPRECATION = {
  "Deprecation": "true",
  "Link": '</api/keys/requests>; rel="successor-version"',
  "Cache-Control": "no-store",
}


class SecretWrite(BaseModel):
  value: str = Field(min_length=1, max_length=8192)


def _authorize_app(db: Session, principal: Principal, app_id: int) -> models.App:
  if principal.scope not in ("owner", "app"):
    raise HTTPException(status_code=403, detail="This token cannot access app secrets.")
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(status_code=403, detail="Apps can access only their own secrets.")
  app = db.query(models.App).filter(
    models.App.id == app_id, models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


async def _keys() -> list[dict]:
  try:
    return (await key_vault.vault_call("GET", "/keys"))["keys"]
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc


async def _owned(app_id: int, name: str) -> dict:
  key = key_vault.owned_key_in(await _keys(), app_id, name)
  if key is None:
    raise HTTPException(status_code=404, detail="Secret not found.")
  return key


@router.put(
  "/{app_id}/secrets/{name}",
  status_code=204,
  dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)],
)
async def put_secret(
  app_id: int, name: str, body: SecretWrite,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  _authorize_app(db, principal, app_id)
  try:
    await key_vault.store_app_key(app_id, name, body.value)
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc
  return Response(status_code=204, headers=_DEPRECATION)


@router.head("/{app_id}/secrets/{name}")
async def secret_exists(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  _authorize_app(db, principal, app_id)
  await _owned(app_id, name)
  return Response(status_code=204, headers=_DEPRECATION)


@router.get("/{app_id}/secrets/{name}")
async def get_secret(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  if principal.app_id is not None:
    raise HTTPException(
      status_code=403,
      detail="Apps never read key values. Borrow the key through /api/keys/use "
      "(see 'Keys' in the building-apps guide).",
    )
  _authorize_app(db, principal, app_id)
  key = await _owned(app_id, name)
  caller = f"agent:{principal.chat_id}" if principal.chat_id else "owner"
  try:
    value = (await key_vault.vault_call("GET", f"/keys/{key['name']}/value", caller=caller))["value"]
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  return Response(content=value, media_type="text/plain", headers=_DEPRECATION)


@router.delete(
  "/{app_id}/secrets/{name}",
  status_code=204,
  dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)],
)
async def delete_secret(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  """Delete the app's key, or only its grant when other apps share the key."""
  _authorize_app(db, principal, app_id)
  try:
    released = await key_vault.release_app_key(app_id, name)
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc
  if released is None:
    raise HTTPException(status_code=404, detail="Secret not found.")
  return Response(status_code=204, headers=_DEPRECATION)
