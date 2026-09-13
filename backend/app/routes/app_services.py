"""Authenticated and explicitly public routes for accepted app services."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import app_services, models
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site
from app.resource_access import live_app_or_404
from app.storage_io import read_capped_body


router = APIRouter(tags=["app-services"])
_limiter = Limiter(key_func=get_remote_address, key_style="endpoint")


def _response(status: int, body, headers: dict[str, str], media_type: str | None):
  if media_type is not None:
    return Response(body, status_code=status, headers=headers, media_type=media_type)
  return JSONResponse(body, status_code=status, headers=headers)


def _header_principal(request: Request, db: Session) -> Principal | None:
  authorization = request.headers.get("authorization")
  if not authorization:
    return None
  scheme, separator, token = authorization.partition(" ")
  if separator != " " or scheme.lower() != "bearer" or not token:
    raise HTTPException(401, "Invalid authorization header.")
  return get_principal(token, db)


async def _envelope(request: Request, path: str, *, public: bool, scope: str) -> dict:
  if ".." in path.split("/") or len(path) > 512:
    raise HTTPException(404, "App service path not found.")
  raw = await read_capped_body(
    request,
    app_services.MAX_REQUEST_BYTES,
    too_large="App service request is too large.",
  )
  body = None
  if raw:
    try:
      body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
      raise HTTPException(400, "App service requests must contain JSON.") from exc
  return {
    "schema": 1,
    "method": request.method,
    "path": path,
    "query": {key: request.query_params.getlist(key) for key in request.query_params},
    "headers": {
      name: request.headers[name]
      for name in ("accept", "content-type", "if-match", "if-none-match")
      if name in request.headers
    },
    "body": body,
    "public": public,
    "actor": {"scope": scope},
  }


@router.api_route(
  "/api/apps/{app_id}/service/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@_limiter.limit("120/minute")
async def authenticated_app_service(
  app_id: int,
  path: str,
  request: Request,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  if request.method != "GET":
    reject_cross_site(request)
  app = live_app_or_404(db, app_id)
  if principal.app_id is not None and principal.app_id != app.id:
    raise HTTPException(403, "An app can invoke only its own service.")
  app_services.service_contract(app, access="self")
  envelope = await _envelope(request, path, public=False, scope=principal.scope)
  db.expunge(app)
  db.expunge(principal.owner)
  db.close()
  status, body, headers, media_type = await app_services.invoke_service(
    app, principal.owner, envelope,
  )
  return _response(status, body, headers, media_type)


@router.api_route(
  "/api/services/{slug}/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@_limiter.limit("120/minute")
async def shared_app_service(
  slug: str,
  path: str,
  request: Request,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  if request.method != "GET":
    reject_cross_site(request)
  target = (
    db.query(models.App)
    .filter(models.App.slug == slug, models.App.deleted_at.is_(None))
    .one_or_none()
  )
  if target is None:
    raise HTTPException(404, "App service not found.")
  caller = db.get(models.App, principal.app_id) if principal.app_id is not None else None
  if principal.app_id is not None and caller is None:
    raise HTTPException(403, "Calling app is unavailable.")
  required = "self" if principal.app_id in {None, target.id} else "apps"
  app_services.service_contract(target, access=required)
  envelope = await _envelope(request, path, public=False, scope=principal.scope)
  envelope["actor"].update({
    "app_id": principal.app_id,
    "app_slug": caller.slug if caller is not None else None,
    "delegated": principal.delegation_id is not None,
  })
  db.expunge(target)
  db.expunge(principal.owner)
  db.close()
  status, body, headers, media_type = await app_services.invoke_service(
    target, principal.owner, envelope,
  )
  return _response(status, body, headers, media_type)


@router.api_route(
  "/api/app-services/{slug}/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@_limiter.limit("60/minute")
async def public_app_service(
  slug: str,
  path: str,
  request: Request,
  db: Session = Depends(get_db),
):
  app = (
    db.query(models.App)
    .filter(models.App.slug == slug, models.App.deleted_at.is_(None))
    .one_or_none()
  )
  if app is None:
    raise HTTPException(404, "Public app service not found.")
  app_services.service_contract(app, access="public")
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "Owner setup is incomplete.")
  envelope = await _envelope(request, path, public=True, scope="public")
  db.expunge(app)
  db.expunge(owner)
  db.close()
  status, body, headers, media_type = await app_services.invoke_service(
    app, owner, envelope,
  )
  return _response(status, body, headers, media_type)


@router.api_route(
  "/api/common/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@_limiter.limit("60/minute")
async def social_protocol_service(
  path: str,
  request: Request,
  db: Session = Depends(get_db),
):
  """Serve Social's published federation protocol through its app-owned service.

  ``/api/common`` is an independently deployed peer contract, not an internal
  runtime alias.  The platform owns only authentication and bounded dispatch;
  the installed Social app owns every response and protocol decision.
  """
  app = (
    db.query(models.App)
    .filter(models.App.slug == "common", models.App.deleted_at.is_(None))
    .one_or_none()
  )
  if app is None:
    raise HTTPException(404, "Social service not found.")
  app_services.service_contract(app, access="public")
  principal = _header_principal(request, db)
  public = principal is None
  if not public and request.method != "GET":
    reject_cross_site(request)
  actor = {"scope": "public"}
  owner = db.query(models.Owner).first() if public else principal.owner
  caller = None
  if principal is not None:
    caller = db.get(models.App, principal.app_id) if principal.app_id else None
    actor = {
      "scope": principal.scope,
      "app_id": principal.app_id,
      "app_slug": caller.slug if caller is not None else None,
      "delegated": principal.delegation_id is not None,
    }
  if owner is None:
    raise HTTPException(503, "Owner setup is incomplete.")
  envelope = await _envelope(request, path, public=public, scope=actor["scope"])
  envelope["actor"] = actor
  db.expunge(app)
  db.expunge(owner)
  db.close()
  status, body, headers, media_type = await app_services.invoke_service(
    app, owner, envelope,
  )
  return _response(status, body, headers, media_type)
