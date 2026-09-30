"""Host-isolated, bounded HTTP reuse; no cookies or ambient proxy credentials.

Callers still validate and pin each URL and attach credentials per request.
Public and credentialed fetches own separate pools, sharing only this lifecycle.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
import threading

import httpx


class _NoCookies(DefaultCookiePolicy):
  def set_ok(self, cookie, request):
    return False


@dataclass
class _PooledClient:
  client: httpx.AsyncClient
  leases: int = 0


class PinnedFetchClientPool:
  """Bounded, host-isolated connection reuse for stateless upstream requests.

  DNS safety pins requests to an IP while TLS still uses the declared host.
  One global client would be unsafe because HTTPX pools by the pinned origin
  and could reuse a TLS connection for two logical hosts sharing an IP. Keying
  clients by Host/SNI preserves that boundary and still removes a handshake per
  map tile or API page.
  """

  def __init__(self, max_clients: int = 64, max_active: int = 64):
    self.max_clients = max_clients
    self.max_active = max_active
    self._lock = asyncio.Lock()
    self._capacity = asyncio.BoundedSemaphore(max_active)
    self._clients: OrderedDict[tuple[str, str], _PooledClient] = OrderedDict()
    self._metrics_lock = threading.Lock()
    self._metrics = {"clients": 0, "active_requests": 0, "active_limit": max_active,
                     "clients_created": 0, "clients_evicted": 0}

  def snapshot(self) -> dict:
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
    await self._capacity.acquire()
    try:
      key = (host_header, sni_host)
      retired: list[httpx.AsyncClient] = []
      async with self._lock:
        entry = self._clients.pop(key, None)
        if entry is None:
          entry = _PooledClient(httpx.AsyncClient(
            follow_redirects=False,
            timeout=15,
            trust_env=False,
            cookies=CookieJar(policy=_NoCookies()),
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
