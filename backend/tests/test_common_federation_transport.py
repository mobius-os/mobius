"""Hermetic security contracts for Common's outbound federation transport."""

import base64
import gzip
import socket
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app import common_transport
from app.routes import common as common_routes


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

  monkeypatch.setattr(common_transport.httpx, "AsyncClient", client_factory)
  return options


@pytest.mark.asyncio
async def test_untrusted_envelope_cannot_fetch_actor_from_loopback(monkeypatch):
  _resolve_to(monkeypatch, "127.0.0.1")
  attempted = []

  class MustNotConnect:
    def __init__(self, **_kwargs):
      attempted.append(True)

  monkeypatch.setattr(common_transport.httpx, "AsyncClient", MustNotConnect)
  envelope = {
    "from": "127.0.0.1:8000",
    "sent_at": time.time(),
    "sig": "invalid",
  }

  with pytest.raises(HTTPException) as exc:
    await common_routes._verify_peer_envelope(envelope)

  assert exc.value.status_code == 403
  assert exc.value.detail == "Envelope signature is invalid."
  assert attempted == []


@pytest.mark.parametrize(
  ("host", "address"),
  (
    ("127.0.0.1", "127.0.0.1"),
    ("private.example", "10.23.4.5"),
    ("metadata.example", "169.254.169.254"),
  ),
)
@pytest.mark.asyncio
async def test_actor_fetch_rejects_non_public_destinations_before_connect(
  monkeypatch, host, address,
):
  _resolve_to(monkeypatch, address)

  class MustNotConnect:
    def __init__(self, **_kwargs):
      raise AssertionError("unsafe destination reached the HTTP client")

  monkeypatch.setattr(common_transport.httpx, "AsyncClient", MustNotConnect)

  with pytest.raises(HTTPException) as exc:
    await common_routes._fetch_actor(host, force=True)

  assert exc.value.status_code == 502
  assert exc.value.detail == "Peer could not be reached."


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_change_pinned_peer_host_or_tls_name(monkeypatch):
  dns_calls = _resolve_to(monkeypatch, _PUBLIC_IP)
  requests = []

  def handler(request):
    requests.append(request)
    return httpx.Response(200, json={"status": "ok"})

  client_options = _mock_network(monkeypatch, handler)
  response = await common_transport.federation_request(
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
  with pytest.raises(common_transport.FederationTransportError):
    await common_transport.federation_request(
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
  with pytest.raises(common_transport.FederationTransportError):
    await common_transport.federation_request(
      "GET", "https://peer.example/api/common/actor"
    )


@pytest.mark.asyncio
async def test_fetch_actor_rejects_invalid_actor_shape_with_generic_error(monkeypatch):
  _resolve_to(monkeypatch, _PUBLIC_IP)

  def handler(_request):
    return httpx.Response(200, json={
      "protocol": common_routes.PROTOCOL,
      "host": "peer.example",
      "public_key": [],
    })

  _mock_network(monkeypatch, handler)
  with pytest.raises(HTTPException) as exc:
    await common_routes._fetch_actor("peer.example", force=True)

  assert exc.value.status_code == 502
  assert exc.value.detail == "Peer returned an invalid actor card."


@pytest.mark.asyncio
async def test_peer_response_is_stopped_at_the_callers_byte_limit(monkeypatch):
  _resolve_to(monkeypatch, _PUBLIC_IP)

  def handler(_request):
    return httpx.Response(
      200, content=b"x" * 9, headers={"content-type": "application/json"}
    )

  _mock_network(monkeypatch, handler)
  with pytest.raises(common_transport.FederationTransportError):
    await common_transport.federation_request(
      "GET", "https://peer.example/api/common/actor", max_response_bytes=8
    )


@pytest.mark.asyncio
async def test_public_media_downloads_stay_pinned_and_content_validated(monkeypatch):
  _resolve_to(monkeypatch, _PUBLIC_IP)
  requests = []

  def handler(request):
    requests.append(request)
    if request.url.path.endswith("/avatar"):
      return httpx.Response(
        200, content=b"avatar", headers={"content-type": "image/png"}
      )
    return httpx.Response(
      200, content=b"not supported", headers={"content-type": "image/gif"}
    )

  _mock_network(monkeypatch, handler)

  assert await common_routes._download_avatar(
    "https://media.example/api/common/avatar"
  ) == b"avatar"
  with pytest.raises(ValueError, match="supported image"):
    await common_routes._download_board_media(
      "https://media.example/api/common/board/media/post-id"
    )
  assert [request.url.host for request in requests] == [_PUBLIC_IP, _PUBLIC_IP]
  assert all(request.headers["host"] == "media.example" for request in requests)


@pytest.mark.asyncio
async def test_valid_public_actor_fetch_verifies_peer_signature(monkeypatch):
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

  _resolve_to(monkeypatch, _PUBLIC_IP)
  host = "signed-peer.example"
  cache = common_routes._actor_verifier.cache_path(host)
  cache.unlink(missing_ok=True)
  key = Ed25519PrivateKey.generate()
  public_b64 = base64.b64encode(
    key.public_key().public_bytes(
      encoding=serialization.Encoding.Raw,
      format=serialization.PublicFormat.Raw,
    )
  ).decode()
  actor = {
    "protocol": common_routes.PROTOCOL,
    "host": host,
    "handle": "peer",
    "public_key": {"alg": "ed25519", "key_b64": public_b64},
  }
  requests = []

  def handler(request):
    requests.append(request)
    return httpx.Response(200, json=actor)

  _mock_network(monkeypatch, handler)
  envelope = {"from": host, "sent_at": time.time(), "kind": "proof"}
  envelope["sig"] = base64.b64encode(
    key.sign(common_routes._canonical(envelope))
  ).decode()

  assert await common_routes._verify_peer_envelope(envelope) == actor
  assert len(requests) == 1
  assert requests[0].url.host == _PUBLIC_IP
  assert requests[0].headers["host"] == host


def test_all_common_outbound_calls_use_the_federation_transport():
  route_files = (
    Path(common_routes.__file__),
    Path(common_routes.__file__).with_name("common_groups.py"),
    Path(common_routes.__file__).with_name("common_objects.py"),
  )
  for route_file in route_files:
    source = route_file.read_text()
    assert "federation_request(" in source
    assert "AsyncClient(" not in source
    assert "urlopen(" not in source
    assert "requests." not in source

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
  response = await common_transport.federation_request(
    "GET", "https://peer.example/api/common/actor"
  )
  assert response.json() == {"status": "ok"}
  assert response.content == payload
  assert "content-encoding" not in response.headers
  assert int(response.headers["content-length"]) == len(payload)
