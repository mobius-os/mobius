"""HTTP proxy route: lets mini-apps fetch external URLs server-side.

This sidesteps browser CORS restrictions for external APIs that mini-apps
need to read (e.g. public market data feeds).

Only GET and POST are supported. Requests are authenticated by the
owner or an app-scoped token.
"""

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from math import ceil
from time import monotonic
from urllib.parse import urljoin, urlparse
from urllib.request import parse_http_list

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.deps import authorize_current_owner_or_app_detached, reject_cross_site
from app.net_utils import MAX_REDIRECTS as _MAX_REDIRECTS, validate_url_safe
from app.pinned_http_clients import PinnedHostClientPool

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# Identify outbound proxy requests with a real User-Agent. httpx's default
# ("python-httpx/x.y") is 403'd by several public APIs that require an
# identifiable client (OSM Nominatim, Photon and others enforce this per their
# usage policy, and Nominatim additionally asks for a contact URL). A stable
# app-identifying UA keeps those endpoints usable for every mini-app; same
# convention as _FAVICON_USER_AGENT below.
_PROXY_USER_AGENT = "Mobius/1.0 (app proxy; +https://github.com/mobius-os/mobius)"

# Hard limit on response size to avoid pulling in huge payloads. The general
# proxy preserves its original behavior of returning the first 2 MiB.
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
# httpx's timeout applies to each network read, so a drip-fed body could hold a
# pooled slot for as long as it keeps sending; this bounds the whole exchange.
_EXCHANGE_DEADLINE = 60

# 512 KB — generous for API payloads, prevents memory exhaustion from abuse.
_MAX_BODY = 512 * 1024
_FORWARDED_RESPONSE_HEADERS = (
  "retry-after",
  "x-ratelimit-limit",
  "x-ratelimit-remaining",
  "x-ratelimit-reset",
  "x-ratelimit-used",
)

# The owner proxy reuses keep-alive connections per (Host, SNI); see
# app.pinned_http_clients for why one global client would be unsafe.
_proxy_clients = PinnedHostClientPool()

# Browser freshness the owner proxy will grant at most, whatever upstream says.
_PROXY_MAX_BROWSER_AGE = 24 * 60 * 60

ResponseCacheHeaders = Callable[[httpx.Response, float], dict[str, str]]


def forward_upstream_cache_headers(
  upstream: httpx.Response, elapsed: float = 0,
) -> dict[str, str]:
  """Pass upstream cache headers through unchanged (anonymous public apps)."""
  return {
    name: upstream.headers[name]
    for name in ("cache-control", "etag", "expires", "last-modified")
    if name in upstream.headers
  }


def _cache_directives(value: str) -> dict[str, str]:
  directives: dict[str, str] = {}
  for part in parse_http_list(value):
    name, _, argument = part.strip().partition("=")
    if name:
      name = name.strip().lower()
      # RFC 9111 allows treating repeated freshness directives as stale.
      # Never let a later max-age override an earlier restrictive one.
      directives[name] = "" if name in directives else argument.strip().strip('"')
  return directives


def private_browser_cache_headers(
  upstream: httpx.Response, elapsed: float = 0,
) -> dict[str, str]:
  """Let the requesting browser, and only it, reuse a proxied public read.

  The proxy URL is requested with a bearer token, so an upstream `public` or
  `s-maxage` must never reach a shared cache (a CDN in front of Möbius) that
  could replay owner-fetched bytes, and the list of URLs the owner reads, to
  anyone without a token. Freshness is therefore always rewritten to `private`
  and clamped; upstream `no-store` stays `no-store`. Validators are never
  exposed: the proxy URL does not bind the origin selected by redirects, so
  expired reads must fetch again unconditionally. Nothing is cached server-side.
  """
  if upstream.status_code not in (200, 304, *_REDIRECT_STATUSES):
    return {}
  directives = _cache_directives(upstream.headers.get("cache-control", ""))
  if "no-store" in directives:
    return {"cache-control": "no-store"}
  headers: dict[str, str] = {}
  # Preserve the origin's selection dimensions. In particular, Vary: * can
  # never match a stored response; replacing it with Authorization would make
  # an intentionally unmatchable representation reusable.
  vary = upstream.headers.get("vary", "")
  vary_names = [name.strip() for name in vary.split(",") if name.strip()]
  if "*" in vary_names:
    headers["vary"] = "*"
  elif any(name.lower() == "authorization" for name in vary_names):
    headers["vary"] = vary
  else:
    # A private cache can still be shared by bearer identities in one browser.
    headers["vary"] = f"{vary}, Authorization" if vary_names else "Authorization"
  try:
    max_age = int(directives.get("max-age", ""))
  except ValueError:
    max_age = None
  if max_age is not None and max_age > 0:
    # The upstream's Date and Age describe time already spent in prior caches.
    # Dropping them while resetting Date at this proxy would grant that time
    # again, potentially serving stale bytes as fresh.
    age = 0
    try:
      age = max(0, int(upstream.headers.get("age", "0"))) + ceil(elapsed)
    except ValueError:
      age = max_age
    if "date" in upstream.headers:
      try:
        upstream_date = parsedate_to_datetime(upstream.headers["date"])
        if upstream_date.tzinfo is None:
          upstream_date = upstream_date.replace(tzinfo=UTC)
        age = max(age, ceil((datetime.now(UTC) - upstream_date).total_seconds()))
      except (TypeError, ValueError, OverflowError):
        age = max_age
    max_age -= max(0, age)
  if "no-cache" in directives or max_age is None or max_age <= 0:
    headers["cache-control"] = "private, no-cache"
  else:
    headers["cache-control"] = (
      f"private, max-age={min(max_age, _PROXY_MAX_BROWSER_AGE)}"
    )
  if "must-revalidate" in directives:
    headers["cache-control"] += ", must-revalidate"
  return headers


class _PrivateBrowserCacheHeaders:
  """Intersect freshness and selection limits for one resolved proxy URL."""

  def __init__(self):
    self.redirected = False
    self.no_store = False
    self.no_cache = False
    self.must_revalidate = False
    self.fresh_until: float | None = None
    self.vary: dict[str, str] = {}

  def __call__(
    self, upstream: httpx.Response, elapsed: float = 0,
  ) -> dict[str, str]:
    self.redirected |= upstream.status_code in _REDIRECT_STATUSES
    headers = private_browser_cache_headers(upstream, elapsed)
    if not headers:
      if not self.redirected:
        return headers
      # Never grant freshness to an unsupported final status, but do not let
      # a heuristically cacheable error erase the redirector's restrictions.
      directives = _cache_directives(upstream.headers.get("cache-control", ""))
      headers = {
        "cache-control": "no-store" if "no-store" in directives else "private, no-cache",
        "vary": upstream.headers.get("vary", ""),
      }
    directives = _cache_directives(headers["cache-control"])
    self.no_store |= "no-store" in directives
    self.no_cache |= "no-cache" in directives
    self.must_revalidate |= "must-revalidate" in directives
    now = monotonic()
    if "max-age" in directives:
      fresh_until = now + int(directives["max-age"])
      self.fresh_until = (
        fresh_until if self.fresh_until is None
        else min(self.fresh_until, fresh_until)
      )
    for name in headers.get("vary", "").split(","):
      name = name.strip()
      if name:
        self.vary.setdefault(name.lower(), name)
    if self.no_store:
      return {"cache-control": "no-store"}
    # Round down so time spent following later hops is never granted again.
    max_age = int(self.fresh_until - now) if self.fresh_until is not None else 0
    cache_control = (
      "private, no-cache" if self.no_cache or max_age <= 0
      else f"private, max-age={max_age}"
    )
    if self.must_revalidate:
      cache_control += ", must-revalidate"
    return {
      "cache-control": cache_control,
      "vary": "*" if "*" in self.vary else ", ".join(self.vary.values()),
    }


async def close_proxy_clients() -> None:
  await _proxy_clients.close()


# Reference cards use this bounded resolver so redirects, custom icon paths,
# and ordinary root icons share one SSRF-safe loading path.
_FAVICON_MAX_BYTES = 256 * 1024
_FAVICON_PAGE_MAX_BYTES = 512 * 1024
_FAVICON_LINK_LIMIT = 8
_FAVICON_USER_AGENT = "Mobius/1.0 (reference favicon fetch)"
_FAVICON_CONTENT_TYPES = frozenset((
  "application/octet-stream",
  "image/gif",
  "image/ico",
  "image/jpeg",
  "image/png",
  "image/svg+xml",
  "image/vnd.microsoft.icon",
  "image/webp",
  "image/x-icon",
))
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


class ProxyPostRequest(BaseModel):
  url: str
  body: str = ""
  content_type: str = "application/x-www-form-urlencoded"


@dataclass(frozen=True)
class _ExternalRead:
  body: bytes
  status_code: int
  content_type: str | None
  final_url: str
  truncated: bool
  forwarded_headers: dict[str, str] = field(default_factory=dict)


class _FaviconLinkParser(HTMLParser):
  def __init__(self):
    super().__init__(convert_charrefs=True)
    self.hrefs: list[tuple[int, str]] = []

  def handle_starttag(self, tag, attrs):
    if tag.lower() != "link":
      return
    values = {
      str(name).lower(): str(value or "")
      for name, value in attrs
      if name
    }
    rel = frozenset(values.get("rel", "").lower().split())
    if "icon" in rel:
      priority = 0
    elif "apple-touch-icon" in rel:
      priority = 1
    else:
      return
    href = values.get("href", "").strip()
    if href:
      self.hrefs.append((priority, href))


def _declared_favicon_urls(source: str, page_url: str) -> list[str]:
  """Return bounded, distinct http(s) icon links declared by one HTML page."""
  parser = _FaviconLinkParser()
  try:
    parser.feed(source)
  except Exception:
    # A malformed tail must not discard valid <link> tags parsed before it.
    pass
  urls: list[str] = []
  seen: set[str] = set()
  for _priority, href in sorted(parser.hrefs, key=lambda item: item[0]):
    candidate = urljoin(page_url, href)
    parsed = urlparse(candidate)
    if (
      parsed.scheme not in ("http", "https")
      or not parsed.hostname
      or parsed.username
      or parsed.password
      or candidate in seen
    ):
      continue
    seen.add(candidate)
    urls.append(candidate)
    if len(urls) >= _FAVICON_LINK_LIMIT:
      break
  return urls


def _canonical_root_icon_urls(page_url: str) -> list[str]:
  parsed = urlparse(page_url)
  if parsed.scheme not in ("http", "https") or not parsed.hostname:
    return []
  root = parsed._replace(path="/", params="", query="", fragment="").geturl()
  return [
    urljoin(root, name)
    for name in (
      "favicon.ico",
      "favicon.svg",
      "favicon.png",
      "apple-touch-icon.png",
    )
  ]


def _fetch_error(url: str, exc: httpx.RequestError) -> HTTPException:
  if isinstance(exc, httpx.TimeoutException):
    return HTTPException(504, f"Timeout fetching {url}")
  return HTTPException(502, f"Failed to fetch {url}: {exc}")


async def _read_bounded_body(
  upstream: httpx.Response, max_bytes: int, url: str,
) -> bytes:
  """Return a bounded prefix without waiting for EOF, classifying stream failures."""
  body = bytearray()
  try:
    async for chunk in upstream.aiter_bytes():
      room = max_bytes - len(body)
      if room <= 0:
        break
      body.extend(chunk[:room])
      if len(body) >= max_bytes:
        break
  except httpx.RequestError as exc:
    raise _fetch_error(url, exc) from exc
  return bytes(body)


async def _read_external_get(
  client: httpx.AsyncClient | PinnedHostClientPool,
  url: str,
  max_bytes: int,
  *,
  headers: dict[str, str] | None = None,
  probe_truncation: bool = True,
  cache_headers: ResponseCacheHeaders | None = None,
) -> _ExternalRead:
  """Read one public URL with a byte cap and SSRF-safe redirect handling.

  Every hop is resolved, validated, and DNS-pinned independently. Letting
  httpx follow redirects itself would allow a public URL to bounce into the
  container network after only the first host passed validation. The cache
  policy is evaluated on every hop so it can restrict the resolved response.
  """
  current_url = url
  for hop in range(_MAX_REDIRECTS + 1):
    pinned_url, host_header, sni_host = await asyncio.to_thread(
      validate_url_safe, current_url,
    )
    lease = (
      client.lease(host_header, sni_host)
      if isinstance(client, PinnedHostClientPool) else nullcontext(client)
    )
    async with lease as hop_client:
      req = hop_client.build_request(
        "GET",
        pinned_url,
        headers=headers if headers is not None else {
          "Accept": "image/*,text/html;q=0.8,*/*;q=0.1",
          "User-Agent": _FAVICON_USER_AGENT,
        },
      )
      req.headers["host"] = host_header
      req.extensions["sni_hostname"] = sni_host
      try:
        upstream = await hop_client.send(req, stream=True)
      except httpx.RequestError as exc:
        raise _fetch_error(current_url, exc) from exc
      try:
        if upstream.status_code in _REDIRECT_STATUSES:
          location = upstream.headers.get("location")
          if not location:
            raise HTTPException(
              502, f"Redirect from {current_url} missing Location header.",
            )
          if hop >= _MAX_REDIRECTS:
            raise HTTPException(
              502,
              f"Too many redirects (>{_MAX_REDIRECTS}) "
              f"starting from {url}",
            )
          if cache_headers is not None:
            cache_headers(upstream, 0)
          current_url = urljoin(current_url, location)
          continue

        received_at = monotonic()
        body = await _read_bounded_body(
          upstream, max_bytes + int(probe_truncation), current_url,
        )
        return _ExternalRead(
          body=body[:max_bytes],
          status_code=upstream.status_code,
          content_type=upstream.headers.get("content-type"),
          final_url=current_url,
          truncated=len(body) > max_bytes,
          forwarded_headers=_response_headers(
            upstream, len(body) >= max_bytes, cache_headers,
            elapsed=monotonic() - received_at,
          ),
        )
      finally:
        await upstream.aclose()
  raise HTTPException(502, "Redirect resolution failed.")


async def _first_supported_icon(
  client: httpx.AsyncClient,
  candidates: list[str],
) -> _ExternalRead | None:
  for candidate in candidates:
    try:
      icon = await _read_external_get(client, candidate, _FAVICON_MAX_BYTES)
    except HTTPException:
      # A site may publish a stale, private, malformed, or unavailable icon
      # link. It is data, not authority to weaken the network boundary.
      continue
    content_type = icon.content_type or "application/octet-stream"
    content_type = content_type.partition(";")[0].strip().lower()
    if (
      200 <= icon.status_code < 300
      and not icon.truncated
      and icon.body
      and content_type in _FAVICON_CONTENT_TYPES
    ):
      return icon
  return None


def _response_headers(
  upstream: httpx.Response,
  capped: bool,
  cache_headers: ResponseCacheHeaders | None,
  *,
  elapsed: float = 0,
) -> dict[str, str]:
  headers = {
    name: upstream.headers[name]
    for name in _FORWARDED_RESPONSE_HEADERS
    if name in upstream.headers
  }
  if cache_headers is not None:
    headers.update(cache_headers(upstream, elapsed))
    if capped and upstream.status_code == 200:
      # A bounded prefix must never carry full-body validators or freshness.
      for name in ("etag", "last-modified", "expires"):
        headers.pop(name, None)
      headers["cache-control"] = "no-store"
  return headers


async def _capped_response(
  client: httpx.AsyncClient,
  req: httpx.Request,
  url: str,
  *,
  cache_headers: ResponseCacheHeaders | None = None,
) -> Response:
  """Read a bounded prefix within one exchange deadline.

  Errors name the caller's URL, not the DNS-pinned request address. Reaching
  the cap stops immediately, even if the upstream then stalls without EOF.
  """
  try:
    async with asyncio.timeout(_EXCHANGE_DEADLINE):
      try:
        upstream = await client.send(req, stream=True)
      except httpx.RequestError as exc:
        raise _fetch_error(url, exc) from exc
      except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
      try:
        body = await _read_bounded_body(upstream, _MAX_BYTES, url)
        return Response(
          content=body,
          status_code=upstream.status_code,
          headers=_response_headers(
            upstream, len(body) >= _MAX_BYTES, cache_headers,
          ),
          media_type=(
            upstream.headers.get("content-type") if upstream.status_code == 304
            else upstream.headers.get("content-type", "application/octet-stream")
          ),
        )
      finally:
        await upstream.aclose()
  except TimeoutError:
    raise HTTPException(
      504, f"Upstream did not finish within {_EXCHANGE_DEADLINE} seconds.",
    ) from None


@router.get("/favicon")
async def proxy_favicon(
  url: str,
  _: None = Depends(authorize_current_owner_or_app_detached),
):
  """Resolve one site's declared favicon without exposing the cited page path.

  The frontend supplies an origin URL, not the full cited article. The server
  first tries conventional root icons with safe redirect handling. Only sites
  that need it pay for a bounded HTML read and declared-icon discovery.
  """
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    icon = await _first_supported_icon(
      client, _canonical_root_icon_urls(url),
    )
    if icon is not None:
      content_type = icon.content_type or "application/octet-stream"
      content_type = content_type.partition(";")[0].strip().lower()
      return Response(
        content=icon.body,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
      )

    page = await _read_external_get(client, url, _FAVICON_PAGE_MAX_BYTES)
    if (
      page.status_code < 200
      or page.status_code >= 300
    ):
      raise HTTPException(404, "Site icon unavailable.")

    page_type = page.content_type or "application/octet-stream"
    page_type = page_type.partition(";")[0].strip().lower()
    declared = []
    if page_type in ("text/html", "application/xhtml+xml"):
      declared = _declared_favicon_urls(
        page.body.decode("utf-8", errors="ignore"),
        page.final_url,
      )
    candidates = list(dict.fromkeys(
      declared + _canonical_root_icon_urls(page.final_url),
    ))
    icon = await _first_supported_icon(client, candidates)
    if icon is not None:
      content_type = icon.content_type or "application/octet-stream"
      content_type = content_type.partition(";")[0].strip().lower()
      return Response(
        content=icon.body,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
      )
  raise HTTPException(404, "Site icon unavailable.")


@router.get("")
async def proxy_get(
  url: str,
  request: Request,
  _: None = Depends(authorize_current_owner_or_app_detached),
):
  """Fetches a URL via GET and returns the raw response body.

  Opaque-origin mini-app frames legitimately arrive with
  ``Sec-Fetch-Site: cross-site`` even when calling this same host. This route is
  read-only, requires a bearer token (and therefore a CORS preflight), and keeps
  the SSRF allow/deny checks below, so the mutation-oriented CSRF dependency is
  intentionally not applied here. The POST proxy remains guarded.

  Redirects are independently validated and pinned like app install. Each
  hop leases its own Host/SNI client; freshness and Vary restrictions apply
  across the whole chain. Browser validators are never forwarded: a cached
  proxy URL cannot establish which origin will serve its next read.
  """
  headers = {"User-Agent": _PROXY_USER_AGENT}
  try:
    async with asyncio.timeout(_EXCHANGE_DEADLINE):
      read = await _read_external_get(
        _proxy_clients, url, _MAX_BYTES, headers=headers,
        probe_truncation=False, cache_headers=_PrivateBrowserCacheHeaders(),
      )
  except TimeoutError:
    raise HTTPException(
      504, f"Upstream did not finish within {_EXCHANGE_DEADLINE} seconds.",
    ) from None
  return Response(
    content=read.body,
    status_code=read.status_code,
    headers=read.forwarded_headers,
    media_type=(
      read.content_type if read.status_code == 304
      else read.content_type or "application/octet-stream"
    ),
  )


@router.post("", dependencies=[Depends(reject_cross_site)])
async def proxy_post(
  body: ProxyPostRequest,
  _: None = Depends(authorize_current_owner_or_app_detached),
):
  """Posts to a URL and returns the raw response body."""
  if body.body and len(body.body.encode()) > _MAX_BODY:
    raise HTTPException(413, "Request body too large (max 512 KB)")
  pinned_url, host_header, sni_host = await asyncio.to_thread(
    validate_url_safe, body.url,
  )
  async with _proxy_clients.lease(host_header, sni_host) as client:
    req = client.build_request(
      "POST", pinned_url,
      content=body.body.encode(),
      headers={"Content-Type": body.content_type},
    )
    req.headers["host"] = host_header
    req.headers["user-agent"] = _PROXY_USER_AGENT
    req.extensions["sni_hostname"] = sni_host
    return await _capped_response(client, req, body.url)
