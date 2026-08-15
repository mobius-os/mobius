"""HTTP proxy route: lets mini-apps fetch external URLs server-side.

This sidesteps browser CORS restrictions for external APIs that mini-apps
need to read (e.g. public market data feeds).

Only GET and POST are supported. Requests are authenticated by the
owner or an app-scoped token.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.deps import authorize_current_owner_or_app_detached, reject_cross_site
from app.net_utils import validate_url_safe

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# Hard limit on response size to avoid pulling in huge payloads.
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

# 512 KB — generous for API payloads, prevents memory exhaustion from abuse.
_MAX_BODY = 512 * 1024
_FORWARDED_RESPONSE_HEADERS = (
  "retry-after",
  "x-ratelimit-limit",
  "x-ratelimit-remaining",
  "x-ratelimit-reset",
  "x-ratelimit-used",
)

# Reference cards use this bounded resolver so redirects, custom icon paths,
# and ordinary root icons share one SSRF-safe loading path.
_FAVICON_MAX_BYTES = 256 * 1024
_FAVICON_PAGE_MAX_BYTES = 512 * 1024
_FAVICON_MAX_REDIRECTS = 5
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
  content_type: str
  final_url: str
  truncated: bool


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


async def _read_external_get(
  client: httpx.AsyncClient,
  url: str,
  max_bytes: int,
) -> _ExternalRead:
  """Read one public URL with a byte cap and SSRF-safe redirect handling.

  Every hop is resolved, validated, and DNS-pinned independently. Letting
  httpx follow redirects itself would allow a public URL to bounce into the
  container network after only the first host passed validation.
  """
  current_url = url
  for hop in range(_FAVICON_MAX_REDIRECTS + 1):
    pinned_url, host_header, sni_host = validate_url_safe(current_url)
    req = client.build_request(
      "GET",
      pinned_url,
      headers={
        "Accept": "image/*,text/html;q=0.8,*/*;q=0.1",
        "User-Agent": _FAVICON_USER_AGENT,
      },
    )
    req.headers["host"] = host_header
    req.extensions["sni_hostname"] = sni_host
    try:
      upstream = await client.send(req, stream=True)
    except httpx.TimeoutException:
      raise HTTPException(504, f"Timeout fetching {current_url}")
    except httpx.RequestError as exc:
      raise HTTPException(502, f"Failed to fetch {current_url}: {exc}")
    try:
      if upstream.status_code in _REDIRECT_STATUSES:
        location = upstream.headers.get("location")
        if not location:
          raise HTTPException(
            502, f"Redirect from {current_url} missing Location header.",
          )
        if hop >= _FAVICON_MAX_REDIRECTS:
          raise HTTPException(
            502,
            f"Too many redirects (>{_FAVICON_MAX_REDIRECTS}) "
            f"starting from {url}",
          )
        current_url = urljoin(current_url, location)
        continue

      body = bytearray()
      async for chunk in upstream.aiter_bytes():
        room = max_bytes + 1 - len(body)
        if room <= 0:
          break
        body.extend(chunk[:room])
        if len(body) > max_bytes:
          break
      return _ExternalRead(
        body=bytes(body[:max_bytes]),
        status_code=upstream.status_code,
        content_type=upstream.headers.get(
          "content-type", "application/octet-stream",
        ),
        final_url=current_url,
        truncated=len(body) > max_bytes,
      )
    finally:
      await upstream.aclose()
  raise HTTPException(502, "Favicon redirect resolution failed.")


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
    content_type = icon.content_type.split(";", 1)[0].strip().lower()
    if (
      200 <= icon.status_code < 300
      and not icon.truncated
      and icon.body
      and content_type in _FAVICON_CONTENT_TYPES
    ):
      return icon
  return None


async def _capped_response(
  client: httpx.AsyncClient,
  req: httpx.Request,
  *,
  forward_cache_headers: bool = False,
) -> Response:
  """Sends `req` streaming and reads at most `_MAX_BYTES` into memory. The prior
  code read the FULL body (`r.content`) before slicing, so a huge or malicious
  upstream response could exhaust process memory before the cap ever applied.
  This stops at the cap and drops the rest."""
  try:
    r = await client.send(req, stream=True)
  except Exception as exc:
    raise HTTPException(status_code=502, detail=str(exc))
  try:
    buf = bytearray()
    async for chunk in r.aiter_bytes():
      # Append only up to the cap so the buffer is STRICTLY bounded by _MAX_BYTES
      # (extending the whole chunk first could overshoot by a chunk's worth).
      room = _MAX_BYTES - len(buf)
      buf.extend(chunk[:room])
      if len(buf) >= _MAX_BYTES:
        break
    headers = {
      name: r.headers[name]
      for name in _FORWARDED_RESPONSE_HEADERS
      if name in r.headers
    }
    if forward_cache_headers:
      for name in ("cache-control", "etag", "expires", "last-modified"):
        if name in r.headers:
          headers[name] = r.headers[name]
    return Response(
      content=bytes(buf),
      status_code=r.status_code,
      headers=headers,
      media_type=r.headers.get("content-type", "application/octet-stream"),
    )
  finally:
    await r.aclose()


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
      content_type = icon.content_type.split(";", 1)[0].strip().lower()
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

    page_type = page.content_type.split(";", 1)[0].strip().lower()
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
      content_type = icon.content_type.split(";", 1)[0].strip().lower()
      return Response(
        content=icon.body,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
      )
  raise HTTPException(404, "Site icon unavailable.")


@router.get("")
async def proxy_get(
  url: str,
  _: None = Depends(authorize_current_owner_or_app_detached),
):
  """Fetches a URL via GET and returns the raw response body.

  Opaque-origin mini-app frames legitimately arrive with
  ``Sec-Fetch-Site: cross-site`` even when calling this same host. This route is
  read-only, requires a bearer token (and therefore a CORS preflight), and keeps
  the SSRF allow/deny checks below, so the mutation-oriented CSRF dependency is
  intentionally not applied here. The POST proxy remains guarded.
  """
  pinned_url, host_header, sni_host = validate_url_safe(url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    req = client.build_request("GET", pinned_url)
    req.headers["host"] = host_header
    # httpcore/anyio require text here. Bytes reach idna2008_resolve(), which
    # calls .encode() itself and turns every real HTTPS proxy request into 502.
    req.extensions["sni_hostname"] = sni_host
    return await _capped_response(client, req)


@router.post("", dependencies=[Depends(reject_cross_site)])
async def proxy_post(
  body: ProxyPostRequest,
  _: None = Depends(authorize_current_owner_or_app_detached),
):
  """Posts to a URL and returns the raw response body."""
  if body.body and len(body.body.encode()) > _MAX_BODY:
    raise HTTPException(413, "Request body too large (max 512 KB)")
  pinned_url, host_header, sni_host = validate_url_safe(body.url)
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    req = client.build_request(
      "POST", pinned_url,
      content=body.body.encode(),
      headers={"Content-Type": body.content_type},
    )
    req.headers["host"] = host_header
    req.extensions["sni_hostname"] = sni_host
    return await _capped_response(client, req)
