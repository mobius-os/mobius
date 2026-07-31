"""Tests for the connector registry core (app.connectors).

Covers the pure/deterministic surfaces the runners and routes lean on:

- the one auth rule (Authorization gets a Bearer prefix, custom headers
  are verbatim, and codex env vars carry the BARE key because codex adds
  its own prefix) — asymmetry here would break exactly one provider,
  which is the worst failure mode to debug;
- codex --config override formatting (secrets must ride env vars, never
  argv, and the slug must be the same key claude uses);
- the SSE-vs-JSON response body tolerance of the handshake parser;
- secret encrypt/decrypt roundtrip and the friendly failure on garbage.
"""

import json

import httpx
import pytest

from app import connectors as core
from app import models


def test_slugify_normalizes_and_bounds():
  assert core.slugify("Context7 Docs!") == "context7_docs"
  assert core.slugify("  --  ") == "connector"
  assert len(core.slugify("x" * 200)) <= 48


def test_estimate_tokens_chars_over_four():
  tools = [{"name": "t", "description": "d" * 396}]
  assert core.estimate_tokens(tools) == len(json.dumps(tools)) // 4
  assert core.estimate_tokens([]) == len("[]") // 4


def test_auth_headers_bearer_rule():
  assert core.auth_headers("Authorization", "sk-123") == {
    "Authorization": "Bearer sk-123"
  }
  # Already-prefixed values are not double-prefixed.
  assert core.auth_headers("Authorization", "Bearer sk-123") == {
    "Authorization": "Bearer sk-123"
  }
  # Custom headers send the pasted value verbatim.
  assert core.auth_headers("X-Api-Key", "sk-123") == {"X-Api-Key": "sk-123"}
  assert core.auth_headers(None, "sk-123") == {}
  assert core.auth_headers("Authorization", None) == {}


def test_bare_secret_strips_bearer_only_for_authorization():
  # codex's bearer_token_env_var adds its own "Bearer " prefix.
  assert core.bare_secret("Authorization", "Bearer sk-1") == "sk-1"
  assert core.bare_secret("Authorization", "sk-1") == "sk-1"
  assert core.bare_secret("X-Api-Key", "Bearer sk-1") == "Bearer sk-1"


def test_parse_rpc_body_json_and_sse():
  json_response = httpx.Response(
    200, headers={"content-type": "application/json"},
    text='{"jsonrpc":"2.0","id":1,"result":{}}',
  )
  assert core._parse_rpc_body(json_response)["id"] == 1

  sse_response = httpx.Response(
    200, headers={"content-type": "text/event-stream"},
    text='event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{}}\n\n',
  )
  assert core._parse_rpc_body(sse_response)["id"] == 2

  empty_sse = httpx.Response(
    200, headers={"content-type": "text/event-stream"}, text="\n\n",
  )
  with pytest.raises(core.ConnectorError):
    core._parse_rpc_body(empty_sse)


def test_secret_roundtrip_and_garbage(monkeypatch):
  token = core.encrypt_secret("sk-value")
  assert token != "sk-value"
  assert core.decrypt_secret(token) == "sk-value"
  with pytest.raises(core.ConnectorError):
    core.decrypt_secret("not-a-fernet-token")


class _FakeQuery:
  def __init__(self, rows):
    self._rows = rows

  def filter(self, *args, **kwargs):
    return self

  def order_by(self, *args, **kwargs):
    return self

  def all(self):
    return self._rows


class _FakeDb:
  def __init__(self, rows):
    self._rows = rows

  def query(self, model):
    return _FakeQuery(self._rows)


def _row(slug, url, auth_header=None, secret=None):
  row = models.Connector(
    slug=slug, name=slug, url=url, enabled=True,
    auth_header=auth_header,
    auth_value_encrypted=core.encrypt_secret(secret) if secret else None,
    tools_json=[], est_tokens=0, status="ok",
  )
  return row


def test_claude_mcp_servers_shapes_http_config():
  db = _FakeDb([
    _row("ctx", "https://mcp.example/mcp"),
    _row("exa", "https://exa.example/mcp", "Authorization", "sk-9"),
  ])
  servers = core.claude_mcp_servers(db)
  assert servers["ctx"] == {"type": "http", "url": "https://mcp.example/mcp"}
  assert servers["exa"]["headers"] == {"Authorization": "Bearer sk-9"}


def test_codex_config_thread_shape_and_env_indirection():
  db = _FakeDb([
    _row("exa", "https://exa.example/mcp", "Authorization", "sk-9"),
    _row("fire", "https://fire.example/mcp", "X-Api-Key", "fk-1"),
  ])
  thread_config, env = core.codex_config(db)
  servers = thread_config["mcp_servers"]
  assert servers["exa"]["url"] == "https://exa.example/mcp"
  # Fresh app-server per turn: without a startup wait, the model's tool
  # snapshot races the remote handshake and usually loses.
  assert servers["exa"]["startup_timeout_sec"] == 20
  assert servers["exa"]["bearer_token_env_var"] == "MOBIUS_CONNECTOR_EXA"
  assert env["MOBIUS_CONNECTOR_EXA"] == "sk-9"
  assert servers["fire"]["env_http_headers"] == {
    "X-Api-Key": "MOBIUS_CONNECTOR_FIRE"
  }
  assert env["MOBIUS_CONNECTOR_FIRE"] == "fk-1"
  # Secrets ride env only — never the thread-config JSON (which is
  # logged/persisted by the app-server protocol layer).
  assert "sk-9" not in repr(thread_config)
  assert "fk-1" not in repr(thread_config)


def test_codex_config_empty_registry_is_none():
  thread_config, env = core.codex_config(_FakeDb([]))
  assert thread_config is None
  assert env == {}
