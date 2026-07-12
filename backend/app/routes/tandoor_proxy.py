"""Minimal reverse proxy: ``/tandoor/*`` -> the local Tandoor server.

Tandoor Recipes runs as its own process on ``127.0.0.1:8123`` (installed under
``/data/tandoor``, launched by ``/data/tandoor/run.sh``). Mobius mini-apps are
sandboxed frontend iframes and cannot *be* a Django server, so to reach Tandoor
from the browser we mount it under the Mobius domain with this thin proxy.

Design notes:
- We forward the FULL ``/tandoor``-prefixed path unchanged. Tandoor runs with
  ``SCRIPT_NAME=/tandoor`` and REJECTS any request path that doesn't start with
  it, so the prefix must be preserved (not stripped).
- We keep the inbound ``Host`` and ``Cookie`` headers and add
  ``X-Forwarded-Proto: https``. Tandoor ships
  ``SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')``, so Django then
  treats the request as HTTPS and same-origin CSRF/login work without pinning
  the public domain here.
- No Mobius auth gate: Tandoor owns its own login. (Single-owner instance.)
- Responses stream back verbatim minus hop-by-hop headers.
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger(__name__)

router = APIRouter()

_UPSTREAM = "http://127.0.0.1:8123"
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

# Headers that must not be forwarded verbatim on request or response.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


@router.get("/tandoor")
async def _tandoor_bare():
    # Normalize the missing trailing slash so relative asset URLs resolve.
    return Response(status_code=307, headers={"Location": "/tandoor/"})


@router.api_route("/tandoor/{path:path}", methods=_METHODS)
async def tandoor_proxy(path: str, request: Request):
    url = f"{_UPSTREAM}/tandoor/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    # Tandoor's Django trusts this to know the browser spoke HTTPS to Mobius.
    fwd_headers["x-forwarded-proto"] = "https"
    host = request.headers.get("host")
    if host:
        fwd_headers["x-forwarded-host"] = host

    body = await request.body()

    client = httpx.AsyncClient(follow_redirects=False, timeout=90.0)
    try:
        upstream_req = client.build_request(
            request.method, url, headers=fwd_headers, content=body,
        )
        upstream = await client.send(upstream_req, stream=True)
    except Exception as exc:  # Tandoor process down / not started
        await client.aclose()
        log.warning("tandoor upstream unreachable: %s", exc)
        return Response(
            content=(
                "Tandoor isn't running right now. Start it with "
                "/data/tandoor/run.sh, then reload."
            ).encode(),
            status_code=502,
            media_type="text/plain",
        )

    # Set-Cookie MUST be forwarded as SEPARATE headers, never merged. httpx's
    # `.items()` comma-joins repeated headers into one value, and a dict would
    # collapse duplicate keys anyway — both corrupt Set-Cookie (its Expires
    # attribute contains commas, and the browser reads the joined blob as a
    # SINGLE cookie, silently dropping the rest). Tandoor's login response sets
    # THREE cookies (sessionid + csrftoken + messages); merged into one, the
    # browser kept csrftoken/messages but dropped `sessionid`, so every request
    # after a successful 302 login was anonymous → an endless bounce back to the
    # login page. So: build the non-cookie headers as a dict (comma-joining is
    # fine for those), and append each Set-Cookie individually via multi_items().
    out_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "set-cookie"
    }
    set_cookies = [
        v for k, v in upstream.headers.multi_items()
        if k.lower() == "set-cookie"
    ]

    async def _body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response = StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=out_headers,
    )
    for cookie in set_cookies:
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response
