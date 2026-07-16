"""Guarded same-origin reverse proxy for owner-configured local web services.

Mini-apps are sandboxed frontend frames, so a real backend web application
(Tandoor, Paperless, Grafana, etc.) cannot run *inside* one.  This route gives
those applications one deliberately narrow integration boundary:

  /services/<slug>/...  ->  a statically configured loopback HTTP origin

The browser never supplies an upstream URL.  Configuration lives outside the
platform checkout at ``<DATA_DIR>/local-services.json`` and stock Möbius ships
with no configured services.  Only literal loopback targets are accepted in
v1; this prevents the route from becoming an SSRF/open-proxy primitive while
covering the local-process use case it was introduced for. A service may also
opt into a dedicated-origin surface. The backend and bundled proxy both read
``MOBIUS_SERVICE_<SLUG>_ORIGIN`` so that origin has one configuration source:

Configuration schema::

  {
    "version": 1,
    "services": {
      "tandoor": {
        "upstream": "http://127.0.0.1:8123",
        "access": "upstream_auth",
        "public_surface": true
      }
    }
  }

The public ``/services/<slug>`` prefix is preserved on the upstream request.
Backend applications must therefore be configured to serve from that same
base path.  This keeps redirects, cookie paths, forms, static assets, and API
URLs coherent without unsafe response-body rewriting. The upstream application
owns user authentication: configuration must acknowledge that explicitly, and
the proxy never forwards a Möbius bearer token to it.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.config import get_settings
from app.deps import get_current_owner

log = logging.getLogger(__name__)

router = APIRouter(tags=["local-services"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CONFIG_NAME = "local-services.json"
_MAX_CONFIG_BYTES = 64 * 1024
_SURFACE_ENV = re.compile(r"^MOBIUS_SERVICE_([A-Z0-9_]+)_ORIGIN$")

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
_PRIVATE_REQUEST_HEADERS = {
  "authorization",
  "host",
  "proxy-authorization",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-prefix",
  "x-forwarded-proto",
  "x-script-name",
}


class ServiceConfigError(ValueError):
  """The private local-service configuration failed closed."""


@dataclass(frozen=True)
class LocalService:
  slug: str
  upstream: str
  surface_origin: str | None = None

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


def _surface_origin_env(slug: str) -> str:
  return f"MOBIUS_SERVICE_{slug.upper().replace('-', '_')}_ORIGIN"


def _validated_surface_origin(value: object, slug: str) -> str:
  if not isinstance(value, str):
    raise ServiceConfigError("surface origin must be a URL string")
  parsed = urlsplit(value)
  is_local_dev = (
    get_settings().domain == "localhost"
    and parsed.scheme == "http"
    and parsed.hostname == f"{slug}.localhost"
  )
  if (
    (parsed.scheme != "https" and not is_local_dev)
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path not in ("", "/")
  ):
    raise ServiceConfigError(
      "surface origin must be a dedicated HTTPS origin without a path"
    )
  shell = urlsplit(get_settings().frontend_origin)
  if (parsed.scheme, parsed.netloc) == (shell.scheme, shell.netloc):
    raise ServiceConfigError("surface origin must not be the shell origin")
  return f"{parsed.scheme}://{parsed.netloc}"


def _configured_surface_origin(slug: str, enabled: object) -> str | None:
  if enabled is not True:
    if enabled in (None, False):
      return None
    raise ServiceConfigError("public_surface must be a boolean")
  value = os.environ.get(_surface_origin_env(slug), "").strip()
  if not value:
    raise ServiceConfigError(f"{_surface_origin_env(slug)} is not configured")
  return _validated_surface_origin(value, slug)


def _service_for(slug: str) -> LocalService | None:
  if not _SLUG.fullmatch(slug):
    return None
  try:
    services = _read_config()
    if services is None or slug not in services:
      return None
    entry = services[slug]
    if not isinstance(entry, dict) or not set(entry).issubset(
      {"upstream", "access", "public_surface"}
    ) or not {"upstream", "access"}.issubset(entry):
      raise ServiceConfigError(
        f"service {slug!r} has unsupported configuration fields"
      )
    # Browser navigations cannot attach the owner's localStorage bearer token.
    # Requiring this explicit declaration prevents an agent from accidentally
    # publishing an unauthenticated dev server through the public Möbius host.
    if entry["access"] != "upstream_auth":
      raise ServiceConfigError(
        f"service {slug!r} must explicitly declare access=upstream_auth"
      )
    return LocalService(
      slug=slug,
      upstream=_validated_upstream(entry["upstream"]),
      surface_origin=_configured_surface_origin(
        slug, entry.get("public_surface"),
      ),
    )
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
    if (
      key.lower().encode("latin-1") not in _HOP_BY_HOP
      and key.lower() not in _PRIVATE_REQUEST_HEADERS
    )
  }
  origin = (
    service.surface_origin
    if _request_matches_surface_origin(request, service)
    else get_settings().frontend_origin
  )
  public = urlsplit(origin)
  public_host = public.netloc
  if public_host:
    headers["host"] = public_host
    headers["x-forwarded-host"] = public_host
  public_scheme = public.scheme
  headers["x-forwarded-proto"] = (
    public_scheme if public_scheme in {"http", "https"} else request.url.scheme
  )
  headers["x-forwarded-for"] = (
    request.client.host if request.client else "127.0.0.1"
  )
  headers["x-script-name"] = service.mount_path
  headers["x-forwarded-prefix"] = service.mount_path
  return headers


def _rewrite_location(value: str, service: LocalService, request: Request) -> str:
  """Keep absolute loopback redirects on the public Möbius origin."""
  parsed = urlsplit(value)
  upstream = urlsplit(service.upstream)
  try:
    same_upstream = (
      parsed.scheme == upstream.scheme
      and parsed.hostname == upstream.hostname
      and (parsed.port or 80) == (upstream.port or 80)
    )
  except ValueError:
    same_upstream = False
  if not same_upstream:
    return value
  origin = (
    service.surface_origin
    if _request_matches_surface_origin(request, service)
    else get_settings().frontend_origin
  )
  public = urlsplit(origin)
  return parsed._replace(scheme=public.scheme, netloc=public.netloc).geturl()


def _rewrite_set_cookie(value: str, service: LocalService) -> str:
  """Confine upstream cookies to their service and the public host."""
  parts = [part.strip() for part in value.split(";")]
  if not parts:
    return value
  attrs: list[str] = []
  saw_path = False
  mount = service.mount_path
  for attr in parts[1:]:
    name, separator, raw_value = attr.partition("=")
    lowered = name.strip().lower()
    if lowered == "domain":
      continue
    if lowered == "path":
      saw_path = True
      path = raw_value.strip() if separator else ""
      if path != mount and not path.startswith(f"{mount}/"):
        path = f"{mount}/"
      attrs.append(f"Path={path}")
      continue
    if attr:
      attrs.append(attr)
  if not saw_path:
    attrs.append(f"Path={mount}/")
  return "; ".join([parts[0], *attrs])


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
  public_surface = _request_matches_surface_origin(request, service)
  response.raw_headers = []
  for name, value in upstream.headers.raw:
    lower = name.lower()
    if lower in _HOP_BY_HOP:
      continue
    if public_surface and lower == b"x-frame-options":
      # The dedicated service origin is framed only by the shell origin below.
      # Normal /services traffic on the shell origin keeps upstream XFO.
      continue
    if public_surface and lower == b"content-security-policy":
      # Keep every upstream restriction except frame-ancestors, which must name
      # the separate shell origin for this deliberately direct surface.
      directives = [part.strip() for part in value.split(b";")]
      value = b"; ".join(
        part for part in directives
        if part and not part.lower().startswith(b"frame-ancestors")
      )
      if not value:
        continue
    if lower == b"location":
      value = _rewrite_location(
        value.decode("latin-1"), service, request,
      ).encode("latin-1")
    elif lower == b"set-cookie":
      # Cookies are always host-only and confined to this service mount.
      value = _rewrite_set_cookie(
        value.decode("latin-1"), service,
      ).encode("latin-1")
    response.raw_headers.append((name, value))
  if public_surface:
    response.raw_headers.append((
      b"content-security-policy",
      f"frame-ancestors 'self' {get_settings().frontend_origin}".encode("latin-1"),
    ))
  return response


def _request_matches_surface_origin(request: Request, service: LocalService) -> bool:
  if not service.surface_origin:
    return False
  public = urlsplit(service.surface_origin)
  return request.headers.get("host", "").lower() == public.netloc.lower()


def is_public_service_surface_request(scope) -> bool:
  """Fail-closed host+path check used by the global frame-header middleware."""
  path = scope.get("path") or ""
  match = re.match(r"^/services/([a-z0-9][a-z0-9-]{0,62})(?:/|$)", path)
  if not match:
    return False
  service = _service_for(match.group(1))
  if service is None or not service.surface_origin:
    return False
  host = ""
  for name, value in scope.get("headers") or []:
    if name.lower() == b"host":
      host = value.decode("latin-1").lower()
      break
  return host == urlsplit(service.surface_origin).netloc.lower()


def service_surface_host_allows_path(scope) -> bool:
  """Reserve each configured service host for that service prefix only.

  This guard intentionally reads the operator environment rather than the
  optional service registry. Even a disabled, missing, or malformed registry
  entry must not turn the dedicated hostname into a second shell/API origin.
  """
  host = ""
  for name, value in scope.get("headers") or []:
    if name.lower() == b"host":
      host = value.decode("latin-1").lower()
      break
  if not host:
    return True
  for key, raw_origin in os.environ.items():
    match = _SURFACE_ENV.fullmatch(key)
    if not match or not raw_origin.strip():
      continue
    try:
      netloc = urlsplit(raw_origin.strip()).netloc.lower()
    except ValueError:
      continue
    if not netloc or host != netloc:
      continue
    slug = match.group(1).lower().replace("_", "-")
    path = scope.get("path") or ""
    prefix = f"/services/{slug}"
    return path == prefix or path.startswith(f"{prefix}/")
  return True


@router.get("/api/local-services/{slug}/surface")
async def local_service_surface(
  slug: str,
  _owner=Depends(get_current_owner),
):
  """Return the shell-owned cross-origin surface for one configured service."""
  service = _service_for(slug)
  if service is None:
    raise HTTPException(status_code=404, detail="Local service not found.")
  if not service.surface_origin:
    raise HTTPException(
      status_code=409,
      detail="This service needs a dedicated public origin before it can open.",
    )
  return {
    "slug": slug,
    "url": f"{service.surface_origin}{service.mount_path}/_mobius/surface",
  }


def _service_surface_html(service: LocalService) -> str:
  shell_origin = get_settings().frontend_origin
  js_slug = json.dumps(service.slug)
  js_mount = json.dumps(f"{service.mount_path}/")
  js_shell_origin = json.dumps(shell_origin)
  # The hash correlation is a browser routing guard, never authorization. The
  # owner-only API above is the only route that reveals this configured URL.
  return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body,#service{{height:100%;margin:0;background:#0f1117;color:#f4f5f8;font:14px system-ui,sans-serif}}
#service{{position:relative;overflow:hidden}} iframe{{position:absolute;inset:0;width:100%;height:100%;border:0;opacity:0}}
#cover{{position:absolute;inset:0;display:grid;place-items:center;background:#0f1117;z-index:1}}
#cover[hidden]{{display:none}}
#card{{text-align:center;padding:24px}} #spin{{width:32px;height:32px;margin:0 auto 14px;border:3px solid #303440;border-top-color:#8b7cf6;border-radius:50%;animation:s .9s linear infinite}}
@keyframes s{{to{{transform:rotate(360deg)}}}}
@media(prefers-color-scheme:light){{html,body,#service,#cover{{background:#f7f7fa;color:#171820}}#spin{{border-color:#d8d9e2;border-top-color:#6657d9}}}}
</style></head><body><div id="service"><iframe id="app" title="{service.slug}"></iframe><div id="cover"><div id="card"><div id="spin"></div>Opening {service.slug}…</div></div></div>
<script>
(()=>{{
  const child=document.getElementById('app'),cover=document.getElementById('cover');
  const correlation=location.hash.slice(1);let ready=false;
  function hide(){{ready=false;child.style.opacity='0';cover.hidden=false}}
  function confirm(){{
    if(ready)return;
    try{{
      const doc=child.contentDocument;
      if(!doc||!doc.documentElement||!child.contentWindow.location.pathname.startsWith({js_mount}))return;
      ready=true;child.style.opacity='1';cover.hidden=true;
      child.contentWindow.addEventListener('beforeunload',hide,{{once:true}});
      child.contentWindow.addEventListener('pagehide',hide,{{once:true}});
      window.parent.postMessage({{type:'moebius:service-ready',service:{js_slug},correlation}},{js_shell_origin});
    }}catch(_error){{}}
  }}
  child.addEventListener('load',()=>{{hide();requestAnimationFrame(()=>requestAnimationFrame(confirm))}});
  child.src={js_mount};
}})();
</script></body></html>"""


@router.get("/services/{slug}/_mobius/surface", include_in_schema=False)
async def local_service_surface_document(slug: str, request: Request):
  """Dedicated-origin adapter kept inert until its same-origin child is real."""
  service = _service_for(slug)
  if service is None or not _request_matches_surface_origin(request, service):
    raise HTTPException(status_code=404, detail="Local service surface not found.")
  response = HTMLResponse(_service_surface_html(service))
  response.headers["Cache-Control"] = "no-store"
  response.headers["Content-Security-Policy"] = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    f"frame-src {service.surface_origin}; "
    f"frame-ancestors 'self' {get_settings().frontend_origin}"
  )
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
