"""Bounded keep-alive HTTP clients for DNS-pinned outbound requests.

SSRF validation (`app.net_utils.validate_url_safe`) pins each request to a
vetted IP while TLS and Host still name the declared host. One global
`httpx.AsyncClient` would be unsafe for that shape: HTTPX pools connections by
the pinned origin (scheme, IP, port), so two logical hosts sharing an IP could
reuse one TLS session negotiated for the other host's name. Keying clients by
(Host, SNI) keeps that boundary and still removes a TCP + TLS handshake per
map tile or API page.

Each caller owns its own pool so one traffic class (anonymous public apps, the
owner's app proxy) can never exhaust the other's capacity.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import http.cookiejar
import threading

import httpx
from fastapi import HTTPException


def _refuse_all_cookies() -> http.cookiejar.CookieJar:
  """A cookie jar whose policy accepts no cookie from any domain."""
  return http.cookiejar.CookieJar(
    policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]),
  )


@dataclass
class _PooledClient:
  client: httpx.AsyncClient
  leases: int = 0


class PinnedHostClientPool:
  """Host-isolated, LRU-bounded keep-alive clients with a concurrency cap.

  Clients never follow redirects: each hop must be re-validated and re-pinned
  by the caller, so automatic redirects would let a public URL bounce into the
  container network.

  A caller waits at most ``slot_wait`` seconds for one of the ``max_active``
  slots and then gets a 503, so a saturated pool sheds load instead of queueing
  requests without limit. How long a caller holds a slot is bounded by its
  exchange deadline (``_capped_response`` in app.routes.proxy).
  """

  def __init__(
    self, max_clients: int = 64, max_active: int = 64, slot_wait: float = 10,
  ):
    self.max_clients = max_clients
    self.max_active = max_active
    self.slot_wait = slot_wait
    self._lock = asyncio.Lock()
    self._capacity = asyncio.BoundedSemaphore(max_active)
    self._clients: OrderedDict[tuple[str, str], _PooledClient] = OrderedDict()
    self._metrics_lock = threading.Lock()
    self._metrics = {
      "clients": 0,
      "active_requests": 0,
      "active_limit": max_active,
      "clients_created": 0,
      "clients_evicted": 0,
    }

  def metrics(self) -> dict[str, int]:
    with self._metrics_lock:
      return dict(self._metrics)

  def _publish_metrics(self) -> None:
    with self._metrics_lock:
      self._metrics["clients"] = len(self._clients)
      self._metrics["active_requests"] = sum(
        entry.leases for entry in self._clients.values()
      )

  def _trim(self) -> list[httpx.AsyncClient]:
    retired: list[httpx.AsyncClient] = []
    while len(self._clients) > self.max_clients:
      idle_key = next(
        (key for key, entry in self._clients.items() if entry.leases == 0),
        None,
      )
      if idle_key is None:
        break
      retired.append(self._clients.pop(idle_key).client)
      with self._metrics_lock:
        self._metrics["clients_evicted"] += 1
    self._publish_metrics()
    return retired

  @asynccontextmanager
  async def lease(self, host_header: str, sni_host: str):
    try:
      async with asyncio.timeout(self.slot_wait):
        await self._capacity.acquire()
    except TimeoutError:
      raise HTTPException(
        status_code=503,
        detail="Too many outbound requests are in flight; retry shortly.",
        headers={"Retry-After": "1"},
      ) from None
    try:
      key = (host_header, sni_host)
      retired: list[httpx.AsyncClient] = []
      async with self._lock:
        entry = self._clients.pop(key, None)
        if entry is None:
          entry = _PooledClient(httpx.AsyncClient(
            # Pooled clients outlive one request and serve every caller that
            # reaches this host (the owner and every app token), so they must
            # never keep upstream cookies: a session set for one caller would
            # otherwise ride along on the next caller's request.
            cookies=_refuse_all_cookies(),
            follow_redirects=False,
            timeout=15,
            limits=httpx.Limits(
              max_connections=16,
              max_keepalive_connections=4,
              keepalive_expiry=30,
            ),
          ))
          with self._metrics_lock:
            self._metrics["clients_created"] += 1
        entry.leases += 1
        self._clients[key] = entry
        retired = self._trim()
      try:
        for client in retired:
          await client.aclose()
        yield entry.client
      finally:
        async with self._lock:
          live = self._clients.get(key)
          if live is entry:
            live.leases = max(0, live.leases - 1)
          retired = self._trim()
        for client in retired:
          await client.aclose()
    finally:
      self._capacity.release()

  async def close(self) -> None:
    async with self._lock:
      clients = [entry.client for entry in self._clients.values()]
      self._clients.clear()
      self._publish_metrics()
    for client in clients:
      await client.aclose()
