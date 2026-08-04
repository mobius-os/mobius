"""Owner-gated registry for provider-neutral remote MCP connections."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import connectors as core
from app import models
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)

router = APIRouter(
  prefix="/api/connectors",
  tags=["connectors"],
  dependencies=[Depends(reject_cross_site)],
)

_MAX_CONNECTORS = 32
_BROKER_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
_BROKER_REQUEST_HEADERS = {
  "accept",
  "content-type",
  "last-event-id",
  "mcp-protocol-version",
  "mcp-method",
  "mcp-name",
  "mcp-session-id",
}
_BROKER_RESPONSE_HEADERS = {
  "cache-control",
  "content-type",
  "mcp-protocol-version",
  "mcp-session-id",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CREATE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class _BrokerSnapshot:
  url: str
  auth_header: str | None
  secret: str | None


class ConnectorCreate(BaseModel):
  url: str = Field(min_length=8, max_length=2048)
  name: str = Field(default="", max_length=128)
  auth_header: str = Field(default="", max_length=64)
  auth_value: str = Field(default="", max_length=4096)


class ConnectorPatch(BaseModel):
  enabled: bool | None = None
  name: str | None = Field(default=None, min_length=1, max_length=128)


def _public(row: models.Connector) -> dict:
  tools = row.tools_json if isinstance(row.tools_json, list) else []
  return {
    "id": row.id,
    # This generation changes at revocation boundaries. Owner mutations must
    # echo it so a reused SQLite integer key cannot target a later connection.
    "generation": row.capability_id,
    "name": row.name,
    "url": row.url,
    "enabled": row.enabled,
    "has_auth": bool(row.auth_header and row.auth_value_encrypted),
    "tool_count": len(tools),
    "status": row.status,
    "status_detail": row.status_detail,
  }


def _unique_slug(db: Session, base: str) -> str:
  slug = base
  suffix = 2
  while db.query(models.Connector.id).filter(
    models.Connector.slug == slug
  ).first():
    suffix_text = f"_{suffix}"
    slug = f"{base[:64 - len(suffix_text)]}{suffix_text}"
    suffix += 1
  return slug


def _require_generation(
  generation: str | None = Header(
    default=None,
    alias="X-Mobius-Connector-Generation",
  ),
) -> str:
  if not generation or len(generation) > 128:
    raise HTTPException(
      status_code=428,
      detail="Refresh the connection list before changing it.",
    )
  return generation


def _get_row(
  db: Session,
  connector_id: int,
  generation: str,
) -> models.Connector:
  row = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="Connection not found.")
  return row


def _require_loopback(request: Request) -> str:
  peer = request.client.host if request.client else ""
  try:
    address = ipaddress.ip_address(peer)
  except ValueError as exc:
    raise HTTPException(status_code=403, detail="MCP broker access denied.") from exc
  if address.version == 6 and address.ipv4_mapped is not None:
    address = address.ipv4_mapped
  if not address.is_loopback:
    raise HTTPException(status_code=403, detail="MCP broker access denied.")

  scheme, _, token = request.headers.get("authorization", "").partition(" ")
  if scheme.lower() != "bearer" or not token:
    raise HTTPException(status_code=401, detail="MCP broker capability required.")
  return token


def _snapshot_broker_row(
  db: Session,
  connector_id: int,
  capability: str,
) -> _BrokerSnapshot:
  row = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.enabled.is_(True),
    models.Connector.status == "ok",
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="MCP connection unavailable.")
  try:
    core.verify_broker_capability(
      capability,
      connector_id,
      row.capability_id,
    )
  except core.ConnectorError as exc:
    raise HTTPException(
      status_code=401, detail="MCP broker capability rejected."
    ) from exc
  secret = None
  if row.auth_header and row.auth_value_encrypted:
    try:
      secret = core.decrypt_secret(row.auth_value_encrypted)
    except core.ConnectorError as exc:
      raise HTTPException(
        status_code=502, detail="The connection key could not be loaded.",
      ) from exc
  return _BrokerSnapshot(
    url=str(row.url),
    auth_header=str(row.auth_header) if row.auth_header else None,
    secret=secret,
  )


def _broker_request_headers(
  request: Request,
  snapshot: _BrokerSnapshot,
) -> dict[str, str]:
  headers = {
    name: value for name, value in request.headers.items()
    if (
      name.lower() in _BROKER_REQUEST_HEADERS
      or name.lower().startswith("mcp-param-")
    )
  }
  headers.update(core.auth_headers(snapshot.auth_header, snapshot.secret))
  return headers


def _broker_secret_patterns(snapshot: _BrokerSnapshot) -> tuple[bytes, ...]:
  if not snapshot.secret:
    return ()
  submitted = snapshot.secret
  values = {
    submitted,
    core.bare_secret(snapshot.auth_header, submitted),
    *core.auth_headers(snapshot.auth_header, submitted).values(),
  }
  # JSON string escaping can transform quotes/newlines in a reflected key.
  values.update(json.dumps(value)[1:-1] for value in tuple(values))
  return tuple(sorted(
    {value.encode("utf-8") for value in values if value},
    key=len,
    reverse=True,
  ))


def _broker_response_headers(
  upstream: httpx.Response,
  snapshot: _BrokerSnapshot,
) -> dict[str, str]:
  """Forward only transport headers whose values cannot reflect a secret."""
  patterns = _broker_secret_patterns(snapshot)
  return {
    name: value
    for name, value in upstream.headers.items()
    if name.lower() in _BROKER_RESPONSE_HEADERS
    and not any(pattern in value.encode("utf-8") for pattern in patterns)
  }


async def _redacted_broker_stream(
  upstream: httpx.Response,
  snapshot: _BrokerSnapshot,
):
  """Stream MCP bytes without leaking a reflected connection credential."""
  patterns = _broker_secret_patterns(snapshot)
  if not patterns:
    async for chunk in upstream.aiter_bytes():
      yield chunk
    return

  pending = b""
  keep = max(len(pattern) for pattern in patterns) - 1
  replacement = b"[redacted]"
  async for chunk in upstream.aiter_bytes():
    pending += chunk
    scan_before = max(0, len(pending) - keep)
    if not scan_before:
      continue
    output = bytearray()
    position = 0
    while position < scan_before:
      match = next(
        (pattern for pattern in patterns if pending.startswith(pattern, position)),
        None,
      )
      if match is not None:
        output.extend(replacement)
        position += len(match)
      else:
        output.append(pending[position])
        position += 1
    pending = pending[position:]
    if output:
      yield bytes(output)
  while pending:
    match = next(
      (pattern for pattern in patterns if pending.startswith(pattern)),
      None,
    )
    if match is not None:
      yield replacement
      pending = pending[len(match):]
    else:
      yield pending[:1]
      pending = pending[1:]


async def _open_broker_upstream(
  request: Request,
  snapshot: _BrokerSnapshot,
) -> tuple[httpx.AsyncClient, httpx.Response]:
  try:
    pinned_url, host_header, sni_host = await asyncio.wait_for(
      asyncio.to_thread(core._safe_endpoint, snapshot.url),
      timeout=10.0,
    )
  except TimeoutError as exc:
    raise HTTPException(
      status_code=504, detail="The MCP connection address did not resolve in time.",
    ) from exc
  except core.ConnectorError as exc:
    raise HTTPException(
      status_code=502, detail="The MCP connection address is no longer safe.",
    ) from exc

  client = httpx.AsyncClient(
    follow_redirects=False,
    timeout=_BROKER_TIMEOUT,
    trust_env=False,
  )
  try:
    declared_length = request.headers.get("content-length")
    has_body = (
      (declared_length is not None and declared_length != "0")
      or "transfer-encoding" in request.headers
    )
    content = request.stream() if has_body else None
    upstream_request = client.build_request(
      request.method,
      pinned_url,
      headers=_broker_request_headers(request, snapshot),
      content=content,
    )
    upstream_request.headers["host"] = host_header
    upstream_request.extensions["sni_hostname"] = sni_host
    upstream = await client.send(upstream_request, stream=True)
    if upstream.status_code in _REDIRECT_STATUSES:
      await upstream.aclose()
      raise HTTPException(
        status_code=502,
        detail="The MCP service redirected; configure its final HTTPS address.",
      )
    return client, upstream
  except HTTPException:
    await client.aclose()
    raise
  except httpx.TimeoutException as exc:
    await client.aclose()
    raise HTTPException(status_code=504, detail="The MCP service timed out.") from exc
  except httpx.HTTPError as exc:
    await client.aclose()
    raise HTTPException(status_code=502, detail="Could not reach the MCP service.") from exc
  except BaseException:
    await client.aclose()
    raise


@router.api_route("/{connector_id}/broker", methods=["GET", "POST", "DELETE"])
async def broker_connector(
  connector_id: int,
  request: Request,
  db: Session = Depends(get_db),
):
  """Stream one capability-authenticated MCP exchange to the pinned endpoint.

  Only provider processes in this container can reach this route. The remote
  endpoint and credential are copied out of the database, then the session is
  closed before DNS validation, request upload, or response streaming begins.
  """
  capability = _require_loopback(request)
  try:
    snapshot = _snapshot_broker_row(db, connector_id, capability)
  finally:
    db.close()

  client, upstream = await _open_broker_upstream(request, snapshot)

  async def stream():
    try:
      async for chunk in _redacted_broker_stream(upstream, snapshot):
        yield chunk
    finally:
      await upstream.aclose()
      await client.aclose()

  headers = _broker_response_headers(upstream, snapshot)
  return StreamingResponse(
    stream(),
    status_code=upstream.status_code,
    headers=headers,
  )


@router.get("")
async def list_connectors(
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  rows = db.query(models.Connector).order_by(models.Connector.id).all()
  return {"connectors": [_public(row) for row in rows]}


@router.post("", status_code=201)
async def add_connector(
  body: ConnectorCreate,
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  url = body.url.strip()
  auth_value = body.auth_value.strip()
  auth_header = body.auth_header.strip()
  if auth_value and not auth_header:
    auth_header = "Authorization"
  if auth_header and not auth_value:
    raise HTTPException(
      status_code=400,
      detail="Enter an API key or remove the header name.",
  )
  try:
    normalized_header = core.validate_auth_header(auth_header or None)
    # The probe can consume the complete handshake deadline. Release the
    # checked-out connection first; this Session reacquires only after the
    # network result is back in hand.
    db.close()
    probe = await core.handshake(url, normalized_header, auth_value or None)
  except core.ConnectorError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc

  parsed = urlparse(url)
  name = body.name.strip() or probe["name"] or (parsed.hostname or "MCP server")
  # The supported deployment has one API worker. Serialize the short commit
  # window so two Settings tabs cannot both pass the limit check or allocate
  # the same slug. The database uniqueness constraint remains the final guard.
  async with _CREATE_LOCK:
    if db.query(models.Connector.id).count() >= _MAX_CONNECTORS:
      raise HTTPException(status_code=400, detail="Connection limit reached.")
    row = models.Connector(
      slug=_unique_slug(db, core.slugify(name)),
      name=name[:128],
      url=url,
      auth_header=normalized_header,
      auth_value_encrypted=(
        core.encrypt_secret(auth_value) if normalized_header and auth_value else None
      ),
      enabled=True,
      tools_json=probe["tools"],
      est_tokens=0,
      status="ok",
      status_detail=None,
      last_checked_at=now_naive_utc(),
    )
    db.add(row)
    try:
      db.commit()
    except IntegrityError as exc:
      db.rollback()
      raise HTTPException(
        status_code=409,
        detail="The connection list changed; try adding it again.",
      ) from exc
    db.refresh(row)
  log.info("MCP connection added: %s (%d tools)", row.slug, len(probe["tools"]))
  return _public(row)


@router.patch("/{connector_id}")
async def patch_connector(
  connector_id: int,
  body: ConnectorPatch,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  row = _get_row(db, connector_id, generation)
  values: dict[str, object] = {}
  if body.enabled is not None:
    if body.enabled and row.status != "ok":
      raise HTTPException(
        status_code=409,
        detail="Re-check this connection successfully before enabling it.",
      )
    if row.enabled and not body.enabled:
      # Disabling is a revocation boundary. Give a later re-enable a fresh
      # identity so a capability minted before the disable cannot revive.
      values["capability_id"] = secrets.token_hex(32)
    values["enabled"] = body.enabled
  if body.name is not None:
    values["name"] = body.name.strip()[:128]
  if not values:
    return _public(row)
  target = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  )
  if body.enabled:
    target = target.filter(models.Connector.status == "ok")
  updated = target.update(values, synchronize_session=False)
  if updated != 1:
    db.rollback()
    if body.enabled:
      raise HTTPException(
        status_code=409,
        detail="Re-check this connection successfully before enabling it.",
      )
    raise HTTPException(
      status_code=404,
      detail="The connection changed before the update completed.",
    )
  db.commit()
  current_generation = str(values.get("capability_id", generation))
  current = _get_row(db, connector_id, current_generation)
  return _public(current)


@router.post("/{connector_id}/refresh")
async def refresh_connector(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  stored = _get_row(db, connector_id, generation)
  secret = None
  if stored.auth_header and stored.auth_value_encrypted:
    try:
      secret = core.decrypt_secret(stored.auth_value_encrypted)
    except core.ConnectorError as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  url = str(stored.url)
  auth_header = str(stored.auth_header) if stored.auth_header else None
  # Do not lease a DB connection while waiting on DNS or a remote service.
  # Re-fetch after the probe so a concurrent disable/delete remains decisive.
  db.close()
  try:
    probe = await core.handshake(url, auth_header, secret)
    values = {
      "tools_json": probe["tools"],
      "est_tokens": 0,
      "status": "ok",
      "status_detail": None,
    }
  except core.ConnectorError as exc:
    values = {
      "status": "error",
      "status_detail": str(exc),
    }
  values["last_checked_at"] = now_naive_utc()
  identity = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  )
  updated = identity.update(values, synchronize_session=False)
  if updated != 1:
    db.rollback()
    raise HTTPException(
      status_code=404,
      detail="The connection changed while it was being refreshed.",
    )
  db.commit()
  row = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  ).first()
  if row is None:
    raise HTTPException(
      status_code=404,
      detail="The connection changed while it was being refreshed.",
    )
  return _public(row)


@router.delete("/{connector_id}")
async def delete_connector(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  deleted = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  ).delete(synchronize_session=False)
  if deleted != 1:
    db.rollback()
    raise HTTPException(status_code=404, detail="Connection not found.")
  db.commit()
  return {"ok": True}
