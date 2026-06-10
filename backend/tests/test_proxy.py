"""Tests for server-side proxy URL validation and DNS pinning.

The proxy now shares the canonical SSRF validator with the install fetcher
(`app.net_utils.validate_url_safe`); the unit tests below exercise it directly
and the integration tests drive it through the proxy endpoints.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.net_utils import validate_url_safe


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


def test_proxy_get_rejects_cross_site_request(client, owner_token):
  """GET /api/proxy must reject cross-site requests (Task 1b CSRF fix)."""
  auth = {"Authorization": f"Bearer {owner_token}"}
  r = client.get(
    "/api/proxy",
    params={"url": "http://example.com/"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert r.status_code == 403


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


def test_validate_url_ipv6_brackets():
  """IPv6 validated IPs are wrapped in brackets in the pinned URL."""
  fake = _fake_getaddrinfo([
    (10, 1, 6, '', ('2606:2800:220:1:248:1893:25c8:1946', 80, 0, 0)),
  ])
  with patch("app.net_utils.socket.getaddrinfo", side_effect=fake):
    pinned, host_header, _ = validate_url_safe("http://example.com/")
  assert "[2606:2800:220:1:248:1893:25c8:1946]" in pinned
  assert host_header == "example.com"
