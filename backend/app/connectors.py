"""Owner-managed remote MCP connections shared by both agent providers.

This module owns the provider-neutral connection boundary:

* a bounded Streamable HTTP handshake used by Settings before saving a row;
* write-only secret encryption compatible with the original connector preview;
* one detached, plain-data turn plan built while the request DB session is live;
* provider-specific projections that never query the database during a turn.

Provider-native plugins/apps remain provider-owned.  These rows are the smaller
cross-provider primitive for an owner-supplied remote MCP endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app import models
from app.config import get_settings
from app.net_utils import validate_url_safe

log = logging.getLogger(__name__)

# Probe the current stateless transport first, then fall back to the stateful
# 2025 transport still used by many deployed servers and provider SDKs.
MCP_PROTOCOL_VERSION = "2026-07-28"
_LEGACY_PROTOCOL_VERSION = "2025-11-25"
_SUPPORTED_LEGACY_PROTOCOL_VERSIONS = {
  _LEGACY_PROTOCOL_VERSION,
  "2025-06-18",
  "2025-03-26",
}
_HANDSHAKE_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_HANDSHAKE_DEADLINE_SECONDS = 22.0
_MAX_RPC_BYTES = 2 * 1024 * 1024
_MAX_TOOLS = 128
_MAX_TOOL_PAGES = 10
# A provider turn can legitimately remain alive for hours, but a copied child
# environment must not retain broker access for days. Cap one turn credential
# at 24 hours; disabling or deleting the connector revokes it immediately
# because every broker request still re-reads the enabled row.
_BROKER_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_HEADER_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RESERVED_AUTH_HEADERS = {
  "accept",
  "content-length",
  "content-type",
  "host",
  "mcp-protocol-version",
  "mcp-session-id",
  "origin",
}


class ConnectorError(Exception):
  """A connection failure safe to present in the owner-facing Settings UI."""


@dataclass(frozen=True)
class ConnectorTurnPlan:
  """Detached provider configuration built before the turn releases its DB.

  The values contain turn-lifetime (at most 24-hour) broker capabilities, so their repr is
  deliberately empty of fields and callers must never log or persist it.
  """

  claude_servers: dict[str, dict] = field(default_factory=dict, repr=False)
  codex_config: dict | None = field(default=None, repr=False)
  codex_env: dict[str, str] = field(default_factory=dict, repr=False)

  @property
  def empty(self) -> bool:
    return not self.claude_servers and not self.codex_config


EMPTY_CONNECTOR_TURN_PLAN = ConnectorTurnPlan()


# ── Secrets ──────────────────────────────────────────────────────────────


def _fernet() -> Fernet:
  # Keep the original salt: an owner who briefly ran PR #431 may still have
  # dormant connector rows after the source removal, and those keys must remain
  # decryptable when the feature returns.
  material = f"mobius-connector-secret-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def encrypt_secret(value: str) -> str:
  return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str) -> str:
  try:
    return _fernet().decrypt(token.encode()).decode()
  except (InvalidToken, ValueError) as exc:
    raise ConnectorError(
      "The stored key can no longer be decrypted. Remove and re-add this connection."
    ) from exc


def _broker_fernet() -> Fernet:
  material = f"mobius-connector-broker-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def mint_broker_capability(connector_id: int, capability_id: str) -> str:
  """Mint one short-lived, connector-scoped credential for a provider turn."""
  if not isinstance(capability_id, str) or len(capability_id) < 32:
    raise ConnectorError("The MCP connection has no broker identity.")
  payload = json.dumps(
    {
      "connector_id": connector_id,
      "capability_id": capability_id,
    },
    separators=(",", ":"),
  ).encode()
  return _broker_fernet().encrypt(payload).decode()


def verify_broker_capability(
  token: str,
  connector_id: int,
  capability_id: str,
) -> None:
  """Fail closed unless ``token`` is live and scoped to this connection."""
  try:
    payload = json.loads(
      _broker_fernet().decrypt(
        token.encode(), ttl=_BROKER_CAPABILITY_TTL_SECONDS,
      )
    )
  except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
    raise ConnectorError("The MCP broker capability is not valid.") from exc
  if (
    not isinstance(payload, dict)
    or payload.get("connector_id") != connector_id
    or payload.get("capability_id") != capability_id
  ):
    raise ConnectorError("The MCP broker capability is not valid for this connection.")


# ── Pure naming/auth/sizing helpers ─────────────────────────────────────


def slugify(name: str) -> str:
  slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
  return slug[:48] or "connector"


def estimate_tokens(tools: list) -> int:
  """Conservative chars/4 estimate for the cached full tool definitions."""
  try:
    return max(0, len(json.dumps(tools, separators=(",", ":"))) // 4)
  except (TypeError, ValueError):
    return 0


def validate_auth_header(name: str | None) -> str | None:
  value = (name or "").strip()
  if not value:
    return None
  if not _HEADER_RE.fullmatch(value):
    raise ConnectorError("The API-key header name is not valid.")
  if value.lower() in _RESERVED_AUTH_HEADERS:
    raise ConnectorError(f"{value} is reserved by the MCP transport.")
  return value


def auth_headers(header_name: str | None, secret: str | None) -> dict[str, str]:
  """Build one static auth header without changing a pasted Bearer value."""
  if not header_name or not secret:
    return {}
  if header_name.lower() == "authorization" and not re.match(
    r"(?i)^bearer\s+", secret,
  ):
    return {header_name: f"Bearer {secret}"}
  return {header_name: secret}


def bare_secret(header_name: str | None, secret: str) -> str:
  """Codex adds ``Bearer`` for bearer_token_env_var; keep only the token."""
  if header_name and header_name.lower() == "authorization":
    return re.sub(r"(?i)^bearer\s+", "", secret, count=1)
  return secret


# ── MCP handshake ────────────────────────────────────────────────────────


def _safe_endpoint(url: str) -> tuple[str, str, str]:
  try:
    parsed = urlparse(url)
    # Force validation of these lazy urlparse properties here so malformed
    # IPv6 authorities and non-numeric/out-of-range ports become domain errors.
    _ = parsed.hostname, parsed.port
    if parsed.scheme != "https":
      raise ConnectorError("Custom MCP endpoints must use public HTTPS.")
    if parsed.fragment:
      raise ConnectorError("The MCP endpoint must not contain a URL fragment.")
    return validate_url_safe(url)
  except ConnectorError:
    raise
  except ValueError as exc:
    raise ConnectorError("The MCP endpoint URL or port is not valid.") from exc
  except HTTPException as exc:
    raise ConnectorError(str(exc.detail)) from exc


def _matching_payload(payload: object, rpc_id: int) -> dict | None:
  candidates = payload if isinstance(payload, list) else [payload]
  for item in candidates:
    if isinstance(item, dict) and item.get("id") == rpc_id:
      return item
  return None


def _decode_rpc_json(data: str, rpc_id: int) -> dict | None:
  try:
    return _matching_payload(json.loads(data), rpc_id)
  except (ValueError, json.JSONDecodeError, RecursionError) as exc:
    raise ConnectorError("The service did not answer with valid MCP JSON.") from exc


async def _matching_rpc(response: httpx.Response, rpc_id: int) -> dict:
  """Incrementally read one bounded response, stopping at the matching id."""
  content_type = response.headers.get("content-type", "").lower()
  total = 0
  if "text/event-stream" not in content_type:
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
      total += len(chunk)
      if total > _MAX_RPC_BYTES:
        raise ConnectorError("The service returned an MCP response that is too large.")
      chunks.append(chunk)
    try:
      text = b"".join(chunks).decode("utf-8")
    except UnicodeError as exc:
      raise ConnectorError("The service did not answer with valid MCP JSON.") from exc
    payload = _decode_rpc_json(text, rpc_id)
    if payload is not None:
      return payload
    raise ConnectorError("The service did not answer the MCP request.")

  decoder = codecs.getincrementaldecoder("utf-8")()
  buffered = ""
  data_lines: list[str] = []

  def finish_event() -> dict | None:
    if not data_lines:
      return None
    data = "\n".join(data_lines)
    data_lines.clear()
    return _decode_rpc_json(data, rpc_id)

  try:
    async for chunk in response.aiter_bytes():
      total += len(chunk)
      if total > _MAX_RPC_BYTES:
        raise ConnectorError("The service returned an MCP response that is too large.")
      buffered += decoder.decode(chunk)
      while "\n" in buffered:
        line, buffered = buffered.split("\n", 1)
        line = line.removesuffix("\r")
        if not line:
          payload = finish_event()
          if payload is not None:
            return payload
        elif line.startswith("data:"):
          value = line[5:]
          data_lines.append(value[1:] if value.startswith(" ") else value)
    buffered += decoder.decode(b"", final=True)
  except UnicodeError as exc:
    raise ConnectorError("The service did not answer with valid MCP JSON.") from exc

  if buffered:
    line = buffered.removesuffix("\r")
    if line.startswith("data:"):
      value = line[5:]
      data_lines.append(value[1:] if value.startswith(" ") else value)
  payload = finish_event()
  if payload is not None:
    return payload
  raise ConnectorError("The service did not answer the MCP request.")


def _redact_metadata(value: object, secrets: tuple[str, ...], depth: int = 0) -> object:
  """Remove submitted credentials recursively before remote metadata persists."""
  if depth > 64:
    return "[metadata omitted]"
  if isinstance(value, str):
    for secret in secrets:
      if secret:
        value = value.replace(secret, "[redacted]")
    return value
  if isinstance(value, list):
    return [_redact_metadata(item, secrets, depth + 1) for item in value]
  if isinstance(value, dict):
    return {
      _redact_metadata(key, secrets, depth + 1) if isinstance(key, str) else key:
      _redact_metadata(item, secrets, depth + 1)
      for key, item in value.items()
    }
  return value


async def _post_rpc(
  client: httpx.AsyncClient,
  endpoint: tuple[str, str, str],
  headers: dict[str, str],
  method: str,
  params: dict | None,
  rpc_id: int | None,
) -> dict | None:
  pinned_url, host_header, sni_host = endpoint
  body: dict = {"jsonrpc": "2.0", "method": method}
  if params is not None:
    body["params"] = params
  if rpc_id is not None:
    body["id"] = rpc_id
  request = client.build_request("POST", pinned_url, json=body, headers=headers)
  request.headers["host"] = host_header
  request.extensions["sni_hostname"] = sni_host
  response = await client.send(request, stream=True)
  try:
    if response.status_code in (301, 302, 303, 307, 308):
      raise ConnectorError(
        "The endpoint redirected. Add the final HTTPS MCP address instead."
      )
    if response.status_code in (401, 403):
      raise ConnectorError(
        "The service rejected the key or requires an OAuth sign-in flow."
      )
    if response.status_code >= 400:
      raise ConnectorError(
        f"The service answered HTTP {response.status_code} to {method}."
      )
    if rpc_id is None:
      return None
    payload = await _matching_rpc(response, rpc_id)
    if "error" in payload:
      raise ConnectorError(f"The service reported an MCP error during {method}.")
    result = payload.get("result")
    return result if isinstance(result, dict) else {}
  finally:
    await response.aclose()


async def _close_session(
  client: httpx.AsyncClient,
  endpoint: tuple[str, str, str],
  headers: dict[str, str],
) -> None:
  pinned_url, host_header, sni_host = endpoint
  request = client.build_request("DELETE", pinned_url, headers=headers)
  request.headers["host"] = host_header
  request.extensions["sni_hostname"] = sni_host
  try:
    response = await client.send(request, stream=True)
    await response.aclose()
  except httpx.HTTPError:
    # Session teardown is advisory and must not turn a successful catalog read
    # into an add/refresh failure.
    pass


async def _initialize_rpc(
  client: httpx.AsyncClient,
  endpoint: tuple[str, str, str],
  headers: dict[str, str],
) -> tuple[dict, str | None]:
  """Initialize while retaining the transport-level session header."""
  pinned_url, host_header, sni_host = endpoint
  request = client.build_request(
    "POST",
    pinned_url,
    json={
      "jsonrpc": "2.0",
      "id": 1,
      "method": "initialize",
      "params": {
        "protocolVersion": _LEGACY_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "mobius", "version": "1.0"},
      },
    },
    headers=headers,
  )
  request.headers["host"] = host_header
  request.extensions["sni_hostname"] = sni_host
  response = await client.send(request, stream=True)
  try:
    if response.status_code in (301, 302, 303, 307, 308):
      raise ConnectorError(
        "The endpoint redirected. Add the final HTTPS MCP address instead."
      )
    if response.status_code in (401, 403):
      raise ConnectorError(
        "The service rejected the key or requires an OAuth sign-in flow."
      )
    if response.status_code >= 400:
      raise ConnectorError(
        f"The service answered HTTP {response.status_code}; check the MCP address."
      )
    payload = await _matching_rpc(response, 1)
    if "error" in payload:
      raise ConnectorError("The service refused the MCP handshake.")
    result = payload.get("result")
    if not isinstance(result, dict):
      raise ConnectorError("The service returned an invalid initialize result.")
    return result, response.headers.get("mcp-session-id")
  finally:
    await response.aclose()


def _modern_request_params(params: dict | None = None) -> dict:
  return {
    **(params or {}),
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
      "io.modelcontextprotocol/clientInfo": {
        "name": "mobius",
        "version": "1.0",
      },
      "io.modelcontextprotocol/clientCapabilities": {},
    },
  }


async def _modern_rpc(
  client: httpx.AsyncClient,
  endpoint: tuple[str, str, str],
  auth: dict[str, str],
  method: str,
  params: dict | None,
  rpc_id: int,
  *,
  allow_unsupported: bool = False,
) -> dict | None:
  """Send one stateless 2026 request; return ``None`` for a legacy server."""
  pinned_url, host_header, sni_host = endpoint
  headers = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    "Mcp-Method": method,
    **auth,
  }
  request = client.build_request(
    "POST",
    pinned_url,
    json={
      "jsonrpc": "2.0",
      "id": rpc_id,
      "method": method,
      "params": _modern_request_params(params),
    },
    headers=headers,
  )
  request.headers["host"] = host_header
  request.extensions["sni_hostname"] = sni_host
  response = await client.send(request, stream=True)
  try:
    if response.status_code in (301, 302, 303, 307, 308):
      raise ConnectorError(
        "The endpoint redirected. Add the final HTTPS MCP address instead."
      )
    if response.status_code in (401, 403):
      raise ConnectorError(
        "The service rejected the key or requires an OAuth sign-in flow."
      )
    if allow_unsupported and response.status_code in (400, 404, 405):
      payload = await _bounded_error_payload(response, rpc_id)
      if payload is None:
        # A legacy Streamable HTTP endpoint commonly answers an unknown modern
        # probe with an empty/plain HTTP error. Only that unrecognized shape is
        # an era signal; modern JSON-RPC errors remain authoritative.
        return None
      error = payload.get("error")
      if not isinstance(error, dict):
        raise ConnectorError("The service returned an invalid server/discover error.")
      if _modern_error_allows_legacy_fallback(error):
        return None
      raise ConnectorError("The service rejected the modern MCP discovery request.")
    if response.status_code >= 400:
      raise ConnectorError(
        f"The service answered HTTP {response.status_code} to {method}."
      )
    payload = await _matching_rpc(response, rpc_id)
    error = payload.get("error")
    if isinstance(error, dict):
      if allow_unsupported and _modern_error_allows_legacy_fallback(error):
        return None
      raise ConnectorError(f"The service reported an MCP error during {method}.")
    result = payload.get("result")
    if not isinstance(result, dict):
      raise ConnectorError(f"The service returned an invalid {method} result.")
    return result
  finally:
    await response.aclose()


async def _bounded_error_payload(
  response: httpx.Response,
  rpc_id: int,
) -> dict | None:
  """Read a bounded HTTP error body, returning only matching JSON-RPC."""
  chunks: list[bytes] = []
  total = 0
  async for chunk in response.aiter_bytes():
    total += len(chunk)
    if total > _MAX_RPC_BYTES:
      raise ConnectorError("The service returned an MCP response that is too large.")
    chunks.append(chunk)
  try:
    payload = json.loads(b"".join(chunks).decode("utf-8"))
  except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
    return None
  return _matching_payload(payload, rpc_id)


def _modern_error_allows_legacy_fallback(error: dict) -> bool:
  """Whether a recognized modern probe error proves legacy is worth trying."""
  code = error.get("code")
  if code == -32601:
    return True
  if code != -32022:
    return False
  data = error.get("data")
  supported = data.get("supported") if isinstance(data, dict) else None
  return (
    isinstance(supported, list)
    and any(
      isinstance(version, str)
      and version in _SUPPORTED_LEGACY_PROTOCOL_VERSIONS
      for version in supported
    )
  )


async def _modern_catalog(
  client: httpx.AsyncClient,
  endpoint: tuple[str, str, str],
  auth: dict[str, str],
) -> tuple[dict, list[dict]] | None:
  """Read a stateless 2026 catalog, or return ``None`` for a legacy server."""
  discovered = await _modern_rpc(
    client,
    endpoint,
    auth,
    "server/discover",
    {},
    1,
    allow_unsupported=True,
  )
  if discovered is None:
    return None
  versions = discovered.get("supportedVersions")
  if not isinstance(versions, list) or MCP_PROTOCOL_VERSION not in versions:
    return None

  tools: list[dict] = []
  cursor: str | None = None
  for page in range(_MAX_TOOL_PAGES):
    params = {"cursor": cursor} if cursor else {}
    listed = await _modern_rpc(
      client,
      endpoint,
      auth,
      "tools/list",
      params,
      2 + page,
    )
    rows = (listed or {}).get("tools")
    if not isinstance(rows, list):
      raise ConnectorError("The service returned an invalid tool catalog.")
    tools.extend(item for item in rows if isinstance(item, dict))
    if len(tools) > _MAX_TOOLS:
      raise ConnectorError(
        f"The service exposes more than {_MAX_TOOLS} tools; narrow the endpoint first."
      )
    next_cursor = (listed or {}).get("nextCursor")
    cursor = str(next_cursor) if next_cursor else None
    if not cursor:
      break
  else:
    raise ConnectorError("The service tool catalog has too many pages.")

  metadata = discovered.get("_meta")
  server_info = (
    metadata.get("io.modelcontextprotocol/serverInfo")
    if isinstance(metadata, dict)
    else {}
  )
  return (server_info if isinstance(server_info, dict) else {}), tools


async def handshake(
  url: str,
  header_name: str | None = None,
  secret: str | None = None,
) -> dict:
  """Probe a Streamable HTTP MCP endpoint and return its bounded tool catalog."""
  header_name = validate_auth_header(header_name)
  auth = auth_headers(header_name, secret)
  legacy_headers = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": _LEGACY_PROTOCOL_VERSION,
    **auth,
  }
  try:
    async with asyncio.timeout(_HANDSHAKE_DEADLINE_SECONDS):
      # DNS resolution is blocking in the canonical SSRF validator. Keep it
      # inside the one handshake deadline without blocking the event loop.
      endpoint = await asyncio.to_thread(_safe_endpoint, url)
      async with httpx.AsyncClient(
        timeout=_HANDSHAKE_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
      ) as client:
        modern = await _modern_catalog(client, endpoint, auth)
        if modern is not None:
          server_info, tools = modern
          negotiated = MCP_PROTOCOL_VERSION
        else:
          init, session_id = await _initialize_rpc(
            client, endpoint, legacy_headers,
          )
          negotiated = str(init.get("protocolVersion") or "")
          if negotiated not in _SUPPORTED_LEGACY_PROTOCOL_VERSIONS:
            raise ConnectorError(
              "The service negotiated an unsupported MCP version."
            )
          session_headers = dict(legacy_headers)
          session_headers["MCP-Protocol-Version"] = negotiated
          if session_id:
            session_headers["MCP-Session-Id"] = session_id

          await _post_rpc(
            client, endpoint, session_headers,
            "notifications/initialized", {}, None,
          )

          tools = []
          cursor: str | None = None
          for page in range(_MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            listed = await _post_rpc(
              client, endpoint, session_headers,
              "tools/list", params, 2 + page,
            )
            rows = (listed or {}).get("tools")
            if not isinstance(rows, list):
              raise ConnectorError("The service returned an invalid tool catalog.")
            tools.extend(item for item in rows if isinstance(item, dict))
            if len(tools) > _MAX_TOOLS:
              raise ConnectorError(
                f"The service exposes more than {_MAX_TOOLS} tools; narrow the endpoint first."
              )
            next_cursor = (listed or {}).get("nextCursor")
            cursor = str(next_cursor) if next_cursor else None
            if not cursor:
              break
          else:
            raise ConnectorError("The service tool catalog has too many pages.")

          if session_id:
            await _close_session(client, endpoint, session_headers)
          server_info = init.get("serverInfo")
          if not isinstance(server_info, dict):
            server_info = {}
  except ConnectorError:
    raise
  except (TimeoutError, httpx.TimeoutException) as exc:
    raise ConnectorError(
      "The service did not answer before the connection timed out."
    ) from exc
  except httpx.HTTPError as exc:
    raise ConnectorError("Could not reach the MCP service.") from exc

  submitted_secrets = tuple(filter(None, (
    secret or "",
    bare_secret(header_name, secret) if secret else "",
    *(auth_headers(header_name, secret).values() if secret else ()),
  )))
  safe_server_info = _redact_metadata(server_info, submitted_secrets)
  safe_tools = _redact_metadata(tools, submitted_secrets)
  if not isinstance(safe_server_info, dict) or not isinstance(safe_tools, list):
    raise ConnectorError("The service returned invalid MCP metadata.")
  return {
    "name": str(
      safe_server_info.get("title") or safe_server_info.get("name") or ""
    ).strip(),
    "tools": safe_tools,
    "est_tokens": estimate_tokens(safe_tools),
    "protocol_version": negotiated,
  }


# ── Per-turn provider plan ───────────────────────────────────────────────


def _broker_url(connector_id: int) -> str:
  try:
    parsed = urlparse(get_settings().api_base_url)
    _ = parsed.hostname
    configured_port = parsed.port
  except ValueError as exc:
    raise ConnectorError("The configured Möbius API port is invalid.") from exc
  env_port = os.environ.get("PORT")
  if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
    port = configured_port or (443 if parsed.scheme == "https" else 80)
  elif env_port and env_port.isdigit():
    port = int(env_port)
  else:
    port = configured_port or 8000
  if not 1 <= port <= 65535:
    raise ConnectorError("The configured Möbius API port is invalid.")
  return f"http://127.0.0.1:{port}/api/connectors/{connector_id}/broker"


def build_turn_plan(db) -> ConnectorTurnPlan:
  """Snapshot enabled rows into provider config while ``db`` is still live."""
  if db is None:
    return EMPTY_CONNECTOR_TURN_PLAN
  rows = (
    db.query(models.Connector)
    .filter(models.Connector.enabled.is_(True))
    .order_by(models.Connector.id)
    .all()
  )
  claude_servers: dict[str, dict] = {}
  codex_servers: dict[str, dict] = {}
  codex_env: dict[str, str] = {}
  for row in rows:
    try:
      server_key = f"mobius_connector_{row.id}_{slugify(row.slug)}"
      broker_url = _broker_url(row.id)
      capability = mint_broker_capability(row.id, row.capability_id)
      env_var = f"MOBIUS_CONNECTOR_CAPABILITY_{row.id}_{slugify(row.slug).upper()}"

      claude_servers[server_key] = {
        "type": "http",
        "url": broker_url,
        "headers": {"Authorization": f"Bearer {capability}"},
      }
      codex_servers[server_key] = {
        "url": broker_url,
        "startup_timeout_sec": 15,
        "bearer_token_env_var": env_var,
      }
      codex_env[env_var] = capability
    except ConnectorError:
      log.warning("connection %s skipped because its broker plan is invalid", row.slug)

  return ConnectorTurnPlan(
    claude_servers=claude_servers,
    codex_config={"mcp_servers": codex_servers} if codex_servers else None,
    codex_env=codex_env,
  )


@contextmanager
def claude_mcp_config_path(
  plan: ConnectorTurnPlan | None,
) -> Iterator[str | None]:
  """Yield an anonymous 0600 MCP config path for Claude startup only.

  Passing the server dict directly makes the SDK serialize its short-lived
  broker capability into ``--mcp-config <json>`` in the process command line.
  ``TemporaryFile`` is anonymous on Linux; the CLI opens it through this
  process's `/proc` fd while ``connect()`` runs, then the runner closes it
  before the model can execute a tool. There is no named capability file left
  behind after a crash or restart.
  """
  servers = plan.claude_servers if plan else {}
  if not servers:
    yield None
    return
  payload = json.dumps({"mcpServers": servers}, separators=(",", ":")).encode()
  with tempfile.TemporaryFile(prefix="mobius-mcp-", suffix=".json") as handle:
    os.fchmod(handle.fileno(), 0o600)
    handle.write(payload)
    handle.flush()
    handle.seek(0)
    path = f"/proc/{os.getpid()}/fd/{handle.fileno()}"
    if not os.path.exists(path):
      raise ConnectorError("Protected MCP configuration files are unavailable.")
    yield path
