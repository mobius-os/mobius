"""Hermetic security contracts for the platform's federation transport."""

import gzip
import socket

import httpx
import pytest
from fastapi import HTTPException

from app import federation_transport


_REAL_ASYNC_CLIENT = httpx.AsyncClient
_PUBLIC_IP = "93.184.216.34"


def _resolve_to(monkeypatch, address: str):
  calls = []

  def getaddrinfo(host, port, *_args, **_kwargs):
    calls.append((host, port))
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

  monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
  return calls


def _mock_network(monkeypatch, handler):
  options = []

  def client_factory(**kwargs):
    options.append(kwargs.copy())
    return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

  monkeypatch.setattr(federation_transport.httpx, "AsyncClient", client_factory)
  return options


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_change_pinned_peer_host_or_tls_name(monkeypatch):
  dns_calls = _resolve_to(monkeypatch, _PUBLIC_IP)
  requests = []

  def handler(request):
    requests.append(request)
    return httpx.Response(200, json={"status": "ok"})

  client_options = _mock_network(monkeypatch, handler)
  response = await federation_transport.federation_request(
    "GET", "https://peer.example/api/common/actor", params={"view": "card"}
  )

  assert response.json() == {"status": "ok"}
  assert dns_calls == [("peer.example", None)]
  assert len(requests) == 1
  assert requests[0].url.host == _PUBLIC_IP
  assert requests[0].url.params["view"] == "card"
  assert requests[0].headers["host"] == "peer.example"
  assert requests[0].extensions["sni_hostname"] == "peer.example"
  assert client_options[0]["follow_redirects"] is False
  assert client_options[0]["trust_env"] is False
  assert client_options[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_redirect_to_metadata_is_rejected_without_second_request(monkeypatch):
  _resolve_to(monkeypatch, _PUBLIC_IP)
  requests = []

  def handler(request):
    requests.append(request)
    return httpx.Response(
      302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
    )

  _mock_network(monkeypatch, handler)
  with pytest.raises(federation_transport.FederationTransportError):
    await federation_transport.federation_request(
      "GET", "https://peer.example/api/common/actor"
    )

  assert len(requests) == 1
  assert requests[0].url.host == _PUBLIC_IP


@pytest.mark.parametrize(
  ("content_type", "body"),
  (
    ("text/html", b"{}"),
    ("application/json", b"not-json"),
    ("application/json", b"[]"),
  ),
)
@pytest.mark.asyncio
async def test_invalid_peer_json_response_is_rejected(
  monkeypatch, content_type, body,
):
  _resolve_to(monkeypatch, _PUBLIC_IP)

  def handler(_request):
    return httpx.Response(200, content=body, headers={"content-type": content_type})

  _mock_network(monkeypatch, handler)
  with pytest.raises(federation_transport.FederationTransportError):
    await federation_transport.federation_request(
      "GET", "https://peer.example/api/common/actor"
    )


@pytest.mark.asyncio
async def test_peer_response_is_stopped_at_the_callers_byte_limit(monkeypatch):
  _resolve_to(monkeypatch, _PUBLIC_IP)

  def handler(_request):
    return httpx.Response(
      200, content=b"x" * 9, headers={"content-type": "application/json"}
    )

  _mock_network(monkeypatch, handler)
  with pytest.raises(federation_transport.FederationTransportError):
    await federation_transport.federation_request(
      "GET", "https://peer.example/api/common/actor", max_response_bytes=8
    )


@pytest.mark.asyncio
async def test_compressed_peer_response_is_decoded_once(monkeypatch):
  _resolve_to(monkeypatch, _PUBLIC_IP)
  payload = b'{"status":"ok"}'
  compressed = gzip.compress(payload)

  def handler(_request):
    return httpx.Response(200, content=compressed, headers={
      "content-type": "application/json",
      "content-encoding": "gzip",
      "content-length": str(len(compressed)),
    })

  _mock_network(monkeypatch, handler)
  response = await federation_transport.federation_request(
    "GET", "https://peer.example/api/common/actor"
  )
  assert response.json() == {"status": "ok"}
  assert response.content == payload
  assert "content-encoding" not in response.headers
  assert int(response.headers["content-length"]) == len(payload)
