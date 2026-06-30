"""HTTP proxy route: lets mini-apps fetch external URLs server-side.

This sidesteps browser CORS restrictions for external APIs that mini-apps
need to read (e.g. public market data feeds).

Only GET and POST are supported. Requests are authenticated by the
owner or an app-scoped token.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import models
from app.deps import get_current_owner_or_app, reject_cross_site
from app.net_utils import validate_url_safe

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# Hard limit on response size to avoid pulling in huge payloads.
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

# 512 KB — generous for API payloads, prevents memory exhaustion from abuse.
_MAX_BODY = 512 * 1024


class ProxyPostRequest(BaseModel):
  url: str
  body: str = ""
  content_type: str = "application/x-www-form-urlencoded"


@router.get("", dependencies=[Depends(reject_cross_site)])
async def proxy_get(
  url: str,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Fetches a URL via GET and returns the raw response body."""
  pinned_url, host_header, sni_host = validate_url_safe(url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    try:
      req = client.build_request("GET", pinned_url)
      req.headers["host"] = host_header
      req.extensions["sni_hostname"] = sni_host.encode("ascii")
      r = await client.send(req)
    except Exception as exc:
      raise HTTPException(status_code=502, detail=str(exc))
  return Response(
    content=r.content[:_MAX_BYTES],
    status_code=r.status_code,
    media_type=r.headers.get("content-type", "application/octet-stream"),
  )


@router.post("", dependencies=[Depends(reject_cross_site)])
async def proxy_post(
  body: ProxyPostRequest,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Posts to a URL and returns the raw response body."""
  if body.body and len(body.body.encode()) > _MAX_BODY:
    raise HTTPException(413, "Request body too large (max 512 KB)")
  pinned_url, host_header, sni_host = validate_url_safe(body.url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    try:
      req = client.build_request(
        "POST", pinned_url,
        content=body.body.encode(),
        headers={"Content-Type": body.content_type},
      )
      req.headers["host"] = host_header
      req.extensions["sni_hostname"] = sni_host.encode("ascii")
      r = await client.send(req)
    except Exception as exc:
      raise HTTPException(status_code=502, detail=str(exc))
  return Response(
    content=r.content[:_MAX_BYTES],
    status_code=r.status_code,
    media_type=r.headers.get("content-type", "application/octet-stream"),
  )
