"""Authenticated and explicitly public routes for accepted app services."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import SlidingWindowCounterRateLimiter
from slowapi import Limiter

from app import app_services, models, public_service_index
from app.database import get_db
from app.deps import Principal, get_principal, reject_cross_site
from app.manifest_contract import PUBLIC_SERVICE_REQUESTS_PER_MINUTE, PUBLIC_SERVICE_VISITOR_SHARES
from app.net_utils import rate_limit_client
from app.resource_access import live_app_or_404
from app.storage_io import read_capped_body


router = APIRouter(tags=["app-services"])
# Total body-read lifetime, separate from the unchanged 15-second execution timeout.
SERVICE_BODY_TIMEOUT_SECONDS = 120
_limiter = Limiter(key_func=rate_limit_client, key_style="endpoint")
# The anonymous door holds each public service to its accepted allowance (60 a
# minute unless its manifest declares more) across all its visitors, and each
# visitor to a 1/PUBLIC_SERVICE_VISITOR_SHARES share of it (never under 60), so
# one visitor cannot spend every other visitor's share. A visitor is the
# request's client address; behind a proxy that address is the proxy's, so
# every visitor shares one visitor share. The sliding window keeps a burst
# across a minute boundary near the allowance instead of doubling it.
# In-memory counters are exact because Mobius serves its API from a single
# uvicorn worker; running several workers would need a shared store here.
_public_window = SlidingWindowCounterRateLimiter(MemoryStorage())


def _admit_public_request(request: Request, bucket: str, per_minute: int) -> None:
  visitor = RateLimitItemPerMinute(
    max(PUBLIC_SERVICE_REQUESTS_PER_MINUTE, per_minute // PUBLIC_SERVICE_VISITOR_SHARES),
  )
  service = RateLimitItemPerMinute(per_minute)
  counters = (
    (visitor, ("visitor", bucket, rate_limit_client(request))),
    (service, ("service", bucket)),
  )
  for allowance, key in counters:
    if not _public_window.test(allowance, *key):
      reset_at, _ = _public_window.get_window_stats(allowance, *key)
      raise HTTPException(
        429,
        f"Rate limit exceeded: {allowance.amount} per 1 minute",
        headers={"Retry-After": str(max(1, int(reset_at - time.time()) + 1))},
      )
  for allowance, key in counters:
    _public_window.hit(allowance, *key)


async def _response(status: int, body, headers: dict[str, str], media_type: str | None):
  """Construct the response while the caller's exchange admission is held.

  A JSON body renders to the same bytes as JSONResponse, one part at a time
  (app_services.render_json), within the estimate its decode reserved.
  """
  if media_type is not None:
    return Response(body, status_code=status, headers=headers, media_type=media_type)
  return Response(
    await app_services.render_json(body), status_code=status, headers=headers,
    media_type=JSONResponse.media_type,
  )


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


async def _envelope(
  request: Request, path: str, *, public: bool, actor: dict,
  max_bytes: int, admission: app_services.ServiceExchangeAdmission,
) -> dict:
  # `tools/` belongs to the platform's agent-tool lane (app_tools.call_app_tool),
  # whose `call` a service trusts as the moment an agent called it. An HTTP
  # caller never reaches it, so a frame cannot forge a tool call.
  segments = [segment for segment in path.split("/") if segment]
  if (
    len(path) > 512 or any(segment in (".", "..") for segment in segments)
    or segments[:1] == ["tools"]
  ):
    raise HTTPException(404, "App service path not found.")
  try:
    async with asyncio.timeout(SERVICE_BODY_TIMEOUT_SECONDS):
      raw = await read_capped_body(
        request,
        max_bytes,
        too_large="App service request is too large.",
        admit_growth=admission.admit_received,
      )
  except TimeoutError as exc:
    raise HTTPException(408, "App service request body timed out.") from exc
  # Only the anonymous ingress share is released. The raw body remains owned
  # by the same total exchange reservation until only its envelope remains.
  admission.finish_ingress()
  body = None
  if raw:
    try:
      body = await admission.decode(raw)
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
  service = app_services.service_contract(app, access="self")
  max_bytes = app_services.exchange_max_bytes(service, request.method, path)
  with app_services.ServiceExchangeAdmission(max_bytes) as admission:
    envelope = await _envelope(
      request, path, public=False, max_bytes=max_bytes, admission=admission,
      actor=app_services.request_actor(
        db, principal, app if principal.app_id is not None else None,
      ),
    )
    db.expunge(app)
    db.expunge(principal.owner)
    db.close()
    status, body, headers, media_type = await app_services.invoke_service(
      app, principal.owner, envelope, admission=admission,
    )
    return await _response(status, body, headers, media_type)


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
  service = app_services.service_contract(target, access=required)
  max_bytes = app_services.exchange_max_bytes(service, request.method, path)
  with app_services.ServiceExchangeAdmission(max_bytes) as admission:
    envelope = await _envelope(
      request, path, public=False, max_bytes=max_bytes, admission=admission,
      actor=app_services.request_actor(db, principal, caller),
    )
    db.expunge(target)
    db.expunge(principal.owner)
    db.close()
    status, body, headers, media_type = await app_services.invoke_service(
      target, principal.owner, envelope, admission=admission,
    )
    return await _response(status, body, headers, media_type)


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
  # The snapshot names each public service's bucket; every other id shares
  # the probe bucket. Both are counted, and an over-limit caller refused,
  # before any lookup.
  public = public_service_index.lookup(db, slug)
  _admit_public_request(request, *(public or public_service_index.bucket(None, {})))
  app = _service_app(db, slug) if public is not None else None
  if app is None:
    raise HTTPException(404, "Public app service not found.")
  service = app_services.service_contract(app, access="public")
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "Owner setup is incomplete.")
  max_bytes = app_services.exchange_max_bytes(service, request.method, path)
  with app_services.ServiceExchangeAdmission(max_bytes, public_ingress=True) as admission:
    envelope = await _envelope(
      request, path, public=True, actor={"scope": "public"},
      max_bytes=max_bytes, admission=admission,
    )
    db.expunge(app)
    db.expunge(owner)
    db.close()
    status, body, headers, media_type = await app_services.invoke_service(
      app, owner, envelope, admission=admission,
    )
    return await _response(status, body, headers, media_type)
