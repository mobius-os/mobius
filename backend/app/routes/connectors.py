"""Owner-gated registry for provider-neutral remote MCP connections."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import dataclasses
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import connectors as core
from app import models
from app.database import get_db
from app.deps import (
  get_owner_or_app_with_connections_manage,
  reject_cross_site,
)
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)

router = APIRouter(
  prefix="/api/connectors",
  tags=["connectors"],
  dependencies=[Depends(reject_cross_site)],
)

# Two OAuth endpoints are reached cross-origin by design and must NOT carry
# the CSRF guard: the client-metadata document is fetched by an authorization
# server, and the callback lands as the provider's top-level redirect. Both
# are safe without it — the metadata is public, and the callback authorizes
# solely on the unforgeable sealed ``state`` (no ambient credential to abuse).
public_router = APIRouter(prefix="/api/connectors", tags=["connectors"])

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
  # Validate this write-only value in the route. Pydantic includes rejected
  # field input in its standard 422 body, which would echo an invalid key.
  auth_value: object = ""


class ConnectorPatch(BaseModel):
  enabled: bool | None = None
  name: str | None = Field(default=None, min_length=1, max_length=128)

  @field_validator("name")
  @classmethod
  def nonblank_name(cls, value: str | None) -> str | None:
    if value is None:
      return None
    stripped = value.strip()
    if not stripped:
      raise ValueError("Connection name must not be blank.")
    return stripped


def _public(
  row: models.Connector,
  oauth: "models.ConnectorOAuth | None" = None,
) -> dict:
  tools = row.tools_json if isinstance(row.tools_json, list) else []
  # Legacy rows persisted full tool metadata dicts; present bounded names.
  names = [
    tool.get("name") if isinstance(tool, dict) else tool
    for tool in tools
  ]
  names = [name for name in names if isinstance(name, str) and name]
  return {
    "id": row.id,
    # This generation changes at revocation boundaries. Owner mutations must
    # echo it so a reused SQLite integer key cannot target a later connection.
    "generation": row.capability_id,
    "name": row.name,
    "url": row.url,
    "enabled": row.enabled,
    "has_auth": bool(row.auth_header and row.auth_value_encrypted),
    "tools": names,
    "tool_count": len(tools),
    "est_tokens": row.est_tokens or 0,
    "status": row.status,
    "status_detail": row.status_detail,
    # OAuth surface — tokens NEVER cross this boundary, only these booleans.
    "auth_kind": "oauth" if oauth is not None else (
      "key" if row.auth_header and row.auth_value_encrypted else "none"
    ),
    "signed_in": bool(oauth is not None and oauth.access_token_encrypted),
    "scopes": list(oauth.scopes_granted or []) if oauth is not None else [],
  }


def _oauth_row(db: Session, connector_id: int) -> "models.ConnectorOAuth | None":
  return (
    db.query(models.ConnectorOAuth)
    .filter(models.ConnectorOAuth.connector_id == connector_id)
    .first()
  )


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
  try:
    auth_header = core.validate_auth_header(row.auth_header)
  except core.ConnectorError as exc:
    raise HTTPException(
      status_code=502,
      detail="The connection key configuration is not valid.",
    ) from exc
  if auth_header and row.auth_value_encrypted:
    try:
      secret = core.decrypt_secret(row.auth_value_encrypted)
      core.validate_auth_secret(auth_header, secret)
    except core.ConnectorError as exc:
      raise HTTPException(
        status_code=502, detail="The connection key could not be loaded.",
      ) from exc
  return _BrokerSnapshot(
    url=str(row.url),
    auth_header=auth_header,
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
    # OAuth connections carry no static key: resolve (and refresh, if near
    # expiry) the access token here, while the session is still live and
    # before any upstream streaming. Refresh-before-attach means the broker
    # never has to retry a non-replayable request body after a 401.
    if snapshot.auth_header is None:
      from app import connector_oauth
      oauth = _oauth_row(db, connector_id)
      if oauth is not None:
        token = await connector_oauth.usable_access_token(db, connector_id)
        if token is None:
          raise HTTPException(
            status_code=404, detail="MCP connection unavailable.",
          )
        snapshot = dataclasses.replace(
          snapshot, auth_header="Authorization", secret=token,
        )
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
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  rows = db.query(models.Connector).order_by(models.Connector.id).all()
  # One batched lookup instead of N: the OAuth rows for exactly these ids.
  oauth_by_id = {
    o.connector_id: o
    for o in db.query(models.ConnectorOAuth).filter(
      models.ConnectorOAuth.connector_id.in_([r.id for r in rows] or [0])
    )
  }
  return {
    "connectors": [_public(row, oauth_by_id.get(row.id)) for row in rows]
  }


@router.post("", status_code=201)
async def add_connector(
  body: ConnectorCreate,
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  url = body.url.strip()
  if not isinstance(body.auth_value, str):
    raise HTTPException(status_code=400, detail="The API key must be text.")
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
    normalized_secret = core.validate_auth_secret(
      normalized_header,
      auth_value or None,
    )
    # The probe can consume the complete handshake deadline. Release the
    # checked-out connection first; this Session reacquires only after the
    # network result is back in hand.
    db.close()
    probe = await core.handshake(url, normalized_header, normalized_secret)
    discovery = None
  except core.OAuthSignInRequired as needs_oauth:
    # An OAuth-gated MCP server. This is a successful add of a signed-out
    # connection, not a failure: save the discovery so the owner can sign in.
    probe = None
    discovery = needs_oauth.discovery
  except core.ConnectorError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc

  parsed = urlparse(url)
  name = (
    body.name.strip()
    or (probe["name"] if probe else "")
    or (parsed.hostname or "MCP server")
  )
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
        core.encrypt_secret(normalized_secret)
        if normalized_header and normalized_secret else None
      ),
      enabled=True,
      tools_json=probe["tools"] if probe else [],
      est_tokens=probe["est_tokens"] if probe else 0,
      status="ok" if probe else "oauth_required",
      status_detail=None if probe else "Sign in to finish connecting.",
      last_checked_at=now_naive_utc(),
    )
    db.add(row)
    try:
      db.flush()
      if discovery is not None:
        db.add(models.ConnectorOAuth(
          connector_id=row.id, **discovery,
        ))
      db.commit()
    except IntegrityError as exc:
      db.rollback()
      raise HTTPException(
        status_code=409,
        detail="The connection list changed; try adding it again.",
      ) from exc
    db.refresh(row)
  log.info(
    "MCP connection added: %s (%s)",
    row.slug,
    "oauth sign-in required" if discovery else f"{len(probe['tools'])} tools",
  )
  return _public(row, _oauth_row(db, row.id))


@router.patch("/{connector_id}")
async def patch_connector(
  connector_id: int,
  body: ConnectorPatch,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  row = _get_row(db, connector_id, generation)
  values: dict[str, object] = {}
  if body.enabled is not None:
    if row.enabled and not body.enabled:
      # Disabling is a revocation boundary. Give a later re-enable a fresh
      # identity so a capability minted before the disable cannot revive.
      values["capability_id"] = secrets.token_hex(32)
    values["enabled"] = body.enabled
  if body.name is not None:
    values["name"] = body.name
  if not values:
    return _public(row, _oauth_row(db, connector_id))
  # The conditional UPDATE is the single enforcement point: filters express
  # every precondition, so the miss branch below is the real (and only)
  # rejection path rather than a dead backstop behind a pre-check.
  target = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  )
  if body.enabled:
    target = target.filter(models.Connector.status == "ok")
  updated = target.update(values, synchronize_session=False)
  if updated != 1:
    db.rollback()
    if body.enabled and row.status != "ok":
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
  return _public(current, _oauth_row(db, connector_id))


def _record_check(connector_id: int, generation: str, values: dict) -> None:
  """Identity-filtered health write in its own short session.

  Shared by the static-key refresh, the OAuth refresh, and the post-sign-in
  probe: a concurrent disable/delete rotates the generation, so a stale
  check can never overwrite a newer row's state.
  """
  values = {**values, "last_checked_at": now_naive_utc()}
  session = _reopen()
  try:
    updated = session.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == generation,
    ).update(values, synchronize_session=False)
    if updated == 1:
      session.commit()
    else:
      session.rollback()
  finally:
    session.close()


async def _probe_signed_in(
  url: str, connector_id: int, generation: str, token: str, prior_status: str,
) -> None:
  """Probe an OAuth row WITH its access token and record the outcome.

  The 2026-08-05 morning bug: the auto-probe ran unauthenticated against a
  signed-in row, took the provider's routine 401 as a failure, and latched a
  working connection to "error". Signed-in rows are only ever probed with
  their token, and an auth rejection latches to signed-out — a recoverable
  state whose fix (sign in) is one tap — never to "error".
  """
  clear_grant = False
  try:
    probe = await core.handshake(url, "Authorization", token)
    values = {
      "tools_json": probe["tools"],
      "est_tokens": probe["est_tokens"],
      "status": "ok",
      "status_detail": None,
    }
  except core.ConnectorError as exc:
    if exc.auth_rejected:
      # The grant no longer works. Latch signed-out AND clear the dead tokens
      # so status and signed_in stay consistent (a still-unexpired but
      # revoked token would otherwise read as signed-in-yet-unavailable).
      values = {
        "status": "oauth_required",
        "status_detail": "Sign in again to reconnect.",
      }
      clear_grant = True
    elif exc.transient:
      values = {"status_detail": str(exc)} if prior_status == "ok" else {}
    else:
      values = {"status": "error", "status_detail": str(exc)}
  _record_check(connector_id, generation, values)
  if clear_grant:
    session = _reopen()
    try:
      # Generation-guard the wipe exactly like _record_check: if a concurrent
      # disconnect/delete rotated capability_id, this stale probe must not
      # clear a newer, valid grant.
      current = session.query(models.Connector).filter(
        models.Connector.id == connector_id,
        models.Connector.capability_id == generation,
      ).first()
      oauth = _oauth_row(session, connector_id) if current is not None else None
      if oauth is not None:
        oauth.access_token_encrypted = None
        oauth.refresh_token_encrypted = None
        oauth.access_expires_at = None
        oauth.scopes_granted = []
        session.commit()
    finally:
      session.close()


@router.post("/{connector_id}/refresh")
async def refresh_connector(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  stored = _get_row(db, connector_id, generation)
  oauth = _oauth_row(db, connector_id)
  if oauth is not None:
    from app import connector_oauth

    url = str(stored.url)
    prior_status = str(stored.status)
    # May refresh the token (commits internally); needs the live session.
    token = await connector_oauth.usable_access_token(db, connector_id)
    db.close()
    if token is None:
      _record_check(connector_id, generation, {
        "status": "oauth_required",
        "status_detail": "Sign in to finish connecting.",
      })
    else:
      await _probe_signed_in(url, connector_id, generation, token, prior_status)
    session = _reopen()
    try:
      row = session.query(models.Connector).filter(
        models.Connector.id == connector_id,
        models.Connector.capability_id == generation,
      ).first()
      if row is None:
        raise HTTPException(
          status_code=404,
          detail="The connection changed while it was being refreshed.",
        )
      return _public(row, _oauth_row(session, connector_id))
    finally:
      session.close()
  secret = None
  try:
    auth_header = core.validate_auth_header(stored.auth_header)
  except core.ConnectorError as exc:
    raise HTTPException(
      status_code=409,
      detail="The connection key configuration is not valid.",
    ) from exc
  if auth_header and stored.auth_value_encrypted:
    try:
      secret = core.decrypt_secret(stored.auth_value_encrypted)
      core.validate_auth_secret(auth_header, secret)
    except core.ConnectorError as exc:
      raise HTTPException(status_code=409, detail=str(exc)) from exc
  url = str(stored.url)
  prior_status = str(stored.status)
  # Do not lease a DB connection while waiting on DNS or a remote service.
  # Re-fetch after the probe so a concurrent disable/delete remains decisive.
  db.close()
  try:
    probe = await core.handshake(url, auth_header, secret)
    values = {
      "tools_json": probe["tools"],
      "est_tokens": probe["est_tokens"],
      "status": "ok",
      "status_detail": None,
    }
  except core.ConnectorError as exc:
    if exc.transient:
      # A transport blip says nothing definitive about the configuration.
      # Keep the last known health so one flaky moment cannot silently
      # remove the connection from later agent turns. Annotate only healthy
      # rows: an already-latched row keeps its definitive diagnosis instead
      # of having it overwritten by a generic transport message.
      values = {"status_detail": str(exc)} if prior_status == "ok" else {}
    else:
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
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  deleted = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  ).delete(synchronize_session=False)
  if deleted != 1:
    db.rollback()
    raise HTTPException(status_code=404, detail="Connection not found.")
  # Delete the paired OAuth grant in the same transaction. SQLite FK cascade
  # is not enforced here, and SQLite reuses INTEGER primary keys after a
  # delete — an orphaned grant row would let a later connection that reused
  # this id inherit the previous provider's sealed tokens (a cross-provider
  # credential leak through the broker). Gated on deleted==1 so a
  # generation-mismatch (already rolled back) never touches a live grant.
  db.query(models.ConnectorOAuth).filter(
    models.ConnectorOAuth.connector_id == connector_id,
  ).delete(synchronize_session=False)
  db.commit()
  return {"ok": True}


# ── OAuth sign-in ──────────────────────────────────────────────────────────


@public_router.get("/oauth/client-metadata.json")
async def oauth_client_metadata():
  """This instance's OAuth Client ID Metadata Document (public, unauthed).

  An authorization server fetches this by URL during sign-in; the URL IS the
  client_id. No owner data here — only this instance's public client identity.
  """
  from app import connector_oauth

  return connector_oauth.client_metadata_document()


@router.post("/{connector_id}/oauth/start")
async def oauth_start(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Begin sign-in: return the provider authorization URL for a popup.

  The PKCE verifier + connector binding travel in a sealed ``state`` value,
  so there is no cookie or server-side record to lose across the popup.
  """
  from app import connector_oauth

  stored = _get_row(db, connector_id, generation)
  oauth = _oauth_row(db, connector_id)
  if oauth is None:
    raise HTTPException(
      status_code=409, detail="This connection does not use sign-in.",
    )
  try:
    # Sign-in is rare and owner-present: re-run live discovery so the client
    # identity decision sees the AS's CURRENT capabilities (the cached row
    # keeps only endpoints for the refresh hot path) and stale endpoints
    # self-heal. The refreshed values are written back to the cache.
    discovery = await connector_oauth.discover(str(stored.url), "")
    for field_name, value in discovery.as_row_fields().items():
      setattr(oauth, field_name, value)
    db.commit()
    # Client registration may reach the authorization server (network), but it
    # is a rare one-time-per-issuer step and needs the session for its cache;
    # repeat sign-ins reuse the cached registration and touch no network here.
    client_id, _secret = await connector_oauth.ensure_client(db, discovery)
    verifier, challenge = connector_oauth.generate_pkce()
    state = connector_oauth.seal_flow({
      "connector_id": connector_id,
      "generation": generation,
      "verifier": verifier,
      "client_id": client_id,
      "issuer": discovery.issuer,
    })
    authorize_url = connector_oauth.authorization_url(
      discovery, client_id, challenge, state,
    )
  except core.ConnectorError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc
  return {"authorize_url": authorize_url}


@public_router.get("/oauth/callback")
async def oauth_callback(
  code: str = "",
  state: str = "",
  error: str = "",
  iss: str = "",
):
  """Provider redirect target. Exchanges the code and seals the tokens.

  Lands as a top-level navigation with no Bearer token; authorization is the
  unforgeable sealed ``state`` (bound to this instance's key). Returns a tiny
  self-closing page so the popup disappears and the app refreshes.
  """
  from app import connector_oauth

  flow = connector_oauth.open_flow(state) if state else None
  if error or not code or flow is None:
    return _oauth_result_page(ok=False)

  db = _reopen()
  try:
    oauth = _oauth_row(db, int(flow["connector_id"]))
    row = db.query(models.Connector).filter(
      models.Connector.id == int(flow["connector_id"]),
    ).first()
    if oauth is None or row is None:
      return _oauth_result_page(ok=False)
    # RFC 9207: if the AS returned an issuer, it MUST match the one we started
    # with, compared as exact strings.
    if iss and iss != str(flow.get("issuer") or ""):
      return _oauth_result_page(ok=False)

    discovery = connector_oauth.Discovery.from_row(oauth)
    _client_id, client_secret = connector_oauth._client_for_refresh(db, oauth)
    db.close()
    try:
      tokens = await connector_oauth.exchange_code(
        discovery, str(flow["client_id"]), client_secret,
        code, str(flow["verifier"]),
      )
    except core.ConnectorError:
      return _oauth_result_page(ok=False)

    db = _reopen()
    oauth = _oauth_row(db, int(flow["connector_id"]))
    row = db.query(models.Connector).filter(
      models.Connector.id == int(flow["connector_id"]),
      # Honor the sealed generation: if the connection was disconnected or
      # deleted-and-reused between start and callback, its capability_id has
      # rotated, so this write must NOT revive a revoked grant or land tokens
      # on a reused row id. Every other mutation is generation-guarded too.
      models.Connector.capability_id == str(flow.get("generation") or ""),
    ).first()
    if oauth is None or row is None:
      return _oauth_result_page(ok=False)
    connector_oauth._store_tokens(oauth, tokens)
    oauth.connected_at = now_naive_utc()
    row.status = "ok"
    row.status_detail = None
    row_url = str(row.url)
    row_generation = str(row.capability_id)
    db.commit()
  finally:
    db.close()
  # Populate tools + cost immediately with the fresh grant. Best-effort: the
  # sign-in already succeeded, and a transient probe failure only leaves the
  # detail note that the card's next auto-check clears.
  await _probe_signed_in(
    row_url, int(flow["connector_id"]), row_generation,
    str(tokens["access_token"]), "ok",
  )
  return _oauth_result_page(ok=True)


@router.post("/{connector_id}/oauth/disconnect")
async def oauth_disconnect(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Sign out: best-effort revoke, clear tokens, and rotate the generation."""
  from app import connector_oauth

  row = _get_row(db, connector_id, generation)
  oauth = _oauth_row(db, connector_id)
  if oauth is None:
    raise HTTPException(
      status_code=409, detail="This connection does not use sign-in.",
    )
  refresh_encrypted = oauth.refresh_token_encrypted
  revocation_endpoint = oauth.revocation_endpoint
  issuer = oauth.issuer
  # Clear + latch signed-out + rotate the capability (a revocation boundary,
  # exactly like disable) so any minted broker capability cannot revive.
  oauth.access_token_encrypted = None
  oauth.refresh_token_encrypted = None
  oauth.access_expires_at = None
  oauth.scopes_granted = []
  new_generation = secrets.token_hex(32)
  row.capability_id = new_generation
  row.status = "oauth_required"
  row.status_detail = "Signed out. Sign in to reconnect."
  # Leave `enabled` alone: status=oauth_required already withholds the
  # connection from every turn, and preserving the owner's on/off preference
  # means a later re-sign-in restores it without a re-toggle — consistent with
  # the expired-token path, which also never touches `enabled`.
  db.commit()
  refreshed = _get_row(db, connector_id, new_generation)
  public = _public(refreshed, _oauth_row(db, connector_id))
  # Best-effort RFC 7009 revocation after the local state is already safe.
  if revocation_endpoint and refresh_encrypted:
    try:
      token = core.decrypt_oauth(refresh_encrypted)
      await core.pinned_json_request(
        "POST", revocation_endpoint,
        form={"token": token, "token_type_hint": "refresh_token"},
        timeout_seconds=8.0,
      )
    except core.ConnectorError:
      pass  # Local sign-out already succeeded; upstream revoke is advisory.
  return public


def _reopen() -> Session:
  """A fresh session for a step that runs after an earlier db.close()."""
  from app.database import SessionLocal

  return SessionLocal()


def _oauth_result_page(*, ok: bool):
  from fastapi.responses import HTMLResponse

  status = "connected" if ok else "failed"
  html = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>Connections sign-in</title></head><body style='font-family:"
    "system-ui;background:#12121a;color:#e8e8f0;display:flex;height:100vh;"
    "align-items:center;justify-content:center;margin:0'>"
    f"<p>Sign-in {status}. You can close this window.</p>"
    "<script>try{if(window.opener)window.opener.postMessage("
    f"{{'type':'mobius-connector-oauth','ok':{str(ok).lower()}}},'*');}}"
    "catch(e){}setTimeout(function(){window.close();},900);</script>"
    "</body></html>"
  )
  return HTMLResponse(content=html)
