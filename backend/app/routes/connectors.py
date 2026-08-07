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
from app import connector_oauth as connector_oauth_mod
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
  # Connector identity authenticated by the broker capability. Passing it to
  # token refresh prevents a deleted/recreated numeric id from refreshing a
  # replacement connection's grant.
  generation: str | None = None
  # Non-secret static headers the provider requires alongside auth — currently
  # only Google Cloud's ``x-goog-user-project`` billing/quota project. A tuple
  # of (name, value) pairs keeps the frozen snapshot cleanly copyable through
  # ``dataclasses.replace`` when the OAuth token is attached.
  extra_headers: tuple[tuple[str, str], ...] = ()


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
    # Which sign-in the card should offer: "gcloud" for a Google Cloud endpoint
    # (link-and-code, no console app), else "browser" (the standard popup).
    # Only meaningful when auth_kind == "oauth".
    "oauth_flavor": (
      ("gcloud" if connector_oauth_mod.is_google_cloud_mcp_url(str(row.url))
       else "browser")
      if oauth is not None else None
    ),
    # For a gcloud grant, the quota project it will bill to (None until chosen).
    "user_project": (
      oauth.user_project if oauth is not None else None
    ),
  }


def _oauth_row(db: Session, connector_id: int) -> "models.ConnectorOAuth | None":
  return (
    db.query(models.ConnectorOAuth)
    .filter(models.ConnectorOAuth.connector_id == connector_id)
    .first()
  )


def _oauth_state_fingerprint(oauth: models.ConnectorOAuth) -> tuple:
  """Stable snapshot of every mutable field on one OAuth grant row.

  Used only around remote-call gaps where a same-generation sign-in can replace
  the grant. Ciphertexts are safe identity markers here (never returned):
  Fernet produces a new value on every authorization even when the provider
  happens to reissue the same plaintext credential.
  """
  return (
    oauth.resource,
    oauth.issuer,
    oauth.authorization_endpoint,
    oauth.token_endpoint,
    oauth.registration_endpoint,
    oauth.revocation_endpoint,
    tuple(oauth.scopes_advertised or []),
    oauth.access_token_encrypted,
    oauth.refresh_token_encrypted,
    oauth.access_expires_at,
    tuple(oauth.scopes_granted or []),
    oauth.connected_at,
    oauth.auth_mode,
    oauth.client_id,
    oauth.client_secret_encrypted,
    oauth.user_project,
  )


def _oauth_credential_fingerprint(oauth: models.ConnectorOAuth) -> tuple:
  """Identity of the access/refresh pair used for one remote operation."""
  return (oauth.access_token_encrypted, oauth.refresh_token_encrypted)


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
  # Google Cloud grants must name a quota/billing project on every call. The
  # value is a project id (not a secret), stored on the grant row; attach it as
  # a static header so it rides alongside the OAuth token resolved in the
  # broker. Absent for every other connection.
  extra_headers: tuple[tuple[str, str], ...] = ()
  oauth = (
    db.query(models.ConnectorOAuth)
    .filter(models.ConnectorOAuth.connector_id == connector_id)
    .first()
  )
  if oauth is not None and oauth.auth_mode == "gcloud" and not oauth.user_project:
    # Invariant: a Google Cloud grant with no billing project cannot satisfy a
    # tool call (the quota header is mandatory). Never broker it — status
    # already withholds it from turns; this is the authoritative backstop.
    raise HTTPException(status_code=404, detail="MCP connection unavailable.")
  if (
    oauth is not None
    and oauth.auth_mode == "gcloud"
    and oauth.user_project
  ):
    extra_headers = (("x-goog-user-project", str(oauth.user_project)),)
  return _BrokerSnapshot(
    url=str(row.url),
    auth_header=auth_header,
    secret=secret,
    generation=str(row.capability_id),
    extra_headers=extra_headers,
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
  # Provider-required static headers last, so an incoming request (which cannot
  # name these — they are not in the forwarded allowlist) can never displace or
  # forge them, and they sit beside, not over, the Authorization header.
  headers.update(dict(snapshot.extra_headers))
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
        token = await connector_oauth.usable_access_token(
          db, connector_id, generation=snapshot.generation,
        )
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
    if normalized_secret is None:
      # A server may answer the whole anonymous handshake yet gate the actual
      # tool calls behind sign-in — Google's MCP servers list tools openly and
      # 401 only at call time. The published protected-resource metadata is
      # the authoritative declaration of that gate, so consult it once here
      # at add time; a service with no such document stays a plain keyless
      # add, and rechecks never repeat this walk.
      from app import connector_oauth
      try:
        anon_gate = await connector_oauth.discover(url, "")
      except connector_oauth.OAuthError:
        pass
      else:
        probe = None
        discovery = anon_gate.as_row_fields()
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


def _gcloud_refresh_shared(
  db: Session, connector_id: int, refresh_plaintext: str,
) -> bool:
  """True if another Google connection still holds this exact refresh token.

  Reuse copies a credential, so several connections can share one refresh
  token. Fernet ciphertext is non-deterministic, so equality is decided on the
  decrypted value. Used so sign-out revokes upstream only when it is safe —
  i.e. no sibling would be signed out by Google killing the shared token.
  """
  rows = (
    db.query(models.ConnectorOAuth)
    .filter(
      models.ConnectorOAuth.auth_mode == "gcloud",
      models.ConnectorOAuth.connector_id != connector_id,
      models.ConnectorOAuth.refresh_token_encrypted.isnot(None),
    )
    .all()
  )
  for other in rows:
    try:
      if core.decrypt_oauth(other.refresh_token_encrypted) == refresh_plaintext:
        return True
    except core.ConnectorError:
      continue
  return False


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
  # Bind the remote result to the exact grant that supplied ``token``. OAuth
  # re-authorization does not rotate the connector capability, so generation
  # alone cannot stop an older in-flight probe from overwriting a newer sign-in.
  snapshot = _reopen()
  try:
    current = snapshot.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == generation,
    ).first()
    oauth = _oauth_row(snapshot, connector_id) if current is not None else None
    if (
      oauth is None
      or not oauth.access_token_encrypted
      or core.decrypt_oauth(oauth.access_token_encrypted) != token
    ):
      return
    grant_state = _oauth_state_fingerprint(oauth)
    needs_project = bool(
      oauth.auth_mode == "gcloud" and not oauth.user_project
    )
  finally:
    snapshot.close()

  clear_grant = False
  try:
    probe = await core.handshake(url, "Authorization", token)
    values = {
      "tools_json": probe["tools"],
      "est_tokens": probe["est_tokens"],
      "status": "ok",
      "status_detail": None,
    }
    # Single enforcement point for "a project-less Google grant is not ready":
    # the handshake succeeds without a project, so without this any Recheck
    # would flip it to "ok" and turns would advertise a tool that 404s on
    # every call. Sign-in and reuse rely on this instead of a separate patch.
    if needs_project:
      values["status"] = "oauth_required"
      values["status_detail"] = "Choose a billing project to run queries."
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
  # Reopen after the handshake and apply health + any definitive token clear in
  # one transaction only if the full OAuth row is still the captured grant.
  session = _reopen()
  try:
    current = session.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == generation,
    ).first()
    oauth = _oauth_row(session, connector_id) if current is not None else None
    if oauth is None or _oauth_state_fingerprint(oauth) != grant_state:
      return
    for field_name, value in {
      **values, "last_checked_at": now_naive_utc(),
    }.items():
      setattr(current, field_name, value)
    if clear_grant:
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
    grant_state = _oauth_state_fingerprint(oauth)
    # May refresh the token. The helper releases this request Session before
    # waiting on its single-flight lock or the provider.
    token = await connector_oauth.usable_access_token(
      db, connector_id, generation=generation,
    )
    db.close()
    if token is None:
      # ``None`` can also mean a newer same-generation sign-in superseded an
      # in-flight refresh. Only latch signed-out when the original grant is
      # still current; otherwise preserve the replacement's tokens and health.
      check = _reopen()
      try:
        current = check.query(models.Connector).filter(
          models.Connector.id == connector_id,
          models.Connector.capability_id == generation,
        ).first()
        current_oauth = (
          _oauth_row(check, connector_id) if current is not None else None
        )
        if (
          current_oauth is not None
          and _oauth_state_fingerprint(current_oauth) == grant_state
        ):
          current.status = "oauth_required"
          current.status_detail = "Sign in to finish connecting."
          current.last_checked_at = now_naive_utc()
          check.commit()
      finally:
        check.close()
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
  except connector_oauth.ClientSetupRequired as needs:
    # The provider can't self-register and the owner hasn't supplied a client
    # yet. Not an error — tell the app to collect credentials. The redirect
    # URI comes from the server's authoritative value so the owner pastes the
    # exact string the callback validates.
    return {
      "needs_client_setup": True,
      "issuer": needs.issuer,
      "redirect_uri": connector_oauth.redirect_uri(),
    }
  except core.ConnectorError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc
  return {"authorize_url": authorize_url}


# ── Google-account sign-in (link + pasted code, no console app) ─────────────


def _require_gcloud_oauth(
  db: Session, connector_id: int, generation: str,
) -> tuple[models.Connector, models.ConnectorOAuth]:
  """Load a Google Cloud connector and its grant row, or raise 4xx.

  Shared precondition for the gcloud routes: the connection must exist at this
  generation, carry a sign-in (OAuth) row, and point at a Google Cloud MCP
  endpoint — the only place the link-and-code path applies.
  """
  stored = _get_row(db, connector_id, generation)
  oauth = _oauth_row(db, connector_id)
  if oauth is None:
    raise HTTPException(
      status_code=409, detail="This connection does not use sign-in.",
    )
  if not connector_oauth_mod.is_google_cloud_mcp_url(str(stored.url)):
    raise HTTPException(
      status_code=409,
      detail="Google sign-in applies only to Google Cloud connections.",
    )
  return stored, oauth


async def _resolve_gcloud_project(
  access_token: str, requested: str,
) -> tuple[str, list[dict]]:
  """Validate/auto-select a billing project against the owner's live list.

  Returns (chosen_or_empty, projects). A malformed id is a 400; a well-formed
  id that isn't in the account is a 422 (skipped when the list is unavailable,
  where the shape check is the only guard). Auto-selects the sole project when
  the caller named none. Shared by the sign-in and reuse paths so both behave
  identically.
  """
  chosen = (requested or "").strip()
  if chosen and not connector_oauth_mod.valid_gcloud_project_id(chosen):
    raise HTTPException(status_code=400, detail="That is not a valid project id.")
  projects = await connector_oauth_mod.list_google_projects(access_token)
  project_ids = [p["project_id"] for p in projects]
  if chosen and project_ids and chosen not in project_ids:
    raise HTTPException(
      status_code=422, detail="That project isn't in your Google account.",
    )
  if not chosen and len(project_ids) == 1:
    chosen = project_ids[0]
  return chosen, projects


@router.post("/{connector_id}/oauth/gcloud/start")
async def oauth_gcloud_start(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Begin Google-account sign-in: return the consent link and sealed state.

  Unlike the browser flow there is no redirect back to this instance — Google
  shows the owner a code to copy. The PKCE verifier and connection binding
  travel in the sealed ``state`` the app hands back to ``complete``, so nothing
  is held server-side between the two calls (restart-safe).
  """
  _require_gcloud_oauth(db, connector_id, generation)
  client_id, _secret = connector_oauth_mod.gcloud_client_identity()
  verifier, challenge = connector_oauth_mod.generate_pkce()
  state = connector_oauth_mod.seal_flow({
    "connector_id": connector_id,
    "generation": generation,
    "verifier": verifier,
    "client_id": client_id,
    "mode": "gcloud",
  })
  authorize_url = connector_oauth_mod.gcloud_authorization_url(
    client_id, challenge, state,
  )
  return {"authorize_url": authorize_url, "state": state}


class GcloudCompleteBody(BaseModel):
  state: str = Field(min_length=1, max_length=8192)
  # Write-only like other credential inputs: typed object, validated in-route
  # so a pydantic 422 can never echo the authorization code back.
  code: object = ""
  project_id: str = Field(default="", max_length=256)


@router.post("/{connector_id}/oauth/gcloud/complete")
async def oauth_gcloud_complete(
  connector_id: int,
  body: GcloudCompleteBody,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Finish Google-account sign-in from the pasted code.

  Exchanges the code with the Cloud SDK client, seals the tokens onto the grant
  through the same storage every connection uses, records the per-connection
  client so refresh needs no console app, and selects the billing project when
  it is unambiguous (or the caller named one). Returns the updated card plus
  the project list so the app can prompt when a choice is needed.
  """
  stored, oauth = _require_gcloud_oauth(db, connector_id, generation)
  flow = connector_oauth_mod.open_flow(body.state)
  if (
    flow is None
    or flow.get("mode") != "gcloud"
    or int(flow.get("connector_id") or 0) != connector_id
    or str(flow.get("generation") or "") != generation
  ):
    raise HTTPException(
      status_code=400, detail="This sign-in expired. Start it again.",
    )
  code = body.code
  if not isinstance(code, str) or not code.strip():
    raise HTTPException(status_code=400, detail="Paste the code from Google.")

  client_id = str(flow["client_id"])
  _cid, client_secret = connector_oauth_mod.gcloud_client_identity()
  url = str(stored.url)
  db.close()
  try:
    tokens = await connector_oauth_mod.gcloud_exchange_code(
      client_id, client_secret, code, str(flow["verifier"]),
    )
  except core.ConnectorError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc

  access_token = str(tokens["access_token"])
  # Resolve the billing project: an explicit choice wins; otherwise pick the
  # sole active project automatically, else leave it for the app to ask.
  chosen, projects = await _resolve_gcloud_project(access_token, body.project_id)

  discovery = connector_oauth_mod.gcloud_discovery(url)
  db = _reopen()
  try:
    row = db.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == generation,
    ).first()
    oauth = _oauth_row(db, connector_id)
    if row is None or oauth is None:
      raise HTTPException(
        status_code=404,
        detail="The connection changed during sign-in.",
      )
    # Stamp the Google endpoints so refresh runs against them, then seal the
    # per-connection client and tokens through the shared writer.
    for field_name, value in discovery.as_row_fields().items():
      setattr(oauth, field_name, value)
    oauth.auth_mode = "gcloud"
    oauth.client_id = client_id
    oauth.client_secret_encrypted = core.encrypt_oauth(client_secret)
    # `or None` clears any project a prior sign-in left, so re-signing this
    # connection into a DIFFERENT account can never keep the old project (which
    # would bypass the project-less withholding and mis-bill the new account).
    oauth.user_project = chosen or None
    connector_oauth_mod._store_authorization_tokens(oauth, tokens)
    oauth.connected_at = now_naive_utc()
    row.status = "ok"
    row.status_detail = None
    row_generation = str(row.capability_id)
    db.commit()
  finally:
    db.close()

  # Populate tools + cost with the fresh grant, and let the probe decide the
  # final status: a project-less gcloud grant is withheld there (a single
  # enforcement point that also survives a later Recheck).
  await _probe_signed_in(url, connector_id, row_generation, access_token, "ok")

  session = _reopen()
  try:
    row = session.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == row_generation,
    ).first()
    public = _public(row, _oauth_row(session, connector_id)) if row else None
  finally:
    session.close()
  if public is None:
    raise HTTPException(status_code=404, detail="Connection not found.")
  return {
    "connection": public,
    "projects": projects,
    "needs_project": not chosen,
  }


@router.get("/{connector_id}/oauth/gcloud/projects")
async def oauth_gcloud_list_projects(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """List the owner's Google Cloud projects for a signed-in connection.

  Read-only: uses the stored token (refreshing if needed) to fetch the live
  project list so the app can offer a picker when changing the billing project,
  instead of asking the owner to type an id. Returns {projects, current}; an
  empty list means the lookup was unavailable and the app falls back to entry.
  """
  _stored, oauth = _require_gcloud_oauth(db, connector_id, generation)
  if not oauth.access_token_encrypted:
    raise HTTPException(status_code=409, detail="Sign in with Google first.")
  current = oauth.user_project
  token = await connector_oauth_mod.usable_access_token(
    db, connector_id, generation=generation,
  )
  if token is None:
    raise HTTPException(status_code=409, detail="Sign in with Google again.")
  # Project lookup is a remote call. Release the request's checked-out DB
  # connection first; no database state is needed to build this read-only
  # response, and the generation was already authenticated above.
  db.close()
  projects = await connector_oauth_mod.list_google_projects(token)
  return {"projects": projects, "current": current}


class GcloudProjectBody(BaseModel):
  project_id: str = Field(min_length=1, max_length=256)


@router.post("/{connector_id}/oauth/gcloud/project")
async def oauth_gcloud_set_project(
  connector_id: int,
  body: GcloudProjectBody,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Set (or change) the billing project for a signed-in Google connection.

  Validated against the owner's live project list using the stored token, so a
  typo can't silently break every later query. Usable before first use (when
  sign-in found several projects) or to move an existing connection later.
  """
  _stored, oauth = _require_gcloud_oauth(db, connector_id, generation)
  if not oauth.access_token_encrypted:
    raise HTTPException(status_code=409, detail="Sign in with Google first.")
  chosen = body.project_id.strip()
  if not connector_oauth_mod.valid_gcloud_project_id(chosen):
    raise HTTPException(status_code=400, detail="That is not a valid project id.")
  token = await connector_oauth_mod.usable_access_token(
    db, connector_id, generation=generation,
  )
  if token is None:
    raise HTTPException(status_code=409, detail="Sign in with Google again.")
  # Tie the returned plaintext to the currently stored grant before releasing
  # the Session. A same-generation re-sign between token resolution and this
  # snapshot must not let account A's project list update account B.
  db.expire_all()
  row = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  ).first()
  oauth = _oauth_row(db, connector_id) if row is not None else None
  if (
    oauth is None
    or not oauth.access_token_encrypted
    or core.decrypt_oauth(oauth.access_token_encrypted) != token
  ):
    raise HTTPException(
      status_code=409,
      detail="The Google sign-in changed before the project could be set.",
    )
  credential_fingerprint = _oauth_credential_fingerprint(oauth)
  # Do not hold the request Session across Cloud Resource Manager, then open a
  # second Session for the write. That pattern can exhaust Postgres's bounded
  # pool with concurrent requests. The fresh session below generation-guards
  # the only mutation after the network result is in hand.
  db.close()
  projects = await connector_oauth_mod.list_google_projects(token)
  project_ids = [p["project_id"] for p in projects]
  if project_ids and chosen not in project_ids:
    raise HTTPException(
      status_code=422, detail="That project isn't in your Google account.",
    )
  session = _reopen()
  try:
    row = session.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == generation,
    ).first()
    oauth = _oauth_row(session, connector_id) if row is not None else None
    if oauth is None or row is None:
      raise HTTPException(status_code=404, detail="Connection not found.")
    if _oauth_credential_fingerprint(oauth) != credential_fingerprint:
      raise HTTPException(
        status_code=409,
        detail="The Google sign-in changed before the project could be set.",
      )
    oauth.user_project = chosen
    # A verified token plus a valid project makes the connection usable again:
    # clear the "choose a project" withholding so it rejoins turns. (The token
    # was just confirmed live via usable_access_token above.)
    if row.status == "oauth_required":
      row.status = "ok"
    row.status_detail = None
    session.commit()
    public = _public(row, _oauth_row(session, connector_id))
  finally:
    session.close()
  return {"connection": public, "projects": projects}


@router.get("/{connector_id}/oauth/gcloud/reusable")
async def oauth_gcloud_reusable(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """List the owner's other signed-in Google connections this one can reuse.

  One Google sign-in already grants cloud-platform access to every Google Cloud
  service, so a second Google Cloud connection needs no fresh consent — only a
  source to adopt the credential from. The source generation is part of the
  choice so a stale tab cannot silently target a deleted/reused SQLite id.
  """
  _require_gcloud_oauth(db, connector_id, generation)
  rows = (
    db.query(models.ConnectorOAuth)
    .filter(
      models.ConnectorOAuth.auth_mode == "gcloud",
      models.ConnectorOAuth.connector_id != connector_id,
      models.ConnectorOAuth.access_token_encrypted.isnot(None),
    )
    .all()
  )
  sources = []
  for source_oauth in rows:
    source = (
      db.query(models.Connector)
      .filter(models.Connector.id == source_oauth.connector_id)
      .first()
    )
    if source is not None:
      sources.append({
        "connector_id": source.id,
        "generation": source.capability_id,
        "name": source.name,
      })
  return {"sources": sources}


class GcloudReuseBody(BaseModel):
  source_connector_id: int
  source_generation: str = Field(min_length=1, max_length=128)
  project_id: str = Field(default="", max_length=256)


@router.post("/{connector_id}/oauth/gcloud/reuse")
async def oauth_gcloud_reuse(
  connector_id: int,
  body: GcloudReuseBody,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Adopt an existing Google sign-in for this connection — no re-approval.

  Copies the sealed Google credential from another of the owner's signed-in
  Google Cloud connections (same account, same cloud-platform scope) onto this
  one, then resolves the billing project. Google does not rotate installed-app
  refresh tokens on use, so the shared credential keeps working for both
  connections independently; each refreshes its own access token. The billing
  project stays per-connection.
  """
  stored, target_oauth = _require_gcloud_oauth(db, connector_id, generation)
  target_state = _oauth_state_fingerprint(target_oauth)
  if body.source_connector_id == connector_id:
    raise HTTPException(status_code=400, detail="Pick a different connection.")
  source_row = db.query(models.Connector).filter(
    models.Connector.id == body.source_connector_id,
    models.Connector.capability_id == body.source_generation,
  ).first()
  source = (
    _oauth_row(db, body.source_connector_id)
    if source_row is not None else None
  )
  if (
    source is None
    or source.auth_mode != "gcloud"
    or not source.access_token_encrypted
  ):
    raise HTTPException(
      status_code=409, detail="That Google sign-in isn't available to reuse.",
    )
  # Freshen the source token (if near expiry) so the adopted access token is
  # live, then snapshot the credential fields as plain values before releasing
  # the session for the network calls.
  fresh = await connector_oauth_mod.usable_access_token(
    db,
    body.source_connector_id,
    generation=body.source_generation,
  )
  # Re-query BOTH identities after token resolution. The helper may await a
  # refresh; during that gap the source can be disconnected/recreated or the
  # target can complete a fresh same-generation sign-in.
  db.expire_all()
  source_row = db.query(models.Connector).filter(
    models.Connector.id == body.source_connector_id,
    models.Connector.capability_id == body.source_generation,
  ).first()
  source = (
    _oauth_row(db, body.source_connector_id)
    if source_row is not None else None
  )
  target_row = db.query(models.Connector).filter(
    models.Connector.id == connector_id,
    models.Connector.capability_id == generation,
  ).first()
  live_target = _oauth_row(db, connector_id) if target_row is not None else None
  if (
    live_target is None
    or _oauth_state_fingerprint(live_target) != target_state
  ):
    raise HTTPException(
      status_code=409,
      detail="This connection changed before the sign-in could be reused.",
    )
  if (
    fresh is None
    or source is None
    or not source.access_token_encrypted
    or core.decrypt_oauth(source.access_token_encrypted) != fresh
  ):
    raise HTTPException(
      status_code=409, detail="That Google sign-in isn't available to reuse.",
    )
  cred = {
    "client_id": source.client_id,
    "client_secret_encrypted": source.client_secret_encrypted,
    "access_token_encrypted": source.access_token_encrypted,
    "refresh_token_encrypted": source.refresh_token_encrypted,
    "access_expires_at": source.access_expires_at,
    "scopes_granted": list(source.scopes_granted or []),
  }
  # Compare both sealed credentials byte-for-byte after the network gap. A
  # disconnect, re-sign, or concurrent refresh replaces at least one of them;
  # copying only the exact pair used for project lookup keeps the snapshot
  # internally consistent.
  credential_fingerprint = _oauth_credential_fingerprint(source)
  access_token = fresh
  url = str(stored.url)
  db.close()

  chosen, projects = await _resolve_gcloud_project(access_token, body.project_id)
  discovery = connector_oauth_mod.gcloud_discovery(url)

  session = _reopen()
  try:
    # Revalidate the SOURCE after the project-list network gap as well as the
    # target. Without this, a concurrent source disconnect can revoke the sole
    # token and reuse will still copy its stale snapshot; a deleted/reused id can
    # likewise resolve to a different Google account in a stale tab.
    live_source_row = session.query(models.Connector).filter(
      models.Connector.id == body.source_connector_id,
      models.Connector.capability_id == body.source_generation,
    ).first()
    live_source = (
      _oauth_row(session, body.source_connector_id)
      if live_source_row is not None else None
    )
    if (
      live_source is None
      or live_source.auth_mode != "gcloud"
      or not live_source.access_token_encrypted
      or _oauth_credential_fingerprint(live_source) != credential_fingerprint
    ):
      raise HTTPException(
        status_code=409,
        detail="That Google sign-in changed before it could be reused.",
      )
    row = session.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == generation,
    ).first()
    target = _oauth_row(session, connector_id) if row is not None else None
    if row is None or target is None:
      raise HTTPException(status_code=404, detail="Connection not found.")
    if _oauth_state_fingerprint(target) != target_state:
      raise HTTPException(
        status_code=409,
        detail="This connection changed before the sign-in could be reused.",
      )
    for field_name, value in discovery.as_row_fields().items():
      setattr(target, field_name, value)
    target.auth_mode = "gcloud"
    target.client_id = cred["client_id"]
    target.client_secret_encrypted = cred["client_secret_encrypted"]
    target.access_token_encrypted = cred["access_token_encrypted"]
    target.refresh_token_encrypted = cred["refresh_token_encrypted"]
    target.access_expires_at = cred["access_expires_at"]
    target.scopes_granted = cred["scopes_granted"]
    # `or None` clears a project left by a prior sign-in, so adopting a
    # different account can't keep the old project and bypass the withhold.
    target.user_project = chosen or None
    target.connected_at = now_naive_utc()
    row.status = "ok"
    row.status_detail = None
    row_generation = str(row.capability_id)
    session.commit()
  finally:
    session.close()

  # The probe decides the final status; a project-less grant is withheld there.
  await _probe_signed_in(url, connector_id, row_generation, access_token, "ok")

  final = _reopen()
  try:
    row = final.query(models.Connector).filter(
      models.Connector.id == connector_id,
      models.Connector.capability_id == row_generation,
    ).first()
    public = _public(row, _oauth_row(final, connector_id)) if row else None
  finally:
    final.close()
  if public is None:
    raise HTTPException(status_code=404, detail="Connection not found.")
  return {"connection": public, "projects": projects, "needs_project": not chosen}


class OAuthClientBody(BaseModel):
  client_id: str = Field(min_length=1, max_length=512)
  # Write-only, like ConnectorCreate.auth_value: typed `object` and validated
  # in-route so a pydantic 422 can never echo the secret in its error body.
  client_secret: object = ""


@router.post("/{connector_id}/oauth/client")
async def oauth_set_client(
  connector_id: int,
  body: OAuthClientBody,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Store the owner's own OAuth client for this connection's issuer.

  The issuer is taken from the connection's discovered row, never from the
  request, so one connector cannot name an issuer to borrow another's
  credentials. Keyed by issuer (shared across connections on the same
  provider) via an upsert, so re-saving a rotated secret updates rather than
  conflicts. The secret is sealed and never returned or logged.
  """
  _get_row(db, connector_id, generation)
  oauth = _oauth_row(db, connector_id)
  if oauth is None:
    raise HTTPException(
      status_code=409, detail="This connection does not use sign-in.",
    )
  client_id = body.client_id.strip()
  if not client_id:
    raise HTTPException(status_code=400, detail="Enter the client ID.")
  secret = body.client_secret
  if not isinstance(secret, str):
    raise HTTPException(status_code=400, detail="The client secret must be text.")
  secret = secret.strip()
  if len(secret) > 4096:
    raise HTTPException(status_code=400, detail="The client secret is too long.")

  issuer = str(oauth.issuer)
  reg = (
    db.query(models.OAuthClientRegistration)
    .filter(models.OAuthClientRegistration.issuer == issuer)
    .first()
  )
  if reg is None:
    reg = models.OAuthClientRegistration(issuer=issuer, mode="byo", client_id=client_id)
    db.add(reg)
  reg.mode = "byo"
  reg.client_id = client_id
  reg.client_secret_encrypted = core.encrypt_oauth(secret) if secret else None
  db.commit()
  log.info(
    "OAuth client set (issuer=%s) for connection %s", issuer, oauth.connector_id,
  )
  return _public(_get_row(db, connector_id, generation), _oauth_row(db, connector_id))


@router.delete("/{connector_id}/oauth/client")
async def oauth_clear_client(
  connector_id: int,
  generation: str = Depends(_require_generation),
  _owner: models.Owner = Depends(get_owner_or_app_with_connections_manage),
  db: Session = Depends(get_db),
):
  """Remove the owner-supplied client for this connection's issuer.

  Issuer-scoped: this removes the shared client setup for every connection on
  the same provider. Existing grants keep working until expiry or sign-out;
  the next refresh then needs a re-setup. Automatically registered clients are
  not owner-supplied and cannot be removed here.
  """
  _get_row(db, connector_id, generation)
  oauth = _oauth_row(db, connector_id)
  if oauth is None:
    raise HTTPException(
      status_code=409, detail="This connection does not use sign-in.",
    )
  removed = (
    db.query(models.OAuthClientRegistration)
    .filter(
      models.OAuthClientRegistration.issuer == str(oauth.issuer),
      models.OAuthClientRegistration.mode == "byo",
    )
    .delete(synchronize_session=False)
  )
  db.commit()
  return {"ok": True, "removed": bool(removed)}


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
    connector_oauth._store_authorization_tokens(oauth, tokens)
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
  # A gcloud credential may be SHARED with sibling connections that adopted it
  # (reuse), and Google keeps a revoked refresh token dead for every holder — so
  # revoking a shared token would silently sign the siblings out too. Skip the
  # upstream revoke ONLY when a sibling still holds this exact token; an unshared
  # grant revokes normally, so a lone connection's sign-out truly ends access.
  if (
    oauth.auth_mode == "gcloud"
    and refresh_encrypted
    and _gcloud_refresh_shared(
      db, connector_id, core.decrypt_oauth(refresh_encrypted),
    )
  ):
    revocation_endpoint = None
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
      await connector_oauth.oauth_json_request(
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
