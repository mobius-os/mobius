"""Connector registry core: MCP handshake, secret handling, per-turn provider config.

A connector (models.Connector) is a remote streamable-HTTP MCP server the
owner registered in Settings. This module owns everything the routes and
the two runners need:

- `handshake()` — add-time/refresh MCP probe (initialize → initialized →
  tools/list over JSON-RPC), returning the server's advertised name, its
  tool list, and a chars/4 token estimate of the full schema payload so
  Settings can show real cost before the owner enables anything.
- secret encrypt/decrypt — Fernet keyed off the instance secret (same
  derivation pattern as app-scoped secrets in routes/secrets.py, distinct
  salt). Plaintext keys never appear in API responses or logs.
- `claude_mcp_servers()` / `codex_config()` — the per-turn injection
  surfaces. Both read enabled connectors fresh each turn (registry edits
  apply on the next turn, no restart). Claude gets the SDK's native
  `mcp_servers` dict (deferred tool loading is the runtime's default, so
  idle connectors cost stubs, not schemas). Codex gets `--config`
  overrides plus env vars carrying the decrypted keys — the standard
  `bearer_token_env_var` / `env_http_headers` indirection so secrets stay
  out of argv.

Injection MUST never break a turn: the runner call sites wrap these in
try/except, and this module keeps per-connector failures local.
"""

import base64
import hashlib
import json
import logging
import re

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app import models
from app.config import get_settings

log = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"
_HANDSHAKE_TIMEOUT = 15.0
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


class ConnectorError(Exception):
  """Handshake or config failure with a partner-presentable message."""


# ── Secrets ──────────────────────────────────────────────────────────────


def _fernet() -> Fernet:
  material = f"mobius-connector-secret-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def encrypt_secret(value: str) -> str:
  return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str) -> str:
  try:
    return _fernet().decrypt(token.encode()).decode()
  except InvalidToken as exc:
    raise ConnectorError(
      "Stored key can no longer be decrypted; remove and re-add the connector."
    ) from exc


# ── Naming / sizing (pure helpers, unit-tested) ──────────────────────────


def slugify(name: str) -> str:
  slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
  return slug[:48] or "connector"


def estimate_tokens(tools: list) -> int:
  """chars/4 estimate of what the full tool schemas cost per message."""
  try:
    return max(0, len(json.dumps(tools)) // 4)
  except (TypeError, ValueError):
    return 0


def auth_headers(header_name: str | None, secret: str | None) -> dict[str, str]:
  """The one pragmatic auth rule: Authorization gets a Bearer prefix
  unless the pasted value already carries one; custom headers send the
  value verbatim."""
  if not header_name or not secret:
    return {}
  if header_name.lower() == "authorization" and not secret.startswith("Bearer "):
    return {header_name: f"Bearer {secret}"}
  return {header_name: secret}


def bare_secret(header_name: str | None, secret: str) -> str:
  """Codex's bearer_token_env_var adds its own `Bearer ` prefix, so the
  env var must carry the bare key even if the owner pasted a full value."""
  if header_name and header_name.lower() == "authorization":
    return secret.removeprefix("Bearer ")
  return secret


# ── MCP handshake ────────────────────────────────────────────────────────


def _parse_rpc_body(response: httpx.Response) -> dict:
  """A streamable-HTTP server answers a POST with JSON or a short SSE
  body; either way exactly one JSON-RPC response object is inside."""
  content_type = response.headers.get("content-type", "")
  if "text/event-stream" in content_type:
    for line in response.text.splitlines():
      if line.startswith("data:"):
        payload = line[len("data:"):].strip()
        if payload:
          return json.loads(payload)
    raise ConnectorError("Service sent an empty event stream.")
  try:
    return response.json()
  except ValueError as exc:
    raise ConnectorError("Service did not answer with valid JSON.") from exc


async def _rpc(
  client: httpx.AsyncClient,
  url: str,
  headers: dict[str, str],
  method: str,
  params: dict | None,
  rpc_id: int | None,
) -> dict | None:
  body: dict = {"jsonrpc": "2.0", "method": method}
  if params is not None:
    body["params"] = params
  if rpc_id is not None:
    body["id"] = rpc_id
  response = await client.post(url, json=body, headers=headers)
  if rpc_id is None:
    return None  # notification — servers answer 202/204/empty
  if response.status_code in (401, 403):
    raise ConnectorError(
      "The service rejected the key (unauthorized). Check the API key and header."
    )
  if response.status_code >= 400:
    raise ConnectorError(
      f"The service answered HTTP {response.status_code} to {method}."
    )
  parsed = _parse_rpc_body(response)
  if "error" in parsed:
    message = str(parsed["error"].get("message", "unknown error"))
    raise ConnectorError(f"The service reported an error: {message}")
  return parsed.get("result") or {}


async def handshake(
  url: str, header_name: str | None = None, secret: str | None = None,
) -> dict:
  """Probes an MCP server; returns {name, tools, est_tokens}.

  Raises ConnectorError with a partner-presentable message on any failure —
  the caller decides whether to save anything.
  """
  base_headers = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    **auth_headers(header_name, secret),
  }
  try:
    async with httpx.AsyncClient(
      timeout=_HANDSHAKE_TIMEOUT, follow_redirects=True,
    ) as client:
      init_body = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "mobius", "version": "1.0"},
      }
      response = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init_body},
        headers=base_headers,
      )
      if response.status_code in (401, 403):
        raise ConnectorError(
          "The service requires sign-in Möbius can't do yet (or the key is wrong). "
          "OAuth services arrive in the next phase; API-key services need the key here."
        )
      if response.status_code >= 400:
        raise ConnectorError(
          f"The service answered HTTP {response.status_code} — not a reachable "
          "MCP endpoint, or the URL is wrong."
        )
      init = _parse_rpc_body(response)
      if "error" in init:
        raise ConnectorError(
          f"The service refused the handshake: {init['error'].get('message', '')}"
        )
      session_headers = dict(base_headers)
      session_id = response.headers.get("mcp-session-id")
      if session_id:
        session_headers["Mcp-Session-Id"] = session_id
      server_info = (init.get("result") or {}).get("serverInfo") or {}

      await _rpc(client, url, session_headers, "notifications/initialized", {}, None)
      listed = await _rpc(client, url, session_headers, "tools/list", {}, 2)
      tools = (listed or {}).get("tools") or []
  except ConnectorError:
    raise
  except httpx.HTTPError as exc:
    raise ConnectorError(f"Could not reach the service: {exc}") from exc

  return {
    "name": str(server_info.get("title") or server_info.get("name") or "").strip(),
    "tools": tools,
    "est_tokens": estimate_tokens(tools),
  }


# ── Per-turn provider config ─────────────────────────────────────────────


def _enabled(db) -> list:
  return (
    db.query(models.Connector)
    .filter(models.Connector.enabled.is_(True))
    .order_by(models.Connector.id)
    .all()
  )


def claude_mcp_servers(db) -> dict:
  """The Claude SDK's native `mcp_servers` option: {slug: http config}."""
  servers: dict[str, dict] = {}
  for row in _enabled(db):
    try:
      config: dict = {"type": "http", "url": row.url}
      if row.auth_header and row.auth_value_encrypted:
        config["headers"] = auth_headers(
          row.auth_header, decrypt_secret(row.auth_value_encrypted)
        )
      servers[row.slug] = config
    except ConnectorError:
      log.warning("connector %s skipped (secret undecryptable)", row.slug)
  return servers


def codex_config(db) -> tuple[dict | None, dict[str, str]]:
  """Codex per-THREAD config + env vars for enabled connectors.

  In app-server mode (how Möbius runs codex) the conversation's config
  governs MCP servers; process-level ``--config`` overrides are ignored
  for them (verified empirically: identical overrides expose tools under
  ``codex exec`` but not through ``thread_start``, while this thread
  config does). So the dict returned here must be passed as the
  ``config=`` argument of ``thread_start``/``thread_resume``.

  Secrets still ride process env vars (never argv, never the thread
  config JSON): Authorization keys use codex's ``bearer_token_env_var``;
  custom headers use ``env_http_headers`` (header name -> env var name).
  The app-server process resolves those vars, so the env dict returned
  here must be merged into its launch env.

  ``startup_timeout_sec`` matters because Möbius launches a fresh
  app-server every turn: codex doesn't block on a remote handshake by
  default, so each turn would race the connect and the model's tool
  snapshot usually loses. The wait only blocks until the server is
  ready (<1s for a healthy connector), hard-capped so a dead one
  degrades that turn after 20s instead of breaking it.
  """
  servers: dict[str, dict] = {}
  env: dict[str, str] = {}
  for row in _enabled(db):
    try:
      server: dict = {"url": row.url, "startup_timeout_sec": 20}
      if row.auth_header and row.auth_value_encrypted:
        var = f"MOBIUS_CONNECTOR_{row.slug.upper()}"
        secret = decrypt_secret(row.auth_value_encrypted)
        env[var] = bare_secret(row.auth_header, secret)
        if row.auth_header.lower() == "authorization":
          server["bearer_token_env_var"] = var
        else:
          server["env_http_headers"] = {row.auth_header: var}
      servers[row.slug] = server
    except ConnectorError:
      log.warning("connector %s skipped (secret undecryptable)", row.slug)
  if not servers:
    return None, env
  return {"mcp_servers": servers}, env
