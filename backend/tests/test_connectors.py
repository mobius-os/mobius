"""Provider-neutral MCP connection registry and broker-boundary tests."""

import asyncio
import json
import os
import socket
import tempfile
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connectors as core
from app import models
from app.database import SessionLocal, checked_out_connections
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
  with pytest.raises(core.ConnectorError, match="at least 8"):
    core.validate_auth_secret("X-Api-Key", "tiny")
  with pytest.raises(core.ConnectorError, match="at least 8"):
    core.validate_auth_secret("Authorization", "Bearer a")
  with pytest.raises(core.ConnectorError, match="reserved"):
    core.validate_auth_header("MCP-Protocol-Version")
  with pytest.raises(core.ConnectorError, match="reserved"):
    core.validate_auth_header("Last-Event-ID")
  with pytest.raises(core.ConnectorError, match="reserved"):
    core.validate_auth_header("Transfer-Encoding")
  with pytest.raises(core.ConnectorError, match="reserved"):
    core.validate_auth_header("Mcp-Param-Cursor")


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
          "result": {
            "tools": [{"name": "second"}, {"description": "no name"}],
          },
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
  assert result["tools"] == ["first", "second"]
  assert calls[0].headers["authorization"] == "Bearer secret-key"
  assert calls[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_handshake_accepts_a_newer_protocol_version(monkeypatch):
  """A routine upstream version bump must not become an outage."""
  methods = []

  def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    methods.append(body["method"])
    if body["method"] == "initialize":
      assert body["params"]["protocolVersion"] == "2025-11-25"
      assert request.headers["mcp-protocol-version"] == "2025-11-25"
      return httpx.Response(
        200,
        request=request,
        headers={"content-type": "application/json"},
        json={
          "jsonrpc": "2.0",
          "id": 1,
          "result": {
            "protocolVersion": "2026-07-28",
            "serverInfo": {"name": "Future MCP"},
          },
        },
      )
    assert request.headers["mcp-protocol-version"] == "2026-07-28"
    if body["method"] == "notifications/initialized":
      return httpx.Response(202, request=request)
    return httpx.Response(
      200,
      request=request,
      headers={"content-type": "application/json"},
      json={
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": [{"name": "future_tool"}]},
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

  result = await core.handshake("https://mcp.example/mcp")
  assert result["name"] == "Future MCP"
  assert result["tools"] == ["future_tool"]
  assert result["est_tokens"] > 0
  assert methods == ["initialize", "notifications/initialized", "tools/list"]


@pytest.mark.asyncio
@pytest.mark.parametrize("negotiated", ("2024-11-05", "vNext"))
async def test_handshake_rejects_prefloor_or_malformed_versions(
  monkeypatch, negotiated,
):
  methods = []

  def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    methods.append(body["method"])
    assert body["method"] == "initialize"
    return httpx.Response(
      200,
      request=request,
      headers={"content-type": "application/json"},
      json={
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
          "protocolVersion": negotiated,
          "serverInfo": {"name": "Old MCP"},
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

  with pytest.raises(core.ConnectorError, match="unsupported MCP version"):
    await core.handshake("https://mcp.example/mcp")
  assert methods == ["initialize"]


@pytest.mark.asyncio
async def test_handshake_redacts_secret_echoes_and_rpc_errors(monkeypatch):
  error_mode = False

  def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
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
            "name": "echo-secret lookup",
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
    "https://mcp.example/mcp", "Authorization", "Bearer echo-secret",
  )
  assert result == {
    "name": "[redacted] service",
    "tools": ["[redacted] lookup"],
    "est_tokens": result["est_tokens"],
  }
  assert result["est_tokens"] > 0
  assert "inputSchema" not in json.dumps(result)
  assert "echo-secret" not in json.dumps(result)

  error_mode = True
  with pytest.raises(core.ConnectorError) as raised:
    await core.handshake(
      "https://mcp.example/mcp", "Authorization", "Bearer echo-secret",
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
  stable_a = "a" * 64
  stable_b = "b" * 64
  capability = core.mint_broker_capability(41, stable_a)
  core.verify_broker_capability(capability, 41, stable_a)
  with pytest.raises(core.ConnectorError, match="not valid for"):
    core.verify_broker_capability(capability, 42, stable_a)
  with pytest.raises(core.ConnectorError, match="not valid for"):
    core.verify_broker_capability(capability, 41, stable_b)

  expired = core._broker_fernet().encrypt_at_time(
    json.dumps({"connector_id": 41, "capability_id": stable_a}).encode(),
    current_time=0,
  ).decode()
  with pytest.raises(core.ConnectorError, match="not valid"):
    core.verify_broker_capability(expired, 41, stable_a)


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
    capability_id=(f"stable-{row_id}-{slug}-" + "x" * 64)[:64],
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


def _owner_headers(auth, generation):
  return {
    **auth,
    "X-Mobius-Connector-Generation": generation,
  }


def test_turn_plan_serves_both_providers_without_config_secrets():
  plan_source_rows = [
    _row(4, "docs", "https://docs.example/mcp"),
    _row(9, "search", "https://search.example/mcp", "Authorization", "sk-9"),
    _row(12, "data", "https://data.example/mcp", "X-Api-Key", "key-12"),
  ]
  plan = core.build_turn_plan(
    _FakeDb(plan_source_rows),
    include_owner_connectors=True,
  )
  assert plan is not None

  expected_keys = {
    "mobius_connector_4_docs",
    "mobius_connector_9_search",
    "mobius_connector_12_data",
  }
  assert set(plan.claude_servers) == expected_keys
  assert plan.codex_config is not None
  codex_servers = plan.codex_config["mcp_servers"]
  for key in expected_keys:
    connector_id = int(key.split("_")[2])
    claude = plan.claude_servers[key]
    codex = codex_servers[key]
    expected_url = f"http://127.0.0.1:9/api/connectors/{connector_id}/broker"
    assert claude["url"] == expected_url
    assert codex["url"] == expected_url
    assert "example" not in claude["url"]
    capability = claude["headers"]["Authorization"].removeprefix("Bearer ")
    row = next(row for row in plan_source_rows if row.id == connector_id)
    core.verify_broker_capability(capability, connector_id, row.capability_id)
    assert codex["startup_timeout_sec"] == 30
    env_var = codex["bearer_token_env_var"]
    assert plan.codex_env[env_var] == capability
  assert "sk-9" not in repr(plan)
  assert "sk-9" not in repr(plan.codex_config)
  assert "key-12" not in repr(plan.codex_config)


def test_turn_plan_requires_explicit_owner_connector_access():
  class _NoQueryDb:
    def query(self, *_args, **_kwargs):
      raise AssertionError("a denied child plan must not read owner connectors")

  assert core.build_turn_plan(
    _NoQueryDb(),
    include_owner_connectors=False,
  ) is None


def test_claude_config_is_retired_without_releasing_argv_visible_fd():
  plan = core.ConnectorTurnPlan(
    claude_servers={
      "search": {
        "type": "http",
        "url": "https://search.example/mcp",
        "headers": {"Authorization": "Bearer secret-in-file"},
      },
    },
  )
  with core.claude_mcp_config_handle(plan) as config:
    assert config is not None
    assert config.path.startswith(f"/proc/{os.getpid()}/fd/")
    assert "secret-in-file" not in config.path
    with open(config.path, encoding="utf-8") as config_file:
      payload = json.load(config_file)
    assert payload["mcpServers"]["search"]["headers"]["Authorization"] == (
      "Bearer secret-in-file"
    )
    held_fd = int(config.path.rsplit("/", 1)[1])
    config.retire()
    with open(config.path, "rb") as retired_file:
      assert retired_file.read() == b""
    with tempfile.TemporaryFile() as unrelated:
      assert unrelated.fileno() != held_fd
    config.retire()  # Retirement is idempotent across nested cleanup paths.
  assert not os.path.exists(config.path)


def test_connector_routes_keep_keys_out_of_responses(
  client, auth, db, monkeypatch,
):
  probe = {
    "name": "Example MCP",
    "tools": ["lookup"],
    "est_tokens": 5150,
  }
  probe_pool_counts = []

  async def probe_without_db_lease(*_args, **_kwargs):
    probe_pool_counts.append(checked_out_connections())
    return probe

  handshake = AsyncMock(side_effect=probe_without_db_lease)
  monkeypatch.setattr("app.routes.connectors.core.handshake", handshake)

  overlong_secret = "never-echo-overlong-" + ("z" * 4096)
  overlong = client.post("/api/connectors", headers=auth, json={
    "url": "https://mcp.example/mcp",
    "auth_value": overlong_secret,
  })
  assert overlong.status_code == 422
  assert overlong_secret not in overlong.text
  assert "never-echo-overlong" not in overlong.text

  wrong_type = client.post("/api/connectors", headers=auth, json={
    "url": "https://mcp.example/mcp",
    "auth_value": {"secret": "never-echo-object"},
  })
  assert wrong_type.status_code == 400
  assert "never-echo-object" not in wrong_type.text
  assert handshake.await_count == 0

  short = client.post("/api/connectors", headers=auth, json={
    "url": "https://mcp.example/mcp",
    "auth_header": "Authorization",
    "auth_value": "Bearer a",
  })
  assert short.status_code == 422
  assert "at least 8" in short.text
  assert handshake.await_count == 0

  add_baseline = checked_out_connections()
  created = client.post("/api/connectors", headers=auth, json={
    "url": "https://mcp.example/mcp",
    "auth_value": "top-secret",
  })
  assert created.status_code == 201, created.text
  body = created.json()
  assert body["name"] == "Example MCP"
  assert body["has_auth"] is True
  assert set(body) == {
    "id", "generation", "name", "url", "enabled", "has_auth",
    "tools", "tool_count", "est_tokens", "status", "status_detail",
  }
  assert body["tools"] == ["lookup"]
  assert body["est_tokens"] == 5150
  assert "top-secret" not in created.text
  assert probe_pool_counts == [add_baseline]

  stored = db.query(models.Connector).one()
  original_identity = stored.capability_id
  assert body["id"] == stored.id
  assert body["generation"] == original_identity
  assert stored.auth_value_encrypted != "top-secret"
  assert core.decrypt_secret(stored.auth_value_encrypted) == "top-secret"

  blank_name = client.patch(
    f"/api/connectors/{stored.id}",
    headers=_owner_headers(auth, original_identity),
    json={"name": "   "},
  )
  assert blank_name.status_code == 422
  db.refresh(stored)
  assert stored.name == "Example MCP"

  listed = client.get("/api/connectors", headers=auth)
  assert listed.status_code == 200
  assert "top-secret" not in listed.text

  toggled = client.patch(
    f"/api/connectors/{stored.id}",
    headers=_owner_headers(auth, original_identity),
    json={"enabled": False},
  )
  assert toggled.status_code == 200
  assert toggled.json()["enabled"] is False
  db.expire_all()
  disabled_identity = db.query(models.Connector).one().capability_id
  assert disabled_identity != original_identity
  assert toggled.json()["generation"] == disabled_identity

  # The direct assertion session above owns its own checkout; release it so
  # the refresh probe can prove the request-owned lease is gone independently.
  db.close()
  refresh_baseline = checked_out_connections()
  refreshed = client.post(
    f"/api/connectors/{stored.id}/refresh",
    headers=_owner_headers(auth, disabled_identity),
  )
  assert refreshed.status_code == 200
  assert handshake.await_count == 2
  assert probe_pool_counts[-1] == refresh_baseline


def test_disable_revokes_capability_across_reenable(client, auth, db):
  row = _row(72, "revoked", "https://remote.example/mcp")
  db.add(row)
  db.commit()
  old_identity = row.capability_id
  old_capability = core.mint_broker_capability(row.id, old_identity)

  disabled = client.patch(
    f"/api/connectors/{row.id}",
    headers=_owner_headers(auth, old_identity),
    json={"enabled": False},
  )
  assert disabled.status_code == 200
  db.expire_all()
  new_identity = row.capability_id
  assert new_identity != old_identity
  enabled = client.patch(
    f"/api/connectors/{row.id}",
    headers=_owner_headers(auth, new_identity),
    json={"enabled": True},
  )
  assert enabled.status_code == 200

  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    denied = loopback.get(
      f"/api/connectors/{row.id}/broker",
      headers={"Authorization": f"Bearer {old_capability}"},
    )
  assert denied.status_code == 401


def test_owner_mutations_require_current_connection_generation(
  client, auth, db,
):
  original = _row(76, "original_owner", "https://original.example/mcp")
  db.add(original)
  db.commit()
  stale_generation = original.capability_id

  missing = client.patch(
    f"/api/connectors/{original.id}",
    headers=auth,
    json={"enabled": False},
  )
  assert missing.status_code == 428

  db.delete(original)
  db.commit()
  replacement = _row(
    76,
    "replacement_owner",
    "https://replacement.example/mcp",
  )
  db.add(replacement)
  db.commit()

  for method, payload in (
    ("PATCH", {"enabled": False}),
    ("DELETE", None),
  ):
    response = client.request(
      method,
      f"/api/connectors/{replacement.id}",
      headers=_owner_headers(auth, stale_generation),
      json=payload,
    )
    assert response.status_code == 404

  db.expire_all()
  untouched = db.query(models.Connector).filter_by(id=76).one()
  assert untouched.slug == "replacement_owner"
  assert untouched.enabled is True


def test_unhealthy_connection_cannot_be_enabled_planned_or_brokered(
  client, auth, db,
):
  row = _row(74, "unhealthy", "https://remote.example/mcp")
  row.enabled = False
  row.status = "error"
  row.status_detail = "Re-check required."
  db.add(row)
  db.commit()

  rejected = client.patch(
    f"/api/connectors/{row.id}",
    headers=_owner_headers(auth, row.capability_id),
    json={"enabled": True},
  )
  assert rejected.status_code == 409

  row.enabled = True  # Model an older row or a failed refresh while enabled.
  db.commit()
  assert core.build_turn_plan(
    db,
    include_owner_connectors=True,
  ) is None
  capability = core.mint_broker_capability(row.id, row.capability_id)
  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    denied = loopback.get(
      f"/api/connectors/{row.id}/broker",
      headers={"Authorization": f"Bearer {capability}"},
    )
  assert denied.status_code == 404


@pytest.mark.asyncio
async def test_dns_blip_is_transient_but_dead_name_is_definitive(monkeypatch):
  """DNS happens in the SSRF validator, not httpx; classify it there too."""

  def resolver_blip(host, *_args, **_kwargs):
    raise socket.gaierror(
      socket.EAI_AGAIN, "Temporary failure in name resolution",
    )

  monkeypatch.setattr("app.net_utils.socket.getaddrinfo", resolver_blip)
  with pytest.raises(core.ConnectorError) as blip:
    await core.handshake("https://mcp.example/mcp")
  assert blip.value.transient is True

  def dead_name(host, *_args, **_kwargs):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

  monkeypatch.setattr("app.net_utils.socket.getaddrinfo", dead_name)
  with pytest.raises(core.ConnectorError) as gone:
    await core.handshake("https://mcp.example/mcp")
  assert gone.value.transient is False


def _app_token(client, auth, *, granted):
  from test_app_fixtures import create_local_app

  app_id = create_local_app(
    client, auth, name="connections-test", description="t",
  )["id"]
  session = SessionLocal()
  try:
    app = session.query(models.App).filter(models.App.id == app_id).first()
    app.connections_manage = granted
    session.commit()
  finally:
    session.close()
  minted = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  )
  assert minted.status_code == 200, minted.text
  return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_app_token_requires_connections_manage(client, auth, db):
  row = _row(79, "app_managed", "https://remote.example/mcp")
  db.add(row)
  db.commit()
  generation = row.capability_id

  denied = _app_token(client, auth, granted=False)
  assert client.get("/api/connectors", headers=denied).status_code == 403
  assert client.patch(
    f"/api/connectors/{row.id}",
    headers={**denied, "X-Mobius-Connector-Generation": generation},
    json={"enabled": False},
  ).status_code == 403

  managed = _app_token(client, auth, granted=True)
  listed = client.get("/api/connectors", headers=managed)
  assert listed.status_code == 200
  assert [c["id"] for c in listed.json()["connectors"]] == [row.id]
  toggled = client.patch(
    f"/api/connectors/{row.id}",
    headers={**managed, "X-Mobius-Connector-Generation": generation},
    json={"enabled": False},
  )
  assert toggled.status_code == 200
  assert toggled.json()["enabled"] is False


def test_estimate_tokens_scales_with_schema_size():
  small = [{"name": "a", "inputSchema": {}}]
  large = [
    {
      "name": f"tool_{index}",
      "inputSchema": {
        "properties": {key: {"type": "string"} for key in ("one", "two")},
      },
    }
    for index in range(40)
  ]
  assert core.estimate_tokens([]) == 0
  assert 0 < core.estimate_tokens(small) < core.estimate_tokens(large)
  assert core.estimate_tokens([object()]) == 0


def test_refresh_transient_failure_keeps_last_known_health(
  client, auth, db, monkeypatch,
):
  row = _row(78, "flaky", "https://remote.example/mcp")
  row.tools_json = ["lookup"]
  row.est_tokens = 1200
  db.add(row)
  db.commit()
  generation = row.capability_id

  async def transient_probe(*_args, **_kwargs):
    raise core.ConnectorError(
      "Could not reach the MCP service.", transient=True,
    )

  monkeypatch.setattr(core, "handshake", transient_probe)
  refreshed = client.post(
    f"/api/connectors/{row.id}/refresh",
    headers=_owner_headers(auth, generation),
  )
  assert refreshed.status_code == 200
  body = refreshed.json()
  # One transport blip must not latch the connection out of agent turns.
  assert body["status"] == "ok"
  assert "Could not reach" in body["status_detail"]
  assert body["est_tokens"] == 1200
  db.expire_all()
  assert core.build_turn_plan(db, include_owner_connectors=True) is not None

  async def definitive_probe(*_args, **_kwargs):
    raise core.ConnectorError(
      "The service rejected the key or requires an OAuth sign-in flow.",
    )

  monkeypatch.setattr(core, "handshake", definitive_probe)
  rejected = client.post(
    f"/api/connectors/{row.id}/refresh",
    headers=_owner_headers(auth, generation),
  )
  assert rejected.status_code == 200
  assert rejected.json()["status"] == "error"
  db.expire_all()
  assert core.build_turn_plan(db, include_owner_connectors=True) is None

  # A blip while already latched keeps the definitive diagnosis so the owner
  # still sees the real reason, not a generic transport message.
  monkeypatch.setattr(core, "handshake", transient_probe)
  still_error = client.post(
    f"/api/connectors/{row.id}/refresh",
    headers=_owner_headers(auth, generation),
  )
  assert still_error.status_code == 200
  assert still_error.json()["status"] == "error"
  assert "rejected the key" in still_error.json()["status_detail"]


def test_refresh_cannot_overwrite_reused_connector_id(
  client, auth, db, monkeypatch,
):
  original = _row(73, "original", "https://original.example/mcp")
  original.tools_json = [{"name": "original"}]
  db.add(original)
  db.commit()
  original_identity = original.capability_id
  db.close()

  async def replace_during_probe(*_args, **_kwargs):
    with SessionLocal() as concurrent:
      stored = concurrent.query(models.Connector).filter_by(id=73).one()
      concurrent.delete(stored)
      concurrent.commit()
      replacement = _row(73, "replacement", "https://replacement.example/mcp")
      replacement.tools_json = [{"name": "replacement"}]
      concurrent.add(replacement)
      concurrent.commit()
    return {
      "name": "Stale probe",
      "tools": ["stale"],
      "est_tokens": 3,
    }

  monkeypatch.setattr(core, "handshake", replace_during_probe)
  refreshed = client.post(
    f"/api/connectors/{original.id}/refresh",
    headers=_owner_headers(auth, original_identity),
  )
  assert refreshed.status_code == 404

  with SessionLocal() as check:
    replacement = check.query(models.Connector).filter_by(id=73).one()
    assert replacement.slug == "replacement"
    assert replacement.tools_json == [{"name": "replacement"}]


@pytest.mark.parametrize(("auth_header", "secret"), (
  ("Authorization", "Bearer a"),
  ("Transfer-Encoding", "long-enough-key"),
))
def test_broker_rejects_legacy_unsafe_key_configuration(
  client,
  db,
  auth_header,
  secret,
):
  row = _row(
    79,
    "unsafe_legacy_key",
    "https://unsafe-key.example/mcp",
    auth_header,
    secret,
  )
  db.add(row)
  db.commit()
  capability = core.mint_broker_capability(row.id, row.capability_id)

  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    rejected = loopback.get(
      f"/api/connectors/{row.id}/broker",
      headers={"Authorization": f"Bearer {capability}"},
    )

  assert rejected.status_code == 502


def test_broker_requires_loopback_and_connector_scoped_capability(
  client, db,
):
  row = _row(
    21, "private", "https://private.example/mcp",
    "Authorization", "upstream-secret",
  )
  db.add(row)
  db.commit()
  capability = core.mint_broker_capability(row.id, row.capability_id)

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
        "Authorization": f"Bearer {core.mint_broker_capability(row.id + 1, row.capability_id)}",
      },
    )
    assert wrong_scope.status_code == 401
    row.enabled = False
    db.commit()
    disabled = loopback.get(
      f"/api/connectors/{row.id}/broker",
      headers={"Authorization": f"Bearer {capability}"},
    )
    assert disabled.status_code == 404


def test_deleted_connector_capability_cannot_authorize_reused_row_id(client, db):
  old = _row(31, "old", "https://old.example/mcp", secret="old-secret")
  db.add(old)
  db.commit()
  old_identity = old.capability_id
  old_capability = core.mint_broker_capability(old.id, old_identity)
  db.delete(old)
  db.commit()

  replacement = _row(
    31, "replacement", "https://new.example/mcp", secret="new-secret",
  )
  db.add(replacement)
  db.commit()
  assert replacement.capability_id != old_identity

  with TestClient(client.app, client=("127.0.0.1", 43100)) as loopback:
    denied = loopback.get(
      f"/api/connectors/{replacement.id}/broker",
      headers={"Authorization": f"Bearer {old_capability}"},
    )
  assert denied.status_code == 401


def test_broker_drops_allowed_response_headers_that_reflect_secrets():
  snapshot = connector_routes._BrokerSnapshot(
    url="https://remote.example/mcp",
    auth_header="Authorization",
    secret="Bearer upstream-secret",
  )
  upstream = httpx.Response(200, headers={
    "content-type": "application/json",
    "mcp-protocol-version": "upstream-secret",
    "mcp-session-id": "Bearer upstream-secret",
    "cache-control": "private, upstream-secret",
    "x-untrusted": "ignored",
  })
  headers = connector_routes._broker_response_headers(upstream, snapshot)
  assert headers == {"content-type": "application/json"}


def test_broker_revalidates_pins_and_streams_each_mcp_method(
  client, db, monkeypatch,
):
  row = _row(
    22, "remote", "https://remote.example/mcp",
    "X-Api-Key", "upstream-secret",
  )
  db.add(row)
  db.commit()
  capability = core.mint_broker_capability(row.id, row.capability_id)
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
  capability = core.mint_broker_capability(row.id, row.capability_id)
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
