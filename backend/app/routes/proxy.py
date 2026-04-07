"""HTTP proxy route: lets mini-apps fetch external URLs server-side.

This sidesteps browser CORS restrictions for external APIs that mini-apps
need to read (e.g. public market data feeds).

Only GET and POST are supported. Requests are authenticated so only the
owner can use this endpoint.
"""

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import models
from app.deps import get_current_owner_or_app

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# Hard limit on response size to avoid pulling in huge payloads.
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

# 512 KB — generous for API payloads, prevents memory exhaustion from abuse.
_MAX_BODY = 512 * 1024


class ProxyPostRequest(BaseModel):
  url: str
  body: str = ""
  content_type: str = "application/x-www-form-urlencoded"


@router.get("")
async def proxy_get(
  url: str,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Fetches a URL via GET and returns the raw response body."""
  _validate_url(url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    try:
      r = await client.get(url)
    except Exception as exc:
      raise HTTPException(status_code=502, detail=str(exc))
  return Response(
    content=r.content[:_MAX_BYTES],
    status_code=r.status_code,
    media_type=r.headers.get("content-type", "application/octet-stream"),
  )


@router.post("")
async def proxy_post(
  body: ProxyPostRequest,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Posts to a URL and returns the raw response body."""
  if body.body and len(body.body.encode()) > _MAX_BODY:
    raise HTTPException(413, "Request body too large (max 512 KB)")
  _validate_url(body.url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    try:
      r = await client.post(
        body.url,
        content=body.body.encode(),
        headers={"Content-Type": body.content_type},
      )
    except Exception as exc:
      raise HTTPException(status_code=502, detail=str(exc))
  return Response(
    content=r.content[:_MAX_BYTES],
    status_code=r.status_code,
    media_type=r.headers.get("content-type", "application/octet-stream"),
  )


def _validate_url(url: str) -> None:
  """Rejects non-HTTP(S) URLs and malformed inputs."""
  if not url.startswith(("http://", "https://")):
    raise HTTPException(status_code=400, detail="Only http/https URLs allowed.")
  parsed = urlparse(url)
  if not parsed.hostname:
    raise HTTPException(status_code=400, detail="Invalid URL.")
