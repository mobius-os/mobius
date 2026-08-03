"""Provider-neutral MCP connection registry and secret-boundary tests."""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connectors as core
from app import models
from app.database import checked_out_connections
from app.routes import connectors as connector_routes


def test_slug_auth_and_token_helpers_are_provider_consistent():
  assert core.slugify("Context7 Docs!") == "context7_docs"
  assert core.slugify(" -- ") == "connector"
  assert len(core.slugify("x" * 200)) == 48
  assert core.auth_headers("Authorization", "sk-123") == {
    "Authorization": "Bearer sk-123",
  }
  assert core.auth_headers("authorization", "bearer sk-123") == {
    "authorization": "bearer sk-123",
  }
  assert core.auth_headers("X-Api-Key", "sk-123") == {
    "X-Api-Key": "sk-123",
  }
  assert core.bare_secret("Authorization", "Bearer sk-123") == "sk-123"
  assert core.bare_secret("X-Api-Key", "Bearer sk-123") == "Bearer sk-123"
  with pytest.raises(core.ConnectorError, match="reserved"):
    core.validate_auth_header("MCP-Protocol-Version")


@pytest.mark.asyncio
async def test_rpc_parser_accepts_json_and_multiline_sse_events():
  response = httpx.Response(
    200,
    headers={"content-type": "text/event-stream"},
    text=(
      'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n'
      'event: message\ndata: {"jsonrpc":"2.0",\n'
      'data: "id":2,"result":{}}\n\n'
    ),
  )
  assert (await core._matching_rpc(response, 2))["id"] == 2
  plain = httpx.Response(
    200,
    headers={"content-type": "application/json"},
    json={"jsonrpc": "2.0", "id": 1, "result": {}},
  )
  assert (await core._matching_rpc(plain, 1))["id"] == 1


class _StopAfterMatchStream(httpx.AsyncByteStream):
  async def __aiter__(self):
    yield b'data: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
    raise AssertionError("matching SSE response was not consumed incrementally")


class _BytesStream(httpx.AsyncByteStream):
  def __init__(self, *chunks: bytes):
    self.chunks = chunks

  async def __aiter__(self):
    for chunk in self.chunks:
      yield chunk


@pytest.mark.asyncio
async def test_rpc_parser_stops_a_live_sse_stream_after_matching_id():
  response = httpx.Response(
    200,
    headers={"content-type": "text/event-stream"},
    stream=_StopAfterMatchStream(),
  )
  assert (await core._matching_rpc(response, 7))["id"] == 7


@pytest.mark.asyncio
async def test_handshake_negotiates_session_and_paginates_tools(monkeypatch):
  calls = []

  def handler(request: httpx.Request) -> httpx.Response:
    calls.append(request)
    assert request.url.host == "203.0.113.8"
    assert request.headers["host"] == "mcp.example"
    if request.method == "DELETE":
      assert request.headers["mcp-session-id"] == "session-1"
      return httpx.Response(204, request=request)
    body = json.loads(request.content)
    if body["method"] == "server/discover":
      return httpx.Response(404, request=request)
    if body["method"] == "initialize":
      return httpx.Response(
        200,
        request=request,
        headers={
          "content-type": "application/json",
          "mcp-session-id": "session-1",
        },
        json={
          "jsonrpc": "2.0",
          "id": 1,
          "result": {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "Example MCP"},
            "capabilities": {"tools": {}},
          },
        },
      )
    assert request.headers["mcp-session-id"] == "session-1"
    assert request.headers["mcp-protocol-version"] == "2025-11-25"
    if body["method"] == "notifications/initialized":
      return httpx.Response(202, request=request)
    if body["params"].get("cursor") == "next":
      return httpx.Response(
        200,
        request=request,
        headers={"content-type": "application/json"},
        json={
          "jsonrpc": "2.0",
          "id": 3,
          "result": {"tools": [{"name": "second"}]},
        },
      )
    return httpx.Response(
      200,
      request=request,
      headers={"content-type": "text/event-stream"},
      text=(
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":2,"result":'
        '{"tools":[{"name":"first"}],"nextCursor":"next"}}\n\n'
      ),
    )

  monkeypatch.setattr(
    core,
    "_safe_endpoint",
    lambda _url: ("https://203.0.113.8/mcp", "mcp.example", "mcp.example"),
  )
  real_async_client = httpx.AsyncClient
  monkeypatch.setattr(
    core.httpx,
    "AsyncClient",
    lambda **kwargs: real_async_client(
      transport=httpx.MockTransport(handler),
      timeout=kwargs.get("timeout"),
      follow_redirects=kwargs.get("follow_redirects", False),
    ),
  )

  result = await core.handshake(
    "https://mcp.example/mcp", "Authorization", "secret-key",
  )
  assert result["name"] == "Example MCP"
  assert [tool["name"] for tool in result["tools"]] == ["first", "second"]
  assert calls[0].headers["authorization"] == "Bearer secret-key"
  assert calls[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_handshake_prefers_stateless_2026_discovery(monkeypatch):
  methods = []

  def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body["method"]
    methods.append(method)
    assert request.headers["mcp-protocol-version"] == "2026-07-28"
    assert request.headers["mcp-method"] == method
    metadata = body["params"]["_meta"]
    assert metadata["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert metadata["io.modelcontextprotocol/clientCapabilities"] == {}
    if method == "server/discover":
      result = {
        "resultType": "complete",
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {}},
        "ttlMs": 60000,
        "cacheScope": "private",
        "_meta": {
          "io.modelcontextprotocol/serverInfo": {
            "name": "Modern MCP",
            "version": "2.0",
          },
        },
      }
    else:
      assert method == "tools/list"
      result = {
        "resultType": "complete",
        "tools": [{"name": "modern_lookup"}],
        "ttlMs": 60000,
        "cacheScope": "private",
      }
    return httpx.Response(
      200,
      request=request,
      headers={"content-type": "application/json"},
      json={"jsonrpc": "2.0", "id": body["id"], "result": result},
    )

  monkeypatch.setattr(
    core,
    "_safe_endpoint",
    lambda _url: ("https://203.0.113.8/mcp", "mcp.example", "mcp.example"),
  )
  real_async_client = httpx.AsyncClient
  monkeypatch.setattr(
    core.httpx,
    "AsyncClient",
    lambda **kwargs: real_async_client(
      transport=httpx.MockTransport(handler),
      timeout=kwargs.get("timeout"),
      follow_redirects=False,
    ),
  )

  result = await core.handshake("https://mcp.example/mcp")
  assert methods == ["server/discover", "tools/list"]
  assert result["name"] == "Modern MCP"
  assert result["protocol_version"] == "2026-07-28"
  assert [tool["name"] for tool in result["tools"]] == ["modern_lookup"]


@pytest.mark.asyncio
async def test_handshake_redacts_secret_echoes_and_rpc_errors(monkeypatch):
  error_mode = False

  def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["method"] == "server/discover":
      return httpx.Response(404, request=request)
    if body["method"] == "initialize":
      if error_mode:
        return httpx.Response(
          200,
          request=request,
          headers={"content-type": "application/json"},
          json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"message": "credential echo-secret was rejected"},
          },
        )
      return httpx.Response(
        200,
        request=request,
        headers={"content-type": "application/json"},
        json={
          "jsonrpc": "2.0",
          "id": 1,
          "result": {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "echo-secret service"},
          },
        },
      )
    if body["method"] == "notifications/initialized":
      return httpx.Response(202, request=request)
    return httpx.Response(
      200,
      request=request,
      headers={"content-type": "application/json"},
      json={
        "jsonrpc": "2.0",
        "id": body["id"],
        "result": {
          "tools": [{
            "name": "lookup",
            "description": "Bearer echo-secret",
            "inputSchema": {
              "type": "object",
              "properties": {"echo-secret": {"default": "echo-secret"}},
            },
          }],
        },
      },
    )

  monkeypatch.setattr(
    core,
    "_safe_endpoint",
    lambda _url: ("https://203.0.113.8/mcp", "mcp.example", "mcp.example"),
  )
  real_async_client = httpx.AsyncClient
  monkeypatch.setattr(
    core.httpx,
    "AsyncClient",
    lambda **kwargs: real_async_client(
      transport=httpx.MockTransport(handler),
      timeout=kwargs.get("timeout"),
      follow_redirects=False,
    ),
  )

  result = await core.handshake(
    "https://mcp.example/mcp", "Authorization", "echo-secret",
  )
  assert "echo-secret" not in json.dumps(result)
  assert "[redacted]" in json.dumps(result)

  error_mode = True
  with pytest.raises(core.ConnectorError) as raised:
    await core.handshake(
      "https://mcp.example/mcp", "Authorization", "echo-secret",
    )
  assert "echo-secret" not in str(raised.value)


class _NeverEndingStream(httpx.AsyncByteStream):
  async def __aiter__(self):
    while True:
      await asyncio.sleep(3600)
      yield b": heartbeat\n\n"


@pytest.mark.asyncio
async def test_handshake_has_one_bounded_deadline_for_live_sse(monkeypatch):
  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
      200,
      request=request,
      headers={"content-type": "text/event-stream"},
      stream=_NeverEndingStream(),
    )

  monkeypatch.setattr(
    core,
    "_safe_endpoint",
    lambda _url: ("https://203.0.113.8/mcp", "mcp.example", "mcp.example"),
  )
  monkeypatch.setattr(core, "_HANDSHAKE_DEADLINE_SECONDS", 0.02)
  real_async_client = httpx.AsyncClient
  monkeypatch.setattr(
    core.httpx,
    "AsyncClient",
    lambda **kwargs: real_async_client(
      transport=httpx.MockTransport(handler),
      timeout=kwargs.get("timeout"),
      follow_redirects=False,
    ),
  )
  with pytest.raises(core.ConnectorError, match="timed out"):
    await core.handshake("https://mcp.example/mcp")
  assert core._HANDSHAKE_DEADLINE_SECONDS <= 22


@pytest.mark.asyncio
async def test_handshake_deadline_includes_dns_validation(monkeypatch):
  def slow_endpoint(_url):
    time.sleep(0.08)
    return "https://203.0.113.8/mcp", "mcp.example", "mcp.example"

  monkeypatch.setattr(core, "_safe_endpoint", slow_endpoint)
  monkeypatch.setattr(core, "_HANDSHAKE_DEADLINE_SECONDS", 0.01)
  with pytest.raises(core.ConnectorError, match="timed out"):
    await core.handshake("https://mcp.example/mcp")


@pytest.mark.parametrize("url", [
  "https://example.test:not-a-port/mcp",
  "https://example.test:99999/mcp",
  "https://[broken/mcp",
])
def test_malformed_endpoint_and_port_are_connector_errors(url):
  with pytest.raises(core.ConnectorError):
    core._safe_endpoint(url)


def test_secret_roundtrip_and_garbage_are_write_only():
  token = core.encrypt_secret("sk-value")
  assert token != "sk-value"
  assert core.decrypt_secret(token) == "sk-value"
  with pytest.raises(core.ConnectorError):
    core.decrypt_secret("not-a-fernet-token")


def test_broker_capability_expires_and_is_connector_scoped():
  assert core._BROKER_CAPABILITY_TTL_SECONDS <= 24 * 60 * 60
  capability = core.mint_broker_capability(41)
  core.verify_broker_capability(capability, 41)
  with pytest.raises(core.ConnectorError, match="not valid for"):
    core.verify_broker_capability(capability, 42)

  expired = core._broker_fernet().encrypt_at_time(
    b'{"connector_id":41}', current_time=0,
  ).decode()
  with pytest.raises(core.ConnectorError, match="not valid"):
    core.verify_broker_capability(expired, 41)


class _FakeQuery:
  def __init__(self, rows):
    self.rows = rows

  def filter(self, *_args, **_kwargs):
    return self

  def order_by(self, *_args, **_kwargs):
    return self

  def all(self):
    return self.rows


class _FakeDb:
  def __init__(self, rows):
    self.rows = rows

  def query(self, _model):
    return _FakeQuery(self.rows)


def _row(row_id, slug, url, auth_header=None, secret=None):
  return models.Connector(
    id=row_id,
    slug=slug,
    name=slug,
    url=url,
    enabled=True,
    auth_header=auth_header,
    auth_value_encrypted=core.encrypt_secret(secret) if secret else None,
    tools_json=[],
    est_tokens=0,
    status="ok",
  )


def test_turn_plan_serves_both_providers_without_config_secrets():
  plan = core.build_turn_plan(_FakeDb([
    _row(4, "docs", "https://docs.example/mcp"),
    _row(9, "search", "https://search.example/mcp", "Authorization", "sk-9"),
    _row(12, "data", "https://data.example/mcp", "X-Api-Key", "key-12"),
  ]))

  expected_keys = {
    "mobius_connector_4_docs",
    "mobius_connector_9_search",
    "mobius_connector_12_data",
  }
  assert set(plan.claude_servers) == expected_keys
  servers = plan.codex_config["mcp_servers"]
  assert set(servers) == expected_keys
  for key in expected_keys:
    connector_id = int(key.split("_")[2])
    claude = plan.claude_servers[key]
    codex = servers[key]
    expected_url = f"http://127.0.0.1:9/api/connectors/{connector_id}/broker"
    assert claude["url"] == expected_url
    assert codex["url"] == expected_url
    assert "example" not in claude["url"]
    capability = claude["headers"]["Authorization"].removeprefix("Bearer ")
    core.verify_broker_capability(capability, connector_id)
    env_var = codex["bearer_token_env_var"]
    assert plan.codex_env[env_var] == capability
  assert "sk-9" not in repr(plan)
  assert "sk-9" not in repr(plan.codex_config)
  assert "key-12" not in repr(plan.codex_config)


def test_claude_config_is_anonymous_and_disappears_after_startup_window():
  plan = core.ConnectorTurnPlan(
    claude_servers={
      "search": {
        "type": "http",
        "url": "https://search.example/mcp",
        "headers": {"Authorization": "Bearer secret-in-file"},
      },
    },
  )
  with core.claude_mcp_config_path(plan) as path:
    assert path.startswith(f"/proc/{os.getpid()}/fd/")
    assert "secret-in-file" not in path
    with open(path, encoding="utf-8") as config_file:
      payload = json.load(config_file)
    assert payload["mcpServers"]["search"]["headers"]["Authorization"] == (
      "Bearer secret-in-file"
    )
  assert not os.path.exists(path)


def test_connector_routes_keep_keys_out_of_responses(
  client, auth, db, monkeypatch,
):
  probe = {
    "name": "Example MCP",
    "tools": [{"name": "lookup", "description": "Look up a record."}],
    "est_tokens": 42,
    "protocol_version": "2025-11-25",
  }
  probe_pool_counts = []

  async def probe_without_db_lease(*_args, **_kwargs):
    probe_pool_counts.append(checked_out_connections())
    return probe

  handshake = AsyncMock(side_effect=probe_without_db_lease)
  monkeypatch.setattr("app.routes.connectors.core.handshake", handshake)

  add_baseline = checked_out_connections()
  created = client.post("/api/connectors", headers=auth, json={
    "url": "https://mcp.example/mcp",
    "auth_value": "top-secret",
  })
  assert created.status_code == 201, created.text
  body = created.json()
  assert body["name"] == "Example MCP"
  assert body["has_auth"] is True
  assert body["providers"] == ["claude", "codex"]
  assert "top-secret" not in created.text
  assert probe_pool_counts == [add_baseline]

  stored = db.query(models.Connector).one()
  assert stored.auth_value_encrypted != "top-secret"
  assert core.decrypt_secret(stored.auth_value_encrypted) == "top-secret"

  listed = client.get("/api/connectors", headers=auth)
  assert listed.status_code == 200
  assert "top-secret" not in listed.text

  toggled = client.patch(
    f"/api/connectors/{stored.id}", headers=auth, json={"enabled": False},
  )
  assert toggled.status_code == 200
  assert toggled.json()["enabled"] is False

  # The direct assertion session above owns its own checkout; release it so
  # the refresh probe can prove the request-owned lease is gone independently.
  db.close()
  refresh_baseline = checked_out_connections()
  refreshed = client.post(
    f"/api/connectors/{stored.id}/refresh", headers=auth,
  )
  assert refreshed.status_code == 200
  assert handshake.await_count == 2
  assert probe_pool_counts[-1] == refresh_baseline


def test_broker_requires_loopback_and_connector_scoped_capability(
  client, db,
):
  row = _row(
    21, "private", "https://private.example/mcp",
    "Authorization", "upstream-secret",
  )
  db.add(row)
  db.commit()
  capability = core.mint_broker_capability(row.id)

  denied = client.get(
    f"/api/connectors/{row.id}/broker",
    headers={"Authorization": f"Bearer {capability}"},
  )
  assert denied.status_code == 403

  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    missing = loopback.get(f"/api/connectors/{row.id}/broker")
    assert missing.status_code == 401
    wrong_scope = loopback.get(
      f"/api/connectors/{row.id}/broker",
      headers={
        "Authorization": f"Bearer {core.mint_broker_capability(row.id + 1)}",
      },
    )
    assert wrong_scope.status_code == 401


def test_broker_revalidates_pins_and_streams_each_mcp_method(
  client, db, monkeypatch,
):
  row = _row(
    22, "remote", "https://remote.example/mcp",
    "X-Api-Key", "upstream-secret",
  )
  db.add(row)
  db.commit()
  capability = core.mint_broker_capability(row.id)
  validated = []
  upstream_calls = []

  def safe_endpoint(url):
    validated.append(url)
    return "https://203.0.113.9/mcp", "remote.example", "remote.example"

  def handler(request: httpx.Request) -> httpx.Response:
    upstream_calls.append(request)
    assert request.url.host == "203.0.113.9"
    assert request.headers["host"] == "remote.example"
    assert request.headers["x-api-key"] == "upstream-secret"
    assert "Bearer " not in request.headers.get("authorization", "")
    assert request.headers["mcp-protocol-version"] == "2025-11-25"
    assert request.headers["mcp-method"] == "tools/list"
    assert request.headers["mcp-name"] == "lookup"
    assert request.headers["mcp-param-region"] == "us-west1"
    return httpx.Response(
      200,
      request=request,
      headers={
        "content-type": "text/event-stream",
        "mcp-session-id": "session-22",
        "content-encoding": "identity",
        "content-length": "999",
        "x-untrusted": "not-forwarded",
      },
      stream=_BytesStream(
        b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"echo\":\"upstream-",
        b"secret\"}}\n\n",
      ),
    )

  monkeypatch.setattr(core, "_safe_endpoint", safe_endpoint)
  real_async_client = httpx.AsyncClient
  monkeypatch.setattr(
    connector_routes.httpx,
    "AsyncClient",
    lambda **kwargs: real_async_client(
      transport=httpx.MockTransport(handler),
      timeout=kwargs.get("timeout"),
      follow_redirects=False,
    ),
  )
  headers = {
    "Authorization": f"Bearer {capability}",
    "MCP-Protocol-Version": "2025-11-25",
    "Mcp-Method": "tools/list",
    "Mcp-Name": "lookup",
    "Mcp-Param-Region": "us-west1",
    "Content-Type": "application/json",
  }
  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    responses = [
      loopback.get(f"/api/connectors/{row.id}/broker", headers=headers),
      loopback.post(
        f"/api/connectors/{row.id}/broker",
        headers=headers,
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
      ),
      loopback.delete(f"/api/connectors/{row.id}/broker", headers=headers),
    ]

  assert [response.status_code for response in responses] == [200, 200, 200]
  assert [request.method for request in upstream_calls] == ["GET", "POST", "DELETE"]
  assert upstream_calls[1].content == (
    b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  )
  assert validated == [row.url, row.url, row.url]
  assert all(response.headers["mcp-session-id"] == "session-22" for response in responses)
  assert all("x-untrusted" not in response.headers for response in responses)
  assert all("content-encoding" not in response.headers for response in responses)
  assert all("upstream-secret" not in response.text for response in responses)
  assert all("[redacted]" in response.text for response in responses)


def test_broker_refuses_redirects_and_releases_upstream(
  client, db, monkeypatch,
):
  row = _row(23, "redirect", "https://remote.example/mcp")
  db.add(row)
  db.commit()
  capability = core.mint_broker_capability(row.id)
  monkeypatch.setattr(
    core,
    "_safe_endpoint",
    lambda _url: ("https://203.0.113.9/mcp", "remote.example", "remote.example"),
  )
  real_async_client = httpx.AsyncClient
  monkeypatch.setattr(
    connector_routes.httpx,
    "AsyncClient",
    lambda **kwargs: real_async_client(
      transport=httpx.MockTransport(lambda request: httpx.Response(
        307,
        request=request,
        headers={"location": "https://elsewhere.example/mcp"},
      )),
      timeout=kwargs.get("timeout"),
      follow_redirects=False,
    ),
  )
  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    response = loopback.get(
      f"/api/connectors/{row.id}/broker",
      headers={"Authorization": f"Bearer {capability}"},
    )
  assert response.status_code == 502
  assert "elsewhere" not in response.text
