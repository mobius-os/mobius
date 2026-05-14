"""Tests for server-side proxy URL validation and DNS pinning."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.routes.proxy import _validate_url


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


# ---------------------------------------------------------------------------
# Unit tests for _validate_url DNS pinning
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(results):
  """Returns a mock for socket.getaddrinfo that returns the given tuples."""
  def _gai(host, port, *a, **kw):
    return results
  return _gai


def test_validate_url_pins_to_resolved_ip():
  """Pinned URL replaces hostname with the validated IP."""
  fake = _fake_getaddrinfo([(2, 1, 6, '', ('93.184.216.34', 80))])
  with patch("app.routes.proxy.socket.getaddrinfo", side_effect=fake):
    pinned, hostname = _validate_url("http://example.com/path?q=1")
  assert hostname == "example.com"
  assert "93.184.216.34" in pinned
  assert "example.com" not in pinned
  assert "/path?q=1" in pinned


def test_validate_url_preserves_port():
  """Custom ports survive the hostname-to-IP rewrite."""
  fake = _fake_getaddrinfo([(2, 1, 6, '', ('93.184.216.34', 8080))])
  with patch("app.routes.proxy.socket.getaddrinfo", side_effect=fake):
    pinned, hostname = _validate_url("http://api.example.com:8080/data")
  assert "93.184.216.34:8080" in pinned
  assert hostname == "api.example.com"


def test_validate_url_preserves_https_scheme():
  """HTTPS scheme is kept in the pinned URL."""
  fake = _fake_getaddrinfo([(2, 1, 6, '', ('93.184.216.34', 443))])
  with patch("app.routes.proxy.socket.getaddrinfo", side_effect=fake):
    pinned, _ = _validate_url("https://secure.example.com/api")
  assert pinned.startswith("https://")
  assert "93.184.216.34" in pinned


def test_validate_url_rejects_if_any_ip_is_private():
  """If even one resolved address is non-global, reject the entire request."""
  fake = _fake_getaddrinfo([
    (2, 1, 6, '', ('93.184.216.34', 80)),
    (2, 1, 6, '', ('192.168.1.1', 80)),
  ])
  with patch("app.routes.proxy.socket.getaddrinfo", side_effect=fake):
    with pytest.raises(HTTPException) as exc_info:
      _validate_url("http://rebind.attacker.com/")
    assert exc_info.value.status_code == 403


def test_validate_url_ipv6_brackets():
  """IPv6 validated IPs are wrapped in brackets in the pinned URL."""
  fake = _fake_getaddrinfo([
    (10, 1, 6, '', ('2606:2800:220:1:248:1893:25c8:1946', 80, 0, 0)),
  ])
  with patch("app.routes.proxy.socket.getaddrinfo", side_effect=fake):
    pinned, hostname = _validate_url("http://example.com/")
  assert "[2606:2800:220:1:248:1893:25c8:1946]" in pinned
  assert hostname == "example.com"
