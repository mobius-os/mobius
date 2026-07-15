"""Guarded same-origin reverse proxy for owner-configured local web services.

Mini-apps are sandboxed frontend frames, so a real backend web application
(Tandoor, Paperless, Grafana, etc.) cannot run *inside* one.  This route gives
those applications one deliberately narrow integration boundary:

  /services/<slug>/...  ->  a statically configured loopback HTTP origin

The browser never supplies an upstream URL.  Configuration lives outside the
platform checkout at ``<DATA_DIR>/local-services.json`` and stock Möbius ships
with no configured services.  Only literal loopback targets are accepted in
v1; this prevents the route from becoming an SSRF/open-proxy primitive while
covering the local-process use case it was introduced for.

Configuration schema::

  {
    "version": 1,
    "services": {
      "tandoor": {"upstream": "http://127.0.0.1:8123"}
    }
  }

The public ``/services/<slug>`` prefix is preserved on the upstream request.
Backend applications must therefore be configured to serve from that same
base path.  This keeps redirects, cookie paths, forms, static assets, and API
URLs coherent without unsafe response-body rewriting.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["local-services"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CONFIG_NAME = "local-services.json"
_MAX_CONFIG_BYTES = 64 * 1024

# RFC 9110 hop-by-hop fields plus Content-Length: StreamingResponse owns the
# downstream framing and httpx calculates the upstream request length.
_HOP_BY_HOP = {
  b"connection",
  b"keep-alive",
  b"proxy-authenticate",
  b"proxy-authorization",
  b"te",
  b"trailer",
  b"trailers",
  b"transfer-encoding",
  b"upgrade",
  b"content-length",
}


class ServiceConfigError(ValueError):
  """The private local-service configuration failed closed."""


@dataclass(frozen=True)
class LocalService:
  slug: str
  upstream: str

  @property
  def mount_path(self) -> str:
    return f"/services/{self.slug}"


def _config_path() -> Path:
  return Path(get_settings().data_dir) / _CONFIG_NAME


def _read_config() -> dict | None:
  path = _config_path()
  try:
    size = path.stat().st_size
  except FileNotFoundError:
    return None
  except OSError as exc:
    raise ServiceConfigError("configuration cannot be read") from exc
  if size > _MAX_CONFIG_BYTES:
    raise ServiceConfigError("configuration is too large")
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise ServiceConfigError("configuration is not valid UTF-8 JSON") from exc
  if not isinstance(payload, dict) or payload.get("version") != 1:
    raise ServiceConfigError("configuration must declare version 1")
  services = payload.get("services")
  if not isinstance(services, dict):
    raise ServiceConfigError("configuration must contain a services object")
  return services


def _validated_upstream(value: object) -> str:
  if not isinstance(value, str):
    raise ServiceConfigError("upstream must be a URL string")
  parsed = urlsplit(value)
  if (
    parsed.scheme != "http"
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path not in ("", "/")
  ):
    raise ServiceConfigError(
      "upstream must be an HTTP loopback origin without credentials or a path"
    )
  try:
    address = ipaddress.ip_address(parsed.hostname)
  except ValueError as exc:
    raise ServiceConfigError("upstream host must be a literal loopback address") from exc
  if not address.is_loopback:
    raise ServiceConfigError("upstream host must be loopback")
  try:
    port = parsed.port
  except ValueError as exc:
    raise ServiceConfigError("upstream port is invalid") from exc
  if port is None:
    port = 80
  host = f"[{address.compressed}]" if address.version == 6 else address.compressed
  return f"http://{host}:{port}"


def _service_for(slug: str) -> LocalService | None:
  if not _SLUG.fullmatch(slug):
    return None
  try:
    services = _read_config()
    if services is None or slug not in services:
      return None
    entry = services[slug]
    if not isinstance(entry, dict) or set(entry) != {"upstream"}:
      raise ServiceConfigError(
        f"service {slug!r} must contain exactly one upstream field"
      )
    return LocalService(slug=slug, upstream=_validated_upstream(entry["upstream"]))
  except ServiceConfigError as exc:
    log.warning("Local service %s is unavailable: %s", slug, exc)
    raise HTTPException(
      status_code=503,
      detail=f"Local service '{slug}' is unavailable because its configuration is invalid.",
    ) from exc


def _forwarded_request_headers(request: Request, service: LocalService) -> dict[str, str]:
  headers = {
    key: value
    for key, value in request.headers.items()
    if key.lower().encode("latin-1") not in _HOP_BY_HOP and key.lower() != "host"
  }
  public_host = request.headers.get("host")
  if public_host:
    headers["host"] = public_host
    headers["x-forwarded-host"] = public_host
  public_scheme = urlsplit(get_settings().frontend_origin).scheme
  headers["x-forwarded-proto"] = (
    public_scheme if public_scheme in {"http", "https"} else request.url.scheme
  )
  headers["x-forwarded-for"] = (
    request.client.host if request.client else "127.0.0.1"
  )
  headers["x-script-name"] = service.mount_path
  return headers


def _upstream_url(service: LocalService, request: Request) -> str:
  # `request.url.path` is already normalized by the ASGI server.  Preserve the
  # reserved public prefix exactly; the service owns all routing below it.
  url = f"{service.upstream}{request.url.path}"
  if request.url.query:
    url = f"{url}?{request.url.query}"
  return url


async def _proxy(service: LocalService, request: Request) -> Response:
  client = httpx.AsyncClient(
    follow_redirects=False,
    timeout=httpx.Timeout(90.0, connect=5.0),
  )
  upstream = None
  try:
    body = await request.body()
    upstream_request = client.build_request(
      request.method,
      _upstream_url(service, request),
      headers=_forwarded_request_headers(request, service),
      content=body,
    )
    upstream = await client.send(upstream_request, stream=True)
  except Exception as exc:
    if upstream is not None:
      await upstream.aclose()
    await client.aclose()
    log.warning("Local service %s is unreachable: %s", service.slug, exc)
    return Response(
      content=f"The local service '{service.slug}' is not available right now.",
      status_code=502,
      media_type="text/plain",
    )

  async def body_stream():
    try:
      async for chunk in upstream.aiter_raw():
        yield chunk
    finally:
      await upstream.aclose()
      await client.aclose()

  response = StreamingResponse(body_stream(), status_code=upstream.status_code)
  # Preserve every end-to-end response field as raw pairs.  In particular,
  # multiple Set-Cookie headers MUST remain separate; joining them corrupts
  # cookies whose Expires attributes contain commas.
  response.raw_headers = [
    (name, value)
    for name, value in upstream.headers.raw
    if name.lower() not in _HOP_BY_HOP
  ]
  return response


@router.api_route("/services", methods=_METHODS)
@router.api_route("/services/", methods=_METHODS)
async def local_services_root():
  # Reserve the namespace even when no services exist; never let it fall
  # through to the SPA catch-all and masquerade as a shell route.
  raise HTTPException(status_code=404, detail="Local service not found.")


@router.api_route("/services/{slug}", methods=_METHODS)
async def local_service_bare(slug: str):
  service = _service_for(slug)
  if service is None:
    raise HTTPException(status_code=404, detail="Local service not found.")
  return RedirectResponse(url=f"{service.mount_path}/", status_code=307)


@router.api_route("/services/{slug}/{path:path}", methods=_METHODS)
async def local_service_proxy(slug: str, path: str, request: Request):
  service = _service_for(slug)
  if service is None:
    raise HTTPException(status_code=404, detail="Local service not found.")
  return await _proxy(service, request)
