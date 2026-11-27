"""Per-client rate limits count the visitor, not the proxy in front of it."""

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app.net_utils import rate_limit_client

PROXY_PEER = "172.18.0.5"  # the bundled Caddy's address on the compose network
# Documentation ranges (203.0.113.0/24 ...) are not globally routable, so a
# client connecting straight from the internet needs a real public address.
PUBLIC_PEER = "8.8.8.8"


def _request(peer: str, *forwarded: str) -> Request:
  headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
  return Request({
    "type": "http", "method": "GET", "path": "/", "query_string": b"",
    "headers": headers, "client": (peer, 40000),
  })


def test_visitors_behind_the_proxy_are_counted_by_the_address_it_reports():
  assert rate_limit_client(_request(PROXY_PEER, "203.0.113.7")) == "203.0.113.7"
  assert rate_limit_client(_request("10.1.2.3", "2001:db8::5")) == "2001:db8::5"


def test_the_proxy_appended_entry_wins_over_one_the_visitor_sent():
  # A proxy that appends keeps the client's own claim first; only the last
  # entry is the address the proxy itself observed.
  spoofed = _request(PROXY_PEER, "198.51.100.1, 203.0.113.7")
  assert rate_limit_client(spoofed) == "203.0.113.7"
  assert rate_limit_client(_request(PROXY_PEER, "198.51.100.1", "203.0.113.7")) == "203.0.113.7"


def test_a_public_peer_cannot_choose_its_own_bucket():
  assert rate_limit_client(_request(PUBLIC_PEER, "198.51.100.1")) == PUBLIC_PEER
  # A dual-stack listener reports an IPv4 client as an IPv4-mapped address.
  mapped = f"::ffff:{PUBLIC_PEER}"
  assert rate_limit_client(_request(mapped, "198.51.100.1")) == mapped


@pytest.mark.parametrize("forwarded", [(), ("",), ("not-an-address",), ("203.0.113.7:443",)])
def test_an_internal_peer_without_a_usable_forwarded_address_is_counted_itself(forwarded):
  assert rate_limit_client(_request(PROXY_PEER, *forwarded)) == PROXY_PEER


def test_one_visitor_filling_the_login_limit_does_not_lock_out_another(client):
  """The sign-in limit was one bucket for everyone behind the proxy, so a
  stranger sending junk logins kept the owner out."""
  from app.routes.auth import _limiter

  behind_proxy = TestClient(client.app, client=(PROXY_PEER, 40000))
  _limiter.reset()
  _limiter.enabled = True
  try:
    def login(visitor: str, username: str) -> int:
      return behind_proxy.post(
        "/api/auth/token",
        data={"username": username, "password": "wrong"},
        headers={"X-Forwarded-For": visitor},
      ).status_code

    attacker = [login("198.51.100.1", "attacker") for _ in range(6)]
    assert attacker[:5] == [401] * 5
    assert attacker[5] == 429
    assert login("203.0.113.7", "owner") == 401
  finally:
    _limiter.enabled = False
    _limiter.reset()


def test_a_direct_client_rotating_forwarded_headers_still_shares_one_limit(client):
  from app.routes.auth import _limiter

  direct = TestClient(client.app, client=(PUBLIC_PEER, 40000))
  _limiter.reset()
  _limiter.enabled = True
  try:
    statuses = [
      direct.post(
        "/api/auth/token",
        data={"username": "attacker", "password": "wrong"},
        headers={"X-Forwarded-For": f"198.51.100.{n}"},
      ).status_code
      for n in range(6)
    ]
    assert statuses[5] == 429
  finally:
    _limiter.enabled = False
    _limiter.reset()


def test_a_strangers_failed_logins_never_lock_the_owner_out(client, owner_token):
  """The failed-login cooldown counts each visitor's own failures: keyed by
  username alone, one stranger could keep the owner locked out with a single
  wrong password every 15 minutes."""
  behind_proxy = TestClient(client.app, client=(PROXY_PEER, 40000))

  def login(visitor: str, password: str) -> int:
    return behind_proxy.post(
      "/api/auth/token",
      data={"username": "test", "password": password},
      headers={"X-Forwarded-For": visitor},
    ).status_code

  stranger = [login("198.51.100.1", "wrong") for _ in range(31)]
  assert stranger[:10] == [401] * 10 and stranger[-1] == 429
  # The stranger stays locked out; the owner, elsewhere, signs in.
  assert login("198.51.100.1", "testpassword123") == 429
  assert login("203.0.113.7", "testpassword123") == 200
