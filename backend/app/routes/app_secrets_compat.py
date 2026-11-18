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
owner and the owner's top-level agents may still read, as before, but no
longer write here: a value they write is the owner's key, which an app uses
only by the owner's own grant, so they write through ``/api/keys/<name>``.
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
from app.resource_access import authorized_app_nonce
from app.routes import keys as keys_routes

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
  with keys_routes.vault_outcomes_as_http():
    return (await key_vault.vault_call("GET", "/keys"))["keys"]


def _only_the_apps_own_token(principal: Principal) -> None:
  """Writes here stay the app's own: one by the owner or an agent would hand
  the app a value it never chose, as its own key, without the owner's grant."""
  if principal.app_id is None:
    raise HTTPException(
      status_code=403,
      detail="Only the app itself saves or deletes its secrets here. Manage keys in "
      "Settings › Keys or with PUT /api/keys/<name>.",
    )


def _install(principal: Principal, app: models.App) -> str:
  """The installation a call is for: the one an app token was checked
  against, or for the owner and agents the app row the route just selected."""
  if principal.app_id is None:
    return app.install_id
  if principal.app_install_id is None:
    raise HTTPException(status_code=401, detail="App token is not bound to an installation.")
  return principal.app_install_id


async def _owned(app_id: int, install: str, name: str) -> dict:
  key = key_vault.owned_key_in(await _keys(), app_id, install, name)
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
  _only_the_apps_own_token(principal)
  app = _authorize_app(db, principal, app_id)
  try:
    with keys_routes.vault_outcomes_as_http():
      async with key_vault.for_live_app(app_id, authorized_app_nonce(principal, app)):
        await key_vault.store_app_key(app_id, _install(principal, app), name, body.value)
  except key_vault.AppGone:
    raise HTTPException(status_code=404, detail="App not found.") from None
  return Response(status_code=204, headers=_DEPRECATION)


@router.head("/{app_id}/secrets/{name}")
async def secret_exists(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  app = _authorize_app(db, principal, app_id)
  await _owned(app_id, _install(principal, app), name)
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
  # The same caller rule as /api/keys/{name}/value: a shared-browser guest or
  # a delegated helper never reads a key, and the read is logged as the agent
  # or owner it is.
  caller = keys_routes.reader_caller(principal)
  app = _authorize_app(db, principal, app_id)
  key = await _owned(app_id, app.install_id, name)
  with keys_routes.vault_outcomes_as_http():
    value = (await key_vault.vault_call("GET", f"/keys/{key['name']}/value", caller=caller))["value"]
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
  """Delete the app's key, or only its grant when other apps share the key.

  A value still waiting in the retired per-app store is withdrawn from the
  import too, so the next start never brings it back.
  """
  _only_the_apps_own_token(principal)
  app = _authorize_app(db, principal, app_id)
  try:
    # Under the app's lock and its authorized installation, as for a save: a
    # delete delayed past a purge must not remove the key of the app that
    # reused the id.
    with keys_routes.vault_outcomes_as_http():
      async with key_vault.for_live_app(app_id, authorized_app_nonce(principal, app)):
        released = await key_vault.release_app_key(app_id, _install(principal, app), name)
        withdrawn = key_vault.withdraw_legacy_secret(app_id, name)
  except key_vault.AppGone:
    raise HTTPException(status_code=404, detail="App not found.") from None
  if released is None and not withdrawn:
    raise HTTPException(status_code=404, detail="Secret not found.")
  return Response(status_code=204, headers=_DEPRECATION)
