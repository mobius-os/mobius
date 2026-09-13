"""Narrow async client for the root-owned runtime identity broker."""

from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_SOCKET = "/run/mobius-identity-broker.sock"


def broker_async_client(*, timeout: float = 10.0) -> httpx.AsyncClient:
  """Return an async client connected only to the private broker socket."""
  socket_path = os.environ.get("MOBIUS_IDENTITY_BROKER_SOCKET", DEFAULT_SOCKET)
  return httpx.AsyncClient(
    base_url="http://broker",
    transport=httpx.AsyncHTTPTransport(uds=socket_path),
    timeout=timeout,
  )


def broker_client(*, timeout: float = 10.0) -> httpx.Client:
  """Return a synchronous client connected only to the private broker socket."""
  socket_path = os.environ.get("MOBIUS_IDENTITY_BROKER_SOCKET", DEFAULT_SOCKET)
  return httpx.Client(
    base_url="http://broker",
    transport=httpx.HTTPTransport(uds=socket_path),
    timeout=timeout,
  )


async def broker_request(
  method: str,
  route: str,
  payload: dict[str, Any] | None = None,
  *,
  timeout: float = 10.0,
) -> dict[str, Any]:
  """Call one private identity route over the broker's Unix socket."""
  async with broker_async_client(timeout=timeout) as client:
    response = await client.request(method, route, json=payload)
    response.raise_for_status()
    value = response.json()
  if not isinstance(value, dict):
    raise ValueError("identity broker returned an invalid response")
  return value
