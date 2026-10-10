"""Tests for server-side proxy URL validation and DNS pinning.

The proxy now shares the canonical SSRF validator with the install fetcher
(`app.net_utils.validate_url_safe`); the unit tests below exercise it directly
and the integration tests drive it through the proxy endpoints.
"""

import asyncio
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.net_utils import validate_url_safe
from app.routes.proxy import (
  _ExternalRead,
  _capped_response,
  _declared_favicon_urls,
  _read_external_get,
  forward_upstream_cache_headers,
)
from test_app_fixtures import create_local_app


@pytest.fixture(autouse=True)
def isolated_proxy_pool(monkeypatch):
  from app.pinned_http_clients import PinnedHostClientPool
  pool = PinnedHostClientPool()
  monkeypatch.setattr("app.routes.proxy._proxy_clients", pool)
  yield
  asyncio.run(pool.close())


# ---------------------------------------------------------------------------
# Integration tests (hit the endpoint via TestClient)
# ---------------------------------------------------------------------------

def test_proxy_rejects_private_ips(client, owner_token):
  """The proxy should reject requests to private/internal addresses."""
  auth = {"Authorization": f"Bearer {owner_token}"}
  for url in [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
  ]:
    r = client.get(f"/api/proxy?url={url}", headers=auth)
    assert r.status_code in (400, 403), f"{url} was not blocked: {r.status_code}"


def test_proxy_post_rejects_private_ips(client, owner_token):
  """POST proxy also validates URLs against private ranges."""
  auth = {"Authorization": f"Bearer {owner_token}"}
  r = client.post("/api/proxy", json={
    "url": "http://169.254.169.254/latest/meta-data/",
  }, headers=auth)
  assert r.status_code in (400, 403)


def test_proxy_rejects_non_http(client, owner_token):
  auth = {"Authorization": f"Bearer {owner_token}"}
  r = client.get("/api/proxy?url=ftp://example.com/file", headers=auth)
  assert r.status_code == 400


def test_proxy_rejects_unresolvable(client, owner_token):
  auth = {"Authorization": f"Bearer {owner_token}"}
  r = client.get(
    "/api/proxy?url=http://this-domain-does-not-exist-xyz123.invalid/",
    headers=auth,
  )
  assert r.status_code == 400


def _gai_v6(ip_str):
  """A getaddrinfo result tuple list for a single IPv6 address."""
  import socket as _socket
  return [(_socket.AF_INET6, _socket.SOCK_STREAM, 0, "", (ip_str, 0, 0, 0))]


def test_proxy_blocks_ipv6_embedded_ipv4(client, owner_token):
  """SSRF regression: IPv6-embedded internal v4 must be blocked at the PROXY.

  These resolutions reach internal v4 hosts but read as `is_global == True` to
  the proxy's old check, so it let them through — a live bypass that the install
  fetcher already closed. The shared validator now rejects all three at the
  proxy too. ::127.0.0.1 (IPv4-compatible loopback), ::ffff:169.254.169.254
  (IPv4-mapped cloud metadata), and 64:ff9b::a9fe:a9fe (NAT64 of
  169.254.169.254).
  """
  auth = {"Authorization": f"Bearer {owner_token}"}
  for ip_str in ("::127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe"):
    with patch("app.net_utils.socket.getaddrinfo", return_value=_gai_v6(ip_str)):
      r = client.get("/api/proxy?url=https://evil.example/", headers=auth)
      assert r.status_code == 400, f"GET {ip_str} not blocked: {r.status_code}"
      r = client.post(
        "/api/proxy", json={"url": "https://evil.example/"}, headers=auth,
      )
      assert r.status_code == 400, f"POST {ip_str} not blocked: {r.status_code}"


# ---------------------------------------------------------------------------
# Unit tests for validate_url_safe DNS pinning
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(results):
  """Returns a mock for socket.getaddrinfo that returns the given tuples."""
  def _gai(host, port, *a, **kw):
    return results
  return _gai


def test_validate_url_pins_to_resolved_ip():
  """Pinned URL replaces hostname with the validated IP."""
  fake = _fake_getaddrinfo([(2, 1, 6, '', ('93.184.216.34', 80))])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    pinned, host_header, sni_host = validate_url_safe("http://example.com/path?q=1")
  assert host_header == "example.com"
  assert sni_host == "example.com"
  assert "93.184.216.34" in pinned
  assert "example.com" not in pinned
  assert "/path?q=1" in pinned


def test_validate_url_preserves_port():
  """Custom ports survive the hostname-to-IP rewrite, and the Host header."""
  fake = _fake_getaddrinfo([(2, 1, 6, '', ('93.184.216.34', 8080))])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    pinned, host_header, _ = validate_url_safe("http://api.example.com:8080/data")
  assert "93.184.216.34:8080" in pinned
  assert host_header == "api.example.com:8080"


def test_validate_url_preserves_https_scheme():
  """HTTPS scheme is kept in the pinned URL."""
  fake = _fake_getaddrinfo([(2, 1, 6, '', ('93.184.216.34', 443))])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    pinned, _, _ = validate_url_safe("https://secure.example.com/api")
  assert pinned.startswith("https://")
  assert "93.184.216.34" in pinned


def test_proxy_get_allows_opaque_app_frame_request(
  client, owner_token, monkeypatch
):
  """Opaque app frames may use the authenticated, read-only GET proxy."""
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  created = create_local_app(
    client, owner_auth, name="proxy-frame", description="test",
  )
  token_response = client.post("/api/auth/app-token", json={
    "app_id": created["id"],
  }, headers=owner_auth)
  assert token_response.status_code == 200, token_response.text

  async def fake_read(
    _client, url, max_bytes, *, headers, probe_truncation, cache_headers,
  ):
    assert url == "https://example.com/manifest.json"
    return _ExternalRead(
      body=b'{"id":"test"}', status_code=200,
      content_type="application/json", final_url=url, truncated=False,
    )

  monkeypatch.setattr("app.routes.proxy._read_external_get", fake_read)
  r = client.get(
    "/api/proxy",
    params={"url": "https://example.com/manifest.json"},
    headers={
      "Authorization": f"Bearer {token_response.json()['token']}",
      "Origin": "null",
      "Sec-Fetch-Site": "cross-site",
    },
  )
  assert r.status_code == 200
  assert r.json() == {"id": "test"}
  # `*`, not the echoed `null`: WebKit refuses to match the literal value
  # and blocks the response before the app frame sees it (see
  # test_opaque_origin_cors.py).
  assert r.headers["access-control-allow-origin"] == "*"


def test_proxy_post_rejects_foreign_cross_site_request(client, owner_token):
  """The mutation-capable POST proxy keeps the foreign-origin CSRF guard."""
  r = client.post(
    "/api/proxy",
    json={"url": "https://example.com/", "body": "value=1"},
    headers={
      "Authorization": f"Bearer {owner_token}",
      "Sec-Fetch-Site": "cross-site",
    },
  )
  assert r.status_code == 403


def test_proxy_post_allows_opaque_app_frame_request(
  client, owner_token, monkeypatch
):
  """Scoped Bearer auth distinguishes a real app fetch from foreign CSRF."""
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  created = create_local_app(
    client, owner_auth, name="proxy-post-frame", description="test",
  )
  token_response = client.post("/api/auth/app-token", json={
    "app_id": created["id"],
  }, headers=owner_auth)

  monkeypatch.setattr(
    "app.routes.proxy.validate_url_safe",
    lambda _url: (
      "https://93.184.216.34/data", "example.com", "example.com",
    ),
  )

  async def fake_capped_response(_client, _req, _url):
    return Response(content=b"ok", media_type="text/plain")

  monkeypatch.setattr("app.routes.proxy._capped_response", fake_capped_response)
  r = client.post(
    "/api/proxy",
    json={"url": "https://example.com/data", "body": "value=1"},
    headers={
      "Authorization": f"Bearer {token_response.json()['token']}",
      "Origin": "null",
      "Sec-Fetch-Site": "cross-site",
    },
  )
  assert r.status_code == 200
  assert r.text == "ok"
  # `*`, not the echoed `null`: WebKit refuses to match the literal value
  # and blocks the response before the app frame sees it (see
  # test_opaque_origin_cors.py).
  assert r.headers["access-control-allow-origin"] == "*"


def test_proxy_get_allows_same_origin_request(client, owner_token):
  """GET /api/proxy allows requests without Sec-Fetch-Site (e.g. curl, native)."""
  auth = {"Authorization": f"Bearer {owner_token}"}
  # We only need to confirm the CSRF guard passes — the URL itself can fail.
  r = client.get(
    "/api/proxy",
    params={"url": "http://this-domain-does-not-exist-xyz123.invalid/"},
    headers=auth,
  )
  # 400 = URL rejected by SSRF validator, not 403 CSRF → guard passed.
  assert r.status_code == 400


def test_proxy_releases_db_connection_before_external_fetch(
  client, owner_token, monkeypatch
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  from app.database import checked_out_connections
  baseline_checked_out = checked_out_connections()
  checked_out = []

  async def fake_read(
    _client, url, max_bytes, *, headers, probe_truncation, cache_headers,
  ):
    assert url == "https://example.com/data"
    checked_out.append(checked_out_connections())
    return _ExternalRead(
      body=b"ok", status_code=200, content_type="text/plain",
      final_url=url, truncated=False,
    )

  monkeypatch.setattr("app.routes.proxy._read_external_get", fake_read)

  r = client.get(
    "/api/proxy",
    params={"url": "https://example.com/data"},
    headers=auth,
  )

  assert r.status_code == 200
  assert r.text == "ok"
  assert checked_out and checked_out[0] <= baseline_checked_out


def test_proxy_post_passes_sni_hostname_as_text(
  client, owner_token, monkeypatch
):
  """The POST proxy uses the same httpcore-compatible SNI representation."""
  def fake_validate_url_safe(url):
    assert url == "https://example.com/data"
    return "https://93.184.216.34/data", "example.com", "example.com"

  async def fake_capped_response(_client, req, _url):
    assert req.extensions["sni_hostname"] == "example.com"
    assert isinstance(req.extensions["sni_hostname"], str)
    return Response(content=b"ok", media_type="text/plain")

  monkeypatch.setattr("app.routes.proxy.validate_url_safe", fake_validate_url_safe)
  monkeypatch.setattr("app.routes.proxy._capped_response", fake_capped_response)

  r = client.post(
    "/api/proxy",
    json={"url": "https://example.com/data", "body": "value=1"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert r.status_code == 200
  assert r.text == "ok"


def test_proxy_sends_identifiable_user_agent(client, owner_token, monkeypatch):
  """Both proxy verbs identify themselves instead of using httpx's default.

  Public APIs with client-identification policies (OSM Nominatim, Photon)
  reject "python-httpx/x.y" with 403, breaking every app that fetches them
  through the proxy.
  """
  from app.routes.proxy import _PROXY_USER_AGENT

  seen = []

  def fake_validate_url_safe(url):
    return "https://93.184.216.34/data", "example.com", "example.com"

  async def fake_capped_response(_client, req, _url):
    seen.append(req.headers.get("user-agent"))
    return Response(content=b"ok", media_type="text/plain")

  async def fake_read(
    _client, url, max_bytes, *, headers, probe_truncation, cache_headers,
  ):
    seen.append(headers["User-Agent"])
    return _ExternalRead(
      body=b"ok", status_code=200, content_type="text/plain",
      final_url=url, truncated=False,
    )

  monkeypatch.setattr("app.routes.proxy.validate_url_safe", fake_validate_url_safe)
  monkeypatch.setattr("app.routes.proxy._capped_response", fake_capped_response)
  monkeypatch.setattr("app.routes.proxy._read_external_get", fake_read)

  auth = {"Authorization": f"Bearer {owner_token}"}
  r = client.get(
    "/api/proxy",
    params={"url": "https://example.com/data"},
    headers=auth,
  )
  assert r.status_code == 200
  r = client.post(
    "/api/proxy",
    json={"url": "https://example.com/data", "body": "value=1"},
    headers=auth,
  )
  assert r.status_code == 200

  assert seen == [_PROXY_USER_AGENT, _PROXY_USER_AGENT]
  assert seen[0].startswith("Mobius/1.0")


def test_proxy_forwards_rate_limit_headers():
  class _RateLimitedResponse:
    status_code = 429
    headers = {
      "content-type": "text/plain",
      "retry-after": "60",
      "x-ratelimit-remaining": "0",
      "x-ratelimit-reset": "1783620000",
      "x-not-forwarded": "secret",
    }

    async def aiter_bytes(self):
      yield b"rate limited"

    async def aclose(self):
      pass

  class _Client:
    async def send(self, req, stream=True):
      return _RateLimitedResponse()

  response = asyncio.run(_capped_response(
    _Client(), httpx.Request("POST", "https://example.com/"),
    "https://example.com/",
  ))
  assert response.status_code == 429
  assert response.headers["retry-after"] == "60"
  assert response.headers["x-ratelimit-remaining"] == "0"
  assert response.headers["x-ratelimit-reset"] == "1783620000"
  assert "x-not-forwarded" not in response.headers


def _upstream(status, headers):
  return httpx.Response(status, headers=headers, content=b"")


def test_proxy_cache_headers_do_not_treat_quoted_extension_text_as_directives():
  from app.routes.proxy import private_browser_cache_headers

  for value in (
    'foo="x,max-age=86400"',
    r'foo="x\",max-age=86400"',
  ):
    assert private_browser_cache_headers(_upstream(200, {
      "cache-control": value,
    })) == {"cache-control": "private, no-cache", "vary": "Authorization"}
  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": 'foo="x,no-store,max-age=86400", max-age=60',
  })) == {"cache-control": "private, max-age=60", "vary": "Authorization"}


def test_proxy_cache_headers_never_let_a_shared_cache_store_owner_reads():
  from app.routes.proxy import private_browser_cache_headers as private_headers

  tile = private_headers(_upstream(200, {
    "cache-control": "public, max-age=604800, s-maxage=604800, immutable",
    "etag": '"tile-1"',
    "last-modified": "Wed, 07 Oct 2026 10:00:00 GMT",
    "expires": "Wed, 14 Oct 2026 10:00:00 GMT",
    "set-cookie": "session=upstream",
  }))
  assert tile == {
    "cache-control": "private, max-age=86400",
    "vary": "Authorization",
  }
  assert private_headers(_upstream(200, {
    "cache-control": "private, max-age=60",
  })) == {"cache-control": "private, max-age=60", "vary": "Authorization"}
  assert private_headers(_upstream(200, {
    "cache-control": "no-store", "etag": '"x"',
  })) == {"cache-control": "no-store"}
  assert private_headers(_upstream(200, {
    "cache-control": "no-cache", "etag": '"x"',
  })) == {"cache-control": "private, no-cache", "vary": "Authorization"}
  # Without explicit freshness the browser must fetch the resource again.
  assert private_headers(_upstream(200, {"etag": '"x"'})) == {
    "cache-control": "private, no-cache", "vary": "Authorization",
  }
  assert private_headers(_upstream(200, {"cache-control": "max-age=junk"})) == {
    "cache-control": "private, no-cache", "vary": "Authorization",
  }
  assert private_headers(_upstream(304, {
    "cache-control": "max-age=300", "etag": '"x"',
  })) == {"cache-control": "private, max-age=300", "vary": "Authorization"}
  assert private_headers(_upstream(500, {
    "cache-control": "max-age=300", "etag": '"x"',
  })) == {}


def test_proxy_cache_separates_bearer_identities_and_accounts_for_upstream_age():
  from datetime import UTC, datetime, timedelta
  from email.utils import format_datetime
  from app.routes.proxy import private_browser_cache_headers

  fresh = private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=300", "etag": '"x"', "age": "180",
  }))
  assert fresh == {
    "cache-control": "private, max-age=120",
    "vary": "Authorization",
  }
  old_date = format_datetime(datetime.now(UTC) - timedelta(seconds=280), usegmt=True)
  dated = private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=300", "date": old_date,
  }))
  assert dated["vary"] == "Authorization"
  assert dated["cache-control"].startswith("private, max-age=")
  assert 0 < int(dated["cache-control"].split("=")[1]) <= 20
  expired = private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=60", "age": "120",
  }))
  assert expired == {"cache-control": "private, no-cache", "vary": "Authorization"}
  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": "no-cache",
  })) == {"cache-control": "private, no-cache", "vary": "Authorization"}


def test_proxy_cache_preserves_origin_vary_and_revalidation_limits():
  from app.routes.proxy import private_browser_cache_headers

  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=300", "vary": "*",
  })) == {"cache-control": "private, max-age=300", "vary": "*"}
  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=300", "vary": "Accept-Language, AUTHORIZATION",
  })) == {
    "cache-control": "private, max-age=300",
    "vary": "Accept-Language, AUTHORIZATION",
  }
  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=300, must-revalidate",
  })) == {
    "cache-control": "private, max-age=300, must-revalidate",
    "vary": "Authorization",
  }
  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=0, max-age=86400",
  })) == {"cache-control": "private, no-cache", "vary": "Authorization"}
  assert private_browser_cache_headers(_upstream(200, {
    "cache-control": "max-age=86400, max-age=0",
  })) == {"cache-control": "private, no-cache", "vary": "Authorization"}


def test_truncated_proxy_body_never_carries_upstream_freshness_or_validators():
  from app.routes.proxy import (
    _MAX_BYTES, forward_upstream_cache_headers, private_browser_cache_headers,
  )

  class _Client:
    async def send(self, req, stream=True):
      return httpx.Response(200, headers={
        "cache-control": "max-age=3600", "etag": '"complete"',
        "last-modified": "Wed, 07 Oct 2026 10:00:00 GMT",
      }, content=b"x" * (_MAX_BYTES + 1))

  for policy in (private_browser_cache_headers, forward_upstream_cache_headers):
    response = asyncio.run(_capped_response(
      _Client(), object(), "https://example.com/", cache_headers=policy,
    ))
    assert len(response.body) == _MAX_BYTES
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers


def test_proxy_revalidation_returns_an_empty_not_modified_response():
  from app.routes.proxy import private_browser_cache_headers

  class _Client:
    async def send(self, req, stream=True):
      return httpx.Response(
        304, headers={"cache-control": "max-age=60", "etag": '"v1"'},
      )

  response = asyncio.run(_capped_response(
    _Client(), object(), "https://example.com/",
    cache_headers=private_browser_cache_headers,
  ))
  assert response.status_code == 304
  assert "content-type" not in response.headers
  assert response.body == b""
  assert response.headers["cache-control"] == "private, max-age=60"
  assert "etag" not in response.headers
  assert "last-modified" not in response.headers
  assert response.headers["vary"] == "Authorization"


def test_proxy_get_reuses_one_client_per_pinned_host_without_forwarding_validators(
  client, owner_token, monkeypatch,
):
  hosts = {
    "https://tile.example/1.png": ("https://93.184.216.34/1.png", "tile.example"),
    "https://tile.example/2.png": ("https://93.184.216.34/2.png", "tile.example"),
    # Same IP, different name: must never share a TLS connection pool.
    "https://other.example/1.png": ("https://93.184.216.34/1.png", "other.example"),
  }
  seen = []

  def fake_validate_url_safe(url):
    pinned, host = hosts[url]
    return pinned, host, host

  async def fake_send(client_, req, **kwargs):
    seen.append((client_, req))
    return _HopUpstream(200, b"png", {
      "content-type": "image/png", "cache-control": "max-age=60",
      "etag": '"tile-1"', "last-modified": "yesterday",
    })

  monkeypatch.setattr("app.routes.proxy.validate_url_safe", fake_validate_url_safe)
  monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
  auth = {"Authorization": f"Bearer {owner_token}"}
  for url in hosts:
    response = client.get(
      "/api/proxy",
      params={"url": url},
      headers={
        **auth, "If-None-Match": '"tile-1"', "Cookie": "a=b",
        "If-Modified-Since": "Wed, 01 Oct 2025 00:00:00 GMT",
      },
    )
    assert response.status_code == 200, response.text

  assert len(seen) == 3
  (first, first_req), (second, _), (other, _) = seen
  for _, req in seen:
    assert "if-none-match" not in req.headers
    assert "if-modified-since" not in req.headers
  assert first is second
  assert other is not first
  assert first.follow_redirects is False
  assert first_req.headers["host"] == "tile.example"
  assert "cookie" not in first_req.headers
  assert "authorization" not in first_req.headers
  assert response.headers["cache-control"] == "private, max-age=60"
  assert response.headers["vary"] == "Authorization"
  assert "etag" not in response.headers
  assert "last-modified" not in response.headers


class _HopUpstream:
  def __init__(self, status_code, body=b"", headers=None):
    self.status_code = status_code
    self._body = body
    self.headers = headers or {}
    self.closed = False

  async def aiter_bytes(self):
    yield self._body

  async def aclose(self):
    self.closed = True


def _hop_client(hops):
  """An httpx.AsyncClient stand-in answering each request with the next hop."""
  sent = []

  class _Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    async def aclose(self):
      pass

    def build_request(self, method, url, headers=None):
      return httpx.Request(method, url, headers=headers)

    async def send(self, req, stream=True):
      sent.append(req)
      return hops[len(sent) - 1]

  return _Client, sent


def _pin_every_hop(monkeypatch):
  validated = []

  def fake_validate(url):
    validated.append(url)
    host = urlparse(url).hostname
    return url.replace(host, "93.184.216.34"), host, host

  monkeypatch.setattr("app.routes.proxy.validate_url_safe", fake_validate)
  return validated


@pytest.mark.parametrize("headers", [None, {}])
def test_external_reader_uses_favicon_defaults_only_when_headers_are_unspecified(
  monkeypatch, headers,
):
  _pin_every_hop(monkeypatch)
  fake_client, sent = _hop_client([_HopUpstream(200, b"ok")])
  asyncio.run(_read_external_get(
    fake_client(), "https://example.com/", 1024, headers=headers,
  ))
  assert ("accept" in sent[0].headers) == (headers is None)
  assert ("user-agent" in sent[0].headers) == (headers is None)


@pytest.mark.parametrize("destination", ["site.example", "cdn.example"])
def test_proxy_get_follows_redirects_like_install(
  client, owner_token, monkeypatch, destination,
):
  """A manifest URL that redirects must preview as it installs."""
  validated = _pin_every_hop(monkeypatch)
  fake_client, sent = _hop_client([
    _HopUpstream(301, headers={
      "location": f"https://{destination}/mobius.json",
      "cache-control": "public, max-age=86400", "etag": '"redirect"',
    }),
    _HopUpstream(
      200, b'{"id":"moved"}',
      {
        "content-type": "application/json", "x-ratelimit-remaining": "9",
        "cache-control": "public, max-age=60", "etag": '"final"',
        "last-modified": "Wed, 01 Oct 2025 00:00:00 GMT",
      },
    ),
  ])
  created = []

  def make_client(**kwargs):
    assert kwargs["follow_redirects"] is False
    instance = fake_client()
    created.append(instance)
    return instance

  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", make_client)

  r = client.get(
    "/api/proxy",
    params={"url": "https://site.example/mobius.json"},
    headers={
      "Authorization": f"Bearer {owner_token}",
      "If-None-Match": '"previous"',
      "If-Modified-Since": "Wed, 01 Oct 2025 00:00:00 GMT",
      "Cookie": "session=local",
    },
  )

  assert r.status_code == 200, r.text
  assert r.json() == {"id": "moved"}
  assert r.headers["x-ratelimit-remaining"] == "9"
  assert validated == [
    "https://site.example/mobius.json", f"https://{destination}/mobius.json",
  ]
  assert len(created) == (1 if destination == "site.example" else 2)
  if destination != "site.example":
    assert created[0] is not created[1]
  assert r.headers["cache-control"] == "private, max-age=60"
  assert "etag" not in r.headers
  assert "last-modified" not in r.headers
  assert r.headers["vary"] == "Authorization"
  for req in sent:
    assert "if-none-match" not in req.headers
    assert "if-modified-since" not in req.headers
    assert "authorization" not in req.headers
    assert "cookie" not in req.headers
  assert sent[1].headers["host"] == destination
  assert sent[1].extensions["sni_hostname"] == destination


@pytest.mark.parametrize("content_type", [None, "application/json"])
def test_proxy_get_not_modified_preserves_absent_content_type(
  client, owner_token, monkeypatch, content_type,
):
  validated = _pin_every_hop(monkeypatch)
  headers = {"etag": '"v1"', "cache-control": "max-age=60"}
  if content_type is not None:
    headers["content-type"] = content_type
  upstream = _HopUpstream(304, headers=headers)
  fake_client, sent = _hop_client([upstream])
  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", lambda **_: fake_client())
  response = client.get(
    "/api/proxy", params={"url": "https://site.example/data"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 304, response.text
  assert response.content == b""
  assert response.headers.get("content-type") == content_type
  assert "etag" not in response.headers
  assert "last-modified" not in response.headers
  assert response.headers["cache-control"] == "private, max-age=60"
  assert validated == ["https://site.example/data"]
  assert len(sent) == 1
  assert upstream.closed


def test_proxy_get_redirector_cannot_short_circuit_with_final_origin_validator(
  client, owner_token, monkeypatch,
):
  validated = _pin_every_hop(monkeypatch)
  sent = []
  upstreams = []

  async def fake_send(client_, req, **kwargs):
    sent.append(req)
    if "if-none-match" in req.headers or "if-modified-since" in req.headers:
      upstream = _HopUpstream(304, headers={"etag": '"old-final"'})
    elif req.headers["host"] == "site.example":
      upstream = _HopUpstream(302, headers={
        "location": "https://new.example/data",
      })
    else:
      upstream = _HopUpstream(200, b"new bytes", {
        "etag": '"new-final"', "cache-control": "max-age=60",
      })
    upstreams.append(upstream)
    return upstream

  monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
  response = client.get(
    "/api/proxy", params={"url": "https://site.example/data"},
    headers={
      "Authorization": f"Bearer {owner_token}",
      "If-None-Match": '"old-final"',
      "If-Modified-Since": "Wed, 01 Oct 2025 00:00:00 GMT",
    },
  )
  assert response.status_code == 200, response.text
  assert response.content == b"new bytes"
  assert response.headers["content-type"] == "application/octet-stream"
  assert "etag" not in response.headers
  assert validated == ["https://site.example/data", "https://new.example/data"]
  assert len(sent) == 2
  assert all(upstream.closed for upstream in upstreams)


@pytest.mark.parametrize("error, status", [
  (httpx.ReadTimeout("body stalled"), 504),
  (httpx.ReadError("connection lost"), 502),
])
def test_proxy_get_classifies_mid_stream_failure_and_closes_response(
  client, owner_token, monkeypatch, error, status,
):
  _pin_every_hop(monkeypatch)

  class _FailingUpstream(_HopUpstream):
    async def aiter_bytes(self):
      yield b"partial body"
      raise error

  upstream = _FailingUpstream(200)
  fake_client, sent = _hop_client([upstream])
  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", lambda **_: fake_client())

  response = client.get(
    "/api/proxy", params={"url": "https://site.example/mobius.json"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == status, response.text
  assert "https://site.example/mobius.json" in response.json()["detail"]
  assert "partial body" not in response.text
  assert len(sent) == 1
  assert upstream.closed


@pytest.mark.parametrize("private_ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254"])
def test_proxy_get_rejects_private_redirect_before_sending_second_request(
  client, owner_token, monkeypatch, private_ip,
):
  resolved = []

  def fake_dns(host, *args, **kwargs):
    resolved.append(host)
    address = "93.184.216.34" if host == "site.example" else private_ip
    return [(2, 1, 6, "", (address, 0))]

  monkeypatch.setattr("app.net_utils.socket.getaddrinfo", fake_dns)
  upstream = _HopUpstream(302, headers={"location": f"http://{private_ip}/private"})
  fake_client, sent = _hop_client([upstream])
  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", lambda **_: fake_client())

  response = client.get(
    "/api/proxy", params={"url": "https://site.example/mobius.json"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 400, response.text
  assert "non-public address" in response.json()["detail"]
  assert resolved == ["site.example", private_ip]
  assert len(sent) == 1
  assert upstream.closed


def test_proxy_get_stops_after_the_install_redirect_limit(
  client, owner_token, monkeypatch,
):
  _pin_every_hop(monkeypatch)
  loop = _HopUpstream(302, headers={"location": "https://site.example/again"})
  fake_client, sent = _hop_client([loop] * 10)
  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", lambda **_: fake_client())

  r = client.get(
    "/api/proxy",
    params={"url": "https://site.example/start"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert r.status_code == 502
  assert "Too many redirects" in r.text
  assert len(sent) == 6


def test_proxy_get_truncates_an_oversized_response(
  client, owner_token, monkeypatch,
):
  from app.routes.proxy import _MAX_BYTES

  _pin_every_hop(monkeypatch)
  fake_client, _ = _hop_client([_HopUpstream(200, b"x" * (_MAX_BYTES + 1), {
    "cache-control": "public, max-age=3600", "etag": '"complete"',
  })])
  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", lambda **_: fake_client())

  r = client.get(
    "/api/proxy",
    params={"url": "https://site.example/huge.json"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert r.status_code == 200
  assert r.content == b"x" * _MAX_BYTES
  assert r.headers["cache-control"] == "no-store"
  assert "etag" not in r.headers


@pytest.mark.parametrize("public_transport", [False, True])
def test_proxy_post_and_public_transport_truncate_oversized_response(public_transport):
  from app.routes.proxy import _MAX_BYTES

  class _Client:
    async def send(self, req, stream=True):
      return _HopUpstream(200, b"x" * (_MAX_BYTES + 1))

  response = asyncio.run(_capped_response(
    _Client(), httpx.Request(
      "GET" if public_transport else "POST", "https://example.com/",
    ),
    "https://example.com/",
    cache_headers=forward_upstream_cache_headers if public_transport else None,
  ))
  assert response.status_code == 200
  assert response.body == b"x" * _MAX_BYTES


@pytest.mark.parametrize("public_transport", [False, True])
@pytest.mark.parametrize("error, status", [
  (httpx.ReadTimeout("body stalled"), 504),
  (httpx.ReadError("connection lost"), 502),
])
def test_proxy_post_and_public_transport_classify_midstream_failure_and_close(
  public_transport, error, status,
):
  class _FailingUpstream(_HopUpstream):
    async def aiter_bytes(self):
      yield b"partial body"
      raise error

  upstream = _FailingUpstream(200)

  class _Client:
    async def send(self, req, stream=True):
      return upstream

  # The request targets the DNS-pinned address; errors name the caller's URL.
  with pytest.raises(HTTPException) as raised:
    asyncio.run(_capped_response(
      _Client(), httpx.Request(
        "GET" if public_transport else "POST", "https://93.184.216.34/v1",
      ),
      "https://example.com/v1",
      cache_headers=forward_upstream_cache_headers if public_transport else None,
    ))
  assert raised.value.status_code == status
  assert "https://example.com/v1" in raised.value.detail
  assert "93.184.216.34" not in raised.value.detail
  assert upstream.closed


def test_declared_favicon_urls_accepts_unquoted_and_relative_icon_links():
  source = """
    <link rel=icon href=/img/logos/favicon.ico>
    <link rel="apple-touch-icon" href="../touch.png">
    <link rel="stylesheet" href="/not-an-icon.png">
    <link rel="icon" href="http://user:pass@example.com/private.ico">
  """
  assert _declared_favicon_urls(
    source, "https://docs.example.com/reference/start",
  ) == [
    "https://docs.example.com/img/logos/favicon.ico",
    "https://docs.example.com/touch.png",
  ]


def test_favicon_redirects_revalidate_and_pin_every_hop(monkeypatch):
  class _Upstream:
    def __init__(self, status_code, body=b"", headers=None):
      self.status_code = status_code
      self._body = body
      self.headers = headers or {}

    async def aiter_bytes(self):
      yield self._body

    async def aclose(self):
      pass

  class _Client:
    def __init__(self):
      self.requests = []

    def build_request(self, method, url, headers=None):
      return httpx.Request(method, url, headers=headers)

    async def send(self, req, stream=True):
      self.requests.append(req)
      if len(self.requests) == 1:
        return _Upstream(302, headers={"location": "https://cdn.example/icon.ico"})
      return _Upstream(
        200, b"\x00\x00\x01\x00", {"content-type": "image/x-icon"},
      )

  validated = []

  def fake_validate(url):
    validated.append(url)
    host = urlparse(url).hostname
    return url.replace(host, "93.184.216.34"), host, host

  monkeypatch.setattr("app.routes.proxy.validate_url_safe", fake_validate)
  client = _Client()
  result = asyncio.run(
    _read_external_get(client, "https://site.example/", 1024),
  )
  assert validated == [
    "https://site.example/",
    "https://cdn.example/icon.ico",
  ]
  assert result.body == b"\x00\x00\x01\x00"
  assert result.final_url == "https://cdn.example/icon.ico"
  assert client.requests[1].headers["host"] == "cdn.example"
  assert client.requests[1].extensions["sni_hostname"] == "cdn.example"


def test_favicon_endpoint_resolves_a_declared_custom_icon(
  client, owner_token, monkeypatch,
):
  reads = []

  async def fake_read(_client, url, max_bytes):
    reads.append((url, max_bytes))
    if url in {
      "https://docs.example/favicon.ico",
      "https://docs.example/favicon.svg",
      "https://docs.example/favicon.png",
      "https://docs.example/apple-touch-icon.png",
    }:
      return _ExternalRead(
        body=b"missing",
        status_code=404,
        content_type="text/plain",
        final_url=url,
        truncated=False,
      )
    if url == "https://docs.example/":
      return _ExternalRead(
        body=b'<link rel=icon href="/assets/site.svg">',
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url=url,
        truncated=False,
      )
    if url == "https://docs.example/assets/site.svg":
      return _ExternalRead(
        body=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        status_code=200,
        content_type="image/svg+xml",
        final_url=url,
        truncated=False,
      )
    raise AssertionError(f"unexpected icon read: {url}")

  monkeypatch.setattr("app.routes.proxy._read_external_get", fake_read)
  response = client.get(
    "/api/proxy/favicon",
    params={"url": "https://docs.example/"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200
  assert response.headers["content-type"] == "image/svg+xml"
  assert response.content.startswith(b"<svg")
  assert reads == [
    ("https://docs.example/favicon.ico", 256 * 1024),
    ("https://docs.example/favicon.svg", 256 * 1024),
    ("https://docs.example/favicon.png", 256 * 1024),
    ("https://docs.example/apple-touch-icon.png", 256 * 1024),
    ("https://docs.example/", 512 * 1024),
    ("https://docs.example/assets/site.svg", 256 * 1024),
  ]


def test_validate_url_rejects_if_any_ip_is_private():
  """If even one resolved address is internal, reject the entire request."""
  fake = _fake_getaddrinfo([
    (2, 1, 6, '', ('93.184.216.34', 80)),
    (2, 1, 6, '', ('192.168.1.1', 80)),
  ])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    with pytest.raises(HTTPException) as exc_info:
      validate_url_safe("http://rebind.attacker.com/")
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("address", [
  "198.18.0.1",  # benchmarking; commonly routed inside test networks
  "192.0.0.8",   # IETF special-purpose space
  "224.0.0.1",   # multicast
  "240.0.0.1",   # reserved
])
def test_validate_url_rejects_every_non_global_address(address):
  fake = _fake_getaddrinfo([(2, 1, 6, "", (address, 443))])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    with pytest.raises(HTTPException, match="non-public"):
      validate_url_safe("https://internal.example/mcp")


def test_validate_url_ipv6_brackets():
  """IPv6 validated IPs are wrapped in brackets in the pinned URL."""
  fake = _fake_getaddrinfo([
    (10, 1, 6, '', ('2606:2800:220:1:248:1893:25c8:1946', 80, 0, 0)),
  ])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    pinned, host_header, _ = validate_url_safe("http://example.com/")
  assert "[2606:2800:220:1:248:1893:25c8:1946]" in pinned
  assert host_header == "example.com"


def test_pooled_upstream_clients_never_carry_cookies_between_callers():
  """A session one caller receives must not ride on the next caller's request.

  Pooled clients serve the owner and every app token for a host, so a
  Set-Cookie from one response may never be replayed on another request.
  """
  import asyncio
  import threading
  from http.server import BaseHTTPRequestHandler, HTTPServer

  from app.pinned_http_clients import PinnedHostClientPool

  seen = []

  class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
      seen.append((self.path, self.headers.get("Cookie")))
      self.send_response(200)
      if self.path == "/login":
        self.send_header("Set-Cookie", "session=SECRET123; Path=/; HttpOnly")
      self.send_header("Content-Length", "2")
      self.end_headers()
      self.wfile.write(b"ok")

    def log_message(self, *args):
      pass

  server = HTTPServer(("127.0.0.1", 0), Upstream)
  port = server.server_address[1]
  threading.Thread(target=server.serve_forever, daemon=True).start()

  async def exercise():
    pool = PinnedHostClientPool()
    try:
      for path in ("/login", "/data"):
        async with pool.lease("api.example.test", "api.example.test") as client:
          request = client.build_request("GET", f"http://127.0.0.1:{port}{path}")
          request.headers["host"] = "api.example.test"
          response = await client.send(request)
          await response.aread()
    finally:
      await pool.close()

  try:
    asyncio.run(exercise())
  finally:
    server.shutdown()
  assert seen == [("/login", None), ("/data", None)]


def test_saturated_pool_answers_503_instead_of_queueing_forever():
  from app.pinned_http_clients import PinnedHostClientPool

  async def exercise():
    pool = PinnedHostClientPool(max_active=1, slot_wait=0.05)
    try:
      async with pool.lease("busy.example", "busy.example"):
        with pytest.raises(HTTPException) as exc:
          async with pool.lease("other.example", "other.example"):
            pass
      assert exc.value.status_code == 503
      assert exc.value.headers == {"Retry-After": "1"}
      # The refused caller never took a slot, so the next one gets it at once.
      async with pool.lease("other.example", "other.example"):
        pass
    finally:
      await pool.close()

  asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def test_drip_fed_upstream_cannot_hold_a_pool_slot_past_the_deadline(monkeypatch):
  """httpx times out each read, not the response, so an upstream sending a
  byte at a time must still lose its slot once the exchange deadline passes."""
  import threading
  import time
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

  from app.pinned_http_clients import PinnedHostClientPool

  class DripFeed(BaseHTTPRequestHandler):
    def do_GET(self):
      self.send_response(200)
      self.send_header("Content-Length", "1000000")
      self.end_headers()
      try:
        for _ in range(200):
          self.wfile.write(b"x")
          self.wfile.flush()
          time.sleep(0.02)
      except OSError:
        pass

    def log_message(self, *args):
      pass

  server = ThreadingHTTPServer(("127.0.0.1", 0), DripFeed)
  server.daemon_threads = True
  port = server.server_address[1]
  threading.Thread(target=server.serve_forever, daemon=True).start()
  monkeypatch.setattr("app.routes.proxy._EXCHANGE_DEADLINE", 0.3)

  async def exercise():
    pool = PinnedHostClientPool(max_active=1, slot_wait=0.05)
    try:
      with pytest.raises(HTTPException) as exc:
        async with pool.lease("drip.example", "drip.example") as client:
          request = client.build_request("GET", f"http://127.0.0.1:{port}/")
          await _capped_response(client, request, "http://drip.example/")
      assert exc.value.status_code == 504
      assert pool.metrics()["active_requests"] == 0
      async with pool.lease("next.example", "next.example"):
        pass
    finally:
      await pool.close()

  try:
    asyncio.run(asyncio.wait_for(exercise(), timeout=3))
  finally:
    server.shutdown()


@pytest.mark.parametrize("consumer", ["get", "post", "public_transport"])
def test_proxy_returns_exact_cap_without_waiting_for_stalled_upstream(
  monkeypatch, consumer,
):
  from app.routes.proxy import _MAX_BYTES, proxy_get

  _pin_every_hop(monkeypatch)

  class _StalledUpstream(_HopUpstream):
    async def aiter_bytes(self):
      yield b"x" * _MAX_BYTES
      await asyncio.Event().wait()

  upstream = _StalledUpstream(200)
  fake_client, _ = _hop_client([upstream])
  monkeypatch.setattr("app.routes.proxy.httpx.AsyncClient", lambda **_: fake_client())

  async def read():
    if consumer == "get":
      operation = proxy_get(
        "https://site.example/body", Request({"type": "http", "headers": []}),
      )
    else:
      operation = _capped_response(
        fake_client(), httpx.Request(
          "POST" if consumer == "post" else "GET", "https://site.example/body",
        ),
        "https://site.example/body",
        cache_headers=(
          forward_upstream_cache_headers if consumer == "public_transport" else None
        ),
      )
    return await asyncio.wait_for(operation, timeout=1)

  response = asyncio.run(read())
  assert response.status_code == 200
  assert response.body == b"x" * _MAX_BYTES
  assert upstream.closed


def test_favicon_reader_still_probes_for_truncation(monkeypatch):
  _pin_every_hop(monkeypatch)
  upstream = _HopUpstream(200, b"12345")
  fake_client, _ = _hop_client([upstream])

  result = asyncio.run(_read_external_get(
    fake_client(), "https://site.example/icon", 4,
  ))

  assert result.body == b"1234"
  assert result.truncated
  assert upstream.closed


def test_redirected_proxy_get_deadline_closes_body_and_releases_host_lease(
  monkeypatch,
):
  from app.routes import proxy

  _pin_every_hop(monkeypatch)
  monkeypatch.setattr(proxy, "_EXCHANGE_DEADLINE", 0.3)

  class StalledBody(_HopUpstream):
    async def aiter_bytes(self):
      yield b"prefix"
      await asyncio.Event().wait()

  redirect = _HopUpstream(302, headers={"location": "https://cdn.example/body"})
  stalled = StalledBody(200)
  fake_client, sent = _hop_client([redirect, stalled])
  monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **_: fake_client())

  async def exercise():
    with pytest.raises(HTTPException) as exc:
      await proxy.proxy_get(
        "https://site.example/start", Request({"type": "http", "headers": []}),
      )
    assert exc.value.status_code == 504
    assert len(sent) == 2
    assert redirect.closed and stalled.closed
    assert proxy._proxy_clients.metrics()["active_requests"] == 0
    async with proxy._proxy_clients.lease("next.example", "next.example"):
      pass

  asyncio.run(asyncio.wait_for(exercise(), timeout=2))
