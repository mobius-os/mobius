"""Authenticated and explicitly public routes for accepted app services."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import app_services, models
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site
from app.resource_access import live_app_or_404
from app.storage_io import read_capped_body


router = APIRouter(tags=["app-services"])
_limiter = Limiter(key_func=get_remote_address, key_style="endpoint")
# The anonymous door counts each caller per public service, against that
# service's accepted allowance (60 a minute unless its manifest declares more).
_public_window = FixedWindowRateLimiter(MemoryStorage())


def _admit_public_request(request: Request, app: models.App | None, service: dict) -> None:
  allowance = RateLimitItemPerMinute(app_services.public_requests_per_minute(service))
  caller = get_remote_address(request)
  bucket = str(app.id) if app is not None else "unknown"
  if _public_window.hit(allowance, caller, bucket):
    return
  reset_at, _ = _public_window.get_window_stats(allowance, caller, bucket)
  raise HTTPException(
    429,
    f"Rate limit exceeded: {allowance.amount} per 1 minute",
    headers={"Retry-After": str(max(1, int(reset_at - time.time()) + 1))},
  )


def _reject_json_constant(value: str):
  raise ValueError(f"invalid JSON constant: {value}")


def _response(status: int, body, headers: dict[str, str], media_type: str | None):
  if media_type is not None:
    return Response(body, status_code=status, headers=headers, media_type=media_type)
  return JSONResponse(body, status_code=status, headers=headers)


def _service_app(db: Session, service_id: str) -> models.App | None:
  """Resolve one stable service id, an explicit transition alias, or legacy.

  Product slugs never become automatic aliases after a stable identity is
  stamped. A reviewed manifest may carry a bounded alias list for one rolling
  rename; removing it from the next accepted version retires that route.
  Rows predating the migration retain their slug until their next accepted
  manifest stamps the explicit service contract.
  """
  target = (
    db.query(models.App)
    .filter(
      models.App.service_id == service_id,
      models.App.deleted_at.is_(None),
    )
    .one_or_none()
  )
  if target is not None:
    return target
  target = (
    db.query(models.App)
    .join(
      models.AppServiceAlias,
      models.AppServiceAlias.app_id == models.App.id,
    )
    .filter(
      models.AppServiceAlias.service_id == service_id,
      models.App.deleted_at.is_(None),
    )
    .one_or_none()
  )
  if target is not None:
    return target
  return (
    db.query(models.App)
    .filter(
      models.App.service_id.is_(None),
      models.App.slug == service_id,
      models.App.deleted_at.is_(None),
    )
    .one_or_none()
  )


async def _envelope(request: Request, path: str, *, public: bool, actor: dict) -> dict:
  # `tools/` belongs to the platform's agent-tool lane (app_tools.call_app_tool),
  # whose `call` a service trusts as the moment an agent called it. An HTTP
  # caller never reaches it, so a frame cannot forge a tool call.
  segments = [segment for segment in path.split("/") if segment]
  if (
    len(path) > 512 or any(segment in (".", "..") for segment in segments)
    or segments[:1] == ["tools"]
  ):
    raise HTTPException(404, "App service path not found.")
  raw = await read_capped_body(
    request,
    app_services.MAX_REQUEST_BYTES,
    too_large="App service request is too large.",
  )
  body = None
  if raw:
    try:
      body = json.loads(raw, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
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
    "actor": actor,
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
  envelope = await _envelope(
    request, path, public=False,
    actor=app_services.request_actor(
      db, principal, app if principal.app_id is not None else None,
    ),
  )
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
  target = _service_app(db, slug)
  if target is None:
    raise HTTPException(404, "App service not found.")
  caller = db.get(models.App, principal.app_id) if principal.app_id is not None else None
  if principal.app_id is not None and caller is None:
    raise HTTPException(403, "Calling app is unavailable.")
  required = "self" if principal.app_id in {None, target.id} else "apps"
  app_services.service_contract(target, access=required)
  envelope = await _envelope(
    request, path, public=False,
    actor=app_services.request_actor(db, principal, caller),
  )
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
async def public_app_service(
  slug: str,
  path: str,
  request: Request,
  db: Session = Depends(get_db),
):
  app = _service_app(db, slug)
  try:
    if app is None:
      raise HTTPException(404, "Public app service not found.")
    service = app_services.service_contract(app, access="public")
  except HTTPException:
    # Probes of missing or non-public services share one default bucket.
    _admit_public_request(request, None, {})
    raise
  _admit_public_request(request, app, service)
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "Owner setup is incomplete.")
  envelope = await _envelope(request, path, public=True, actor={"scope": "public"})
  db.expunge(app)
  db.expunge(owner)
  db.close()
  status, body, headers, media_type = await app_services.invoke_service(
    app, owner, envelope,
  )
  return _response(status, body, headers, media_type)
