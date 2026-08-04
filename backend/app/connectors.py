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
from typing import BinaryIO, Iterator
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app import models
from app.config import get_settings
from app.net_utils import validate_url_safe

log = logging.getLogger(__name__)

# The shared registry targets the newest protocol understood by both bundled
# providers. Codex may move ahead independently, but a saved connection must
# remain usable by Claude as well.
MCP_PROTOCOL_VERSION = "2025-11-25"
_SUPPORTED_PROTOCOL_VERSIONS = {
  MCP_PROTOCOL_VERSION,
  "2025-06-18",
  "2025-03-26",
}
_HANDSHAKE_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_HANDSHAKE_DEADLINE_SECONDS = 22.0
_MAX_RPC_BYTES = 2 * 1024 * 1024
_MAX_TOOLS = 128
_MAX_TOOL_PAGES = 10
_MIN_AUTH_SECRET_CHARS = 8
_MAX_AUTH_SECRET_CHARS = 4096
# A provider turn can legitimately remain alive for hours, but a copied child
# environment must not retain broker access for days. Cap one turn credential
# at 24 hours; disabling or deleting the connector revokes it immediately
# because every broker request still re-reads the enabled row.
_BROKER_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_HEADER_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RESERVED_AUTH_HEADERS = {
  "accept",
  "connection",
  "content-length",
  "content-type",
  "expect",
  "host",
  "keep-alive",
  "last-event-id",
  "mcp-method",
  "mcp-name",
  "mcp-protocol-version",
  "mcp-session-id",
  "origin",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
}


class ConnectorError(Exception):
  """A connection failure safe to present in the owner-facing Settings UI."""


@dataclass(frozen=True)
class ConnectorTurnPlan:
  """Detached provider configuration built before the turn releases its DB.

  The values contain turn-lifetime (at most 24-hour) broker capabilities, so
  their repr is deliberately empty of fields and callers must never log or
  persist it.
  """

  claude_servers: dict[str, dict] = field(default_factory=dict, repr=False)
  codex_config: dict | None = field(default=None, repr=False)
  codex_env: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass
class ClaudeMcpConfigHandle:
  """Anonymous startup config whose proc fd stays safely reserved for a turn."""

  path: str
  _file: BinaryIO = field(repr=False)
  _retired: bool = field(default=False, init=False, repr=False)

  def retire(self) -> None:
    """Destroy the config while keeping its argv-visible fd bound harmlessly."""
    if self._retired:
      return
    with open(os.devnull, "rb") as harmless:
      os.dup2(harmless.fileno(), self._file.fileno())
    self._retired = True


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


# ── Pure naming/auth helpers ─────────────────────────────────────


def slugify(name: str) -> str:
  slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
  return slug[:48] or "connector"


def validate_auth_header(name: str | None) -> str | None:
  value = (name or "").strip()
  if not value:
    return None
  if not _HEADER_RE.fullmatch(value):
    raise ConnectorError("The API-key header name is not valid.")
  lower = value.lower()
  if lower in _RESERVED_AUTH_HEADERS or lower.startswith("mcp-param-"):
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
  """Return the token form Codex expects for Authorization credentials."""
  if header_name and header_name.lower() == "authorization":
    return re.sub(r"(?i)^bearer\s+", "", secret, count=1)
  return secret


def validate_auth_secret(
  header_name: str | None,
  secret: str | None,
) -> str | None:
  """Keep static keys safely bounded and specific enough to redact."""
  if secret and len(secret) > _MAX_AUTH_SECRET_CHARS:
    raise ConnectorError("The API key is too long.")
  effective = bare_secret(header_name, secret) if secret else None
  if effective and len(effective) < _MIN_AUTH_SECRET_CHARS:
    raise ConnectorError("API keys must be at least 8 characters.")
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


def _redact_text(value: object, secrets: tuple[str, ...], limit: int) -> str:
  """Normalize, redact, and bound the remote text that may be persisted."""
  text = " ".join(str(value or "").split())
  for secret in secrets:
    text = text.replace(secret, "[redacted]")
  return text[:limit]


def _tool_names(
  tools: list[dict],
  secrets: tuple[str, ...],
) -> list[str]:
  """Keep only bounded names so the registry can present a tool count."""
  names = [
    _redact_text(tool.get("name"), secrets, 128) for tool in tools
  ]
  return [name for name in names if name]


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
        "protocolVersion": MCP_PROTOCOL_VERSION,
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


async def handshake(
  url: str,
  header_name: str | None = None,
  secret: str | None = None,
) -> dict:
  """Probe a shared-provider MCP endpoint and return bounded tool names."""
  header_name = validate_auth_header(header_name)
  secret = validate_auth_secret(header_name, secret)
  auth = auth_headers(header_name, secret)
  headers = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
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
        init, session_id = await _initialize_rpc(client, endpoint, headers)
        negotiated = str(init.get("protocolVersion") or "")
        if negotiated not in _SUPPORTED_PROTOCOL_VERSIONS:
          raise ConnectorError(
            "The service negotiated an unsupported MCP version."
          )
        session_headers = dict(headers)
        session_headers["MCP-Protocol-Version"] = negotiated
        if session_id:
          session_headers["MCP-Session-Id"] = session_id

        try:
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
        finally:
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

  submitted_secrets = tuple(sorted({
    value
    for value in (
      secret or "",
      bare_secret(header_name, secret) if secret else "",
      *auth.values(),
    )
    if value
  }, key=len, reverse=True))
  return {
    "name": _redact_text(
      server_info.get("title") or server_info.get("name"),
      submitted_secrets,
      128,
    ),
    "tools": _tool_names(tools, submitted_secrets),
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


def build_turn_plan(
  db,
  *,
  include_owner_connectors: bool,
) -> ConnectorTurnPlan | None:
  """Snapshot allowed, enabled rows while ``db`` is still live.

  The policy decision is a required argument so a new child-run caller cannot
  accidentally inherit the owner's remote services. A future delegated grant
  can opt in at the call site that owns that policy.
  """
  if db is None or not include_owner_connectors:
    return None
  rows = (
    db.query(models.Connector)
    .filter(
      models.Connector.enabled.is_(True),
      models.Connector.status == "ok",
    )
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
        "startup_timeout_sec": 30,
        "bearer_token_env_var": env_var,
      }
      codex_env[env_var] = capability
    except ConnectorError:
      log.warning("connection %s skipped because its broker plan is invalid", row.slug)

  if not claude_servers:
    return None
  return ConnectorTurnPlan(
    claude_servers=claude_servers,
    codex_config={"mcp_servers": codex_servers},
    codex_env=codex_env,
  )


@contextmanager
def claude_mcp_config_handle(
  plan: ConnectorTurnPlan | None,
) -> Iterator[ClaudeMcpConfigHandle | None]:
  """Yield an anonymous 0600 MCP config handle for Claude startup only.

  Passing the server dict directly makes the SDK serialize its short-lived
  broker capability into ``--mcp-config <json>`` in the process command line.
  ``TemporaryFile`` is anonymous on Linux; the CLI opens it through this
  process's ``/proc`` fd while ``connect()`` runs. The runner then calls
  :meth:`ClaudeMcpConfigHandle.retire`, atomically replacing that descriptor
  with ``/dev/null``. Keeping the harmless descriptor reserved until teardown
  prevents the argv-visible fd number from being reused for unrelated data.
  There is no named capability file left behind after a crash or restart.
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
    yield ClaudeMcpConfigHandle(path=path, _file=handle)
