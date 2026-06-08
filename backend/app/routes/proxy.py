"""HTTP proxy route: lets mini-apps fetch external URLs server-side.

This sidesteps browser CORS restrictions for external APIs that mini-apps
need to read (e.g. public market data feeds).

Only GET and POST are supported. Requests are authenticated so only the
owner can use this endpoint.
"""

import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import models
from app.deps import get_current_owner_or_app, reject_cross_site

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
  pinned_url, hostname = _validate_url(url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    try:
      req = client.build_request("GET", pinned_url)
      req.headers["host"] = hostname
      req.extensions["sni_hostname"] = hostname.encode("ascii")
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
  pinned_url, hostname = _validate_url(body.url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    try:
      req = client.build_request(
        "POST", pinned_url,
        content=body.body.encode(),
        headers={"Content-Type": body.content_type},
      )
      req.headers["host"] = hostname
      req.extensions["sni_hostname"] = hostname.encode("ascii")
      r = await client.send(req)
    except Exception as exc:
      raise HTTPException(status_code=502, detail=str(exc))
  return Response(
    content=r.content[:_MAX_BYTES],
    status_code=r.status_code,
    media_type=r.headers.get("content-type", "application/octet-stream"),
  )


def _validate_url(url: str) -> tuple[str, str]:
  """Validates URL and returns (pinned_url, original_hostname).

  Resolves the hostname, rejects any non-global IPs, and rewrites the URL
  to connect directly to a validated IP address. This prevents DNS rebinding
  between validation and connection (TOCTOU).
  """
  if not url.startswith(("http://", "https://")):
    raise HTTPException(status_code=400, detail="Only http/https URLs allowed.")
  parsed = urlparse(url)
  if not parsed.hostname:
    raise HTTPException(status_code=400, detail="Invalid URL.")
  try:
    infos = socket.getaddrinfo(
      parsed.hostname,
      parsed.port or (443 if parsed.scheme == "https" else 80),
    )
  except socket.gaierror:
    raise HTTPException(status_code=400, detail="Cannot resolve hostname.")
  validated_ip = None
  for _fam, _type, _proto, _canon, sockaddr in infos:
    ip = ipaddress.ip_address(sockaddr[0])
    if not ip.is_global:
      raise HTTPException(
        status_code=403,
        detail="Proxying to private/internal addresses is not allowed.",
      )
    if validated_ip is None:
      validated_ip = str(ip)
  if validated_ip is None:
    raise HTTPException(status_code=400, detail="Cannot resolve hostname.")
  ip_host = f"[{validated_ip}]" if ":" in validated_ip else validated_ip
  new_netloc = f"{ip_host}:{parsed.port}" if parsed.port else ip_host
  pinned_url = urlunparse(parsed._replace(netloc=new_netloc))
  return pinned_url, parsed.hostname
