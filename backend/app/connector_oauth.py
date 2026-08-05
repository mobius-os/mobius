"""OAuth authorization for owner-managed MCP connections (spec 2026-07-28).

This module owns the authorization lifecycle that sits beside the static-key
path in ``connectors.py``: discovery from a 401 challenge, this instance's
client identity at each authorization server (CIMD, DCR fallback), the
authorization URL, code exchange, and refresh. Every network call goes
through ``connectors.pinned_json_request`` so the SSRF-pinning, redirect
refusal, and bounded reads are identical to the MCP probe.

Custody invariant: tokens and client secrets are Fernet-sealed
(``connectors.encrypt_oauth``) and never leave the server. The registry
exposes only ``signed_in`` and granted scopes.

Frugality: discovery results are cached on the connector row, so sign-in and
every later turn skip the well-known walk. Client registration is cached per
issuer and reused across connectors that share an authorization server.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode, urlparse, urlunparse

from app import connectors as core
from app.config import get_settings
from app.timeutil import now_naive_utc

# The scope that lets a client keep a refresh token past the browser session.
# We request it only when advertised; MCP servers SHOULD NOT gate on it.
_OFFLINE_SCOPE = "offline_access"
_MAX_SCOPES_CHARS = 4096
# Refresh this far before the access token actually expires, so a token handed
# to a provider turn stays valid for the whole exchange.
_REFRESH_SKEW = timedelta(seconds=120)
_DEFAULT_ACCESS_TTL = timedelta(hours=1)
# Cap a provider-supplied expires_in so a malformed/huge/inf/NaN value can
# never overflow int()/timedelta(); anything out of range falls back to the
# default TTL and the next refresh corrects it.
_MAX_ACCESS_TTL_SECONDS = 366 * 24 * 60 * 60

# Per-connector async refresh locks. One API worker → an asyncio.Lock is a
# sufficient single-flight guard so two concurrent turns never double-spend a
# rotating refresh token.
_refresh_locks: dict[int, asyncio.Lock] = {}


def _refresh_lock(connector_id: int) -> asyncio.Lock:
  lock = _refresh_locks.get(connector_id)
  if lock is None:
    lock = asyncio.Lock()
    _refresh_locks[connector_id] = lock
  return lock


class OAuthError(core.ConnectorError):
  """A sign-in step failed in a way worth showing the owner."""


@dataclass(frozen=True)
class Discovery:
  """Everything the sign-in and token flows need, cached from one 401."""

  resource: str  # canonical MCP URL (RFC 8707), the connector's own url
  issuer: str
  authorization_endpoint: str
  token_endpoint: str
  registration_endpoint: str | None = None
  revocation_endpoint: str | None = None
  scopes: list[str] = field(default_factory=list)
  cimd_supported: bool = False
  token_auth_none: bool = False  # AS accepts public clients (token_endpoint_auth "none")

  def as_row_fields(self) -> dict:
    return {
      "resource": self.resource,
      "issuer": self.issuer,
      "authorization_endpoint": self.authorization_endpoint,
      "token_endpoint": self.token_endpoint,
      "registration_endpoint": self.registration_endpoint,
      "revocation_endpoint": self.revocation_endpoint,
      "scopes_advertised": list(self.scopes),
    }

  @classmethod
  def from_row(cls, oauth_row) -> "Discovery":
    return cls(
      resource=oauth_row.resource,
      issuer=oauth_row.issuer,
      authorization_endpoint=oauth_row.authorization_endpoint,
      token_endpoint=oauth_row.token_endpoint,
      registration_endpoint=oauth_row.registration_endpoint,
      revocation_endpoint=oauth_row.revocation_endpoint,
      scopes=list(oauth_row.scopes_advertised or []),
      # These two only steer registration, not refresh; the stored client row
      # is authoritative once a connection exists.
      cimd_supported=False,
      token_auth_none=False,
    )


# ── canonical values ──────────────────────────────────────────────────────


def canonical_resource(url: str) -> str:
  """RFC 8707 canonical resource URI: lower scheme/host, no fragment/query,
  no trailing slash on a bare path. The exact string sent as ``resource=``."""
  parsed = urlparse(url)
  path = parsed.path.rstrip("/") if parsed.path not in ("", "/") else ""
  return urlunparse((
    parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", "",
  ))


def _www_authenticate_param(header: str, key: str) -> str | None:
  match = re.search(rf'{key}="([^"]+)"', header or "")
  return match.group(1) if match else None


def _wellknown_candidates(url: str, kind: str) -> list[str]:
  """RFC 9728 / 8414 well-known URL candidates in spec priority order.

  ``kind`` is 'oauth-protected-resource' or 'oauth-authorization-server'.
  Path-insertion form first (…/.well-known/<kind>/<path>), then root, then —
  for the authorization server — the OIDC appended form.
  """
  parsed = urlparse(url)
  root = f"{parsed.scheme}://{parsed.netloc}"
  path = parsed.path.rstrip("/")
  candidates: list[str] = []
  if path:
    candidates.append(f"{root}/.well-known/{kind}{path}")
  candidates.append(f"{root}/.well-known/{kind}")
  if kind == "oauth-authorization-server":
    # OIDC discovery variants.
    if path:
      candidates.append(f"{root}/.well-known/openid-configuration{path}")
      candidates.append(f"{root}{path}/.well-known/openid-configuration")
    candidates.append(f"{root}/.well-known/openid-configuration")
  # De-dup preserving order.
  seen: set[str] = set()
  ordered: list[str] = []
  for candidate in candidates:
    if candidate not in seen:
      seen.add(candidate)
      ordered.append(candidate)
  return ordered


async def _fetch_json(url: str) -> dict | None:
  try:
    status, body, _ = await core.pinned_json_request("GET", url, timeout_seconds=10.0)
  except core.ConnectorError:
    return None
  return body if status == 200 and body else None


# ── discovery ─────────────────────────────────────────────────────────────


async def discover(resource_url: str, www_authenticate: str) -> Discovery:
  """Run RFC 9728 → 8414/OIDC discovery from a 401 challenge.

  Raises OAuthError if the endpoint is 401 but exposes no usable
  authorization server (i.e. a genuinely bad static key, not an OAuth gate),
  or if the AS omits PKCE S256 support (spec: the client MUST refuse).
  """
  resource = canonical_resource(resource_url)

  # 1. Protected-resource metadata: prefer the challenge's pointer, else walk.
  prm_url = _www_authenticate_param(www_authenticate, "resource_metadata")
  prm = await _fetch_json(prm_url) if prm_url else None
  if prm is None:
    for candidate in _wellknown_candidates(resource_url, "oauth-protected-resource"):
      prm = await _fetch_json(candidate)
      if prm is not None:
        break
  if prm is None:
    raise OAuthError("The service rejected the request and offers no sign-in.")

  servers = prm.get("authorization_servers")
  if not isinstance(servers, list) or not servers:
    raise OAuthError("The service's sign-in metadata names no authorization server.")
  issuer = str(servers[0]).strip()
  if not issuer.startswith("https://"):
    raise OAuthError("The authorization server address is not valid HTTPS.")

  # 2. Authorization-server metadata (RFC 8414 / OIDC), issuer must round-trip.
  meta: dict | None = None
  for candidate in _wellknown_candidates(issuer, "oauth-authorization-server"):
    fetched = await _fetch_json(candidate)
    if fetched and str(fetched.get("issuer") or "") == issuer:
      meta = fetched
      break
  if meta is None:
    raise OAuthError("Could not read the authorization server's metadata.")

  authorization_endpoint = str(meta.get("authorization_endpoint") or "")
  token_endpoint = str(meta.get("token_endpoint") or "")
  if not authorization_endpoint or not token_endpoint:
    raise OAuthError("The authorization server is missing required endpoints.")
  # The authorization endpoint is opened in the owner's browser, so require
  # plain HTTPS — a hostile discovery document must not steer the consent
  # navigation to a javascript:/data:/http: URL. (The token endpoint is
  # fetched server-side through the SSRF-pinned transport, which enforces its
  # own HTTPS pinning.)
  if not authorization_endpoint.lower().startswith("https://"):
    raise OAuthError("The authorization server's sign-in address is not valid HTTPS.")

  pkce = meta.get("code_challenge_methods_supported")
  if not isinstance(pkce, list) or "S256" not in pkce:
    # Spec: the client MUST refuse when S256 PKCE is not advertised.
    raise OAuthError("The authorization server does not support secure sign-in (PKCE S256).")

  auth_methods = meta.get("token_endpoint_auth_methods_supported")
  token_auth_none = isinstance(auth_methods, list) and "none" in auth_methods

  scopes = prm.get("scopes_supported") or meta.get("scopes_supported") or []
  scopes = [str(s) for s in scopes if isinstance(s, str)][:64]

  return Discovery(
    resource=resource,
    issuer=issuer,
    authorization_endpoint=authorization_endpoint,
    token_endpoint=token_endpoint,
    registration_endpoint=(str(meta["registration_endpoint"])
                           if meta.get("registration_endpoint") else None),
    revocation_endpoint=(str(meta["revocation_endpoint"])
                         if meta.get("revocation_endpoint") else None),
    scopes=scopes,
    cimd_supported=bool(meta.get("client_id_metadata_document_supported")),
    token_auth_none=token_auth_none,
  )


# ── this instance's client identity ───────────────────────────────────────


def client_metadata_url() -> str:
  """The CIMD client_id: an HTTPS URL on this instance serving its own
  client-metadata document. Must byte-for-byte equal the ``client_id`` inside
  that document."""
  origin = get_settings().frontend_origin.rstrip("/")
  return f"{origin}/api/connectors/oauth/client-metadata.json"


def redirect_uri() -> str:
  origin = get_settings().frontend_origin.rstrip("/")
  return f"{origin}/api/connectors/oauth/callback"


def client_metadata_document() -> dict:
  """The static CIMD document (draft-ietf-oauth-client-id-metadata-document)."""
  url = client_metadata_url()
  return {
    "client_id": url,
    "client_name": "Möbius Connections",
    "client_uri": get_settings().frontend_origin.rstrip("/"),
    "redirect_uris": [redirect_uri()],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "application_type": "web",
  }


async def ensure_client(db, discovery: Discovery) -> tuple[str, str | None]:
  """Return (client_id, client_secret|None) for this instance at an issuer.

  CIMD when the AS advertises it (no registration, no stored secret, portable
  — the spec's preferred path and where a self-hosted instance shines). Else
  RFC 7591 dynamic registration, cached per issuer and reused across every
  connector that shares this authorization server.
  """
  from app import models

  if discovery.cimd_supported and discovery.token_auth_none:
    return client_metadata_url(), None

  existing = (
    db.query(models.OAuthClientRegistration)
    .filter(models.OAuthClientRegistration.issuer == discovery.issuer)
    .first()
  )
  if existing is not None:
    secret = (core.decrypt_oauth(existing.client_secret_encrypted)
              if existing.client_secret_encrypted else None)
    return existing.client_id, secret

  if not discovery.registration_endpoint:
    raise OAuthError("This service needs a pre-registered app; sign-in isn't available yet.")

  status, body, _ = await core.pinned_json_request(
    "POST",
    discovery.registration_endpoint,
    json_body={
      "client_name": "Möbius Connections",
      "redirect_uris": [redirect_uri()],
      "token_endpoint_auth_method": "none",
      "grant_types": ["authorization_code", "refresh_token"],
      "response_types": ["code"],
      "application_type": "web",
      "client_uri": get_settings().frontend_origin.rstrip("/"),
    },
    timeout_seconds=10.0,
  )
  if status not in (200, 201) or not body.get("client_id"):
    raise OAuthError("The authorization server refused to register this instance.")

  client_id = str(body["client_id"])
  client_secret = body.get("client_secret")
  row = models.OAuthClientRegistration(
    issuer=discovery.issuer,
    mode="dcr",
    client_id=client_id,
    client_secret_encrypted=(core.encrypt_oauth(str(client_secret))
                             if client_secret else None),
  )
  db.add(row)
  db.commit()
  return client_id, (str(client_secret) if client_secret else None)


# ── authorization URL + token exchange ────────────────────────────────────


def generate_pkce() -> tuple[str, str]:
  import base64
  import hashlib

  verifier = secrets.token_urlsafe(64)
  challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()
  ).rstrip(b"=").decode()
  return verifier, challenge


# In-flight consent state travels as the ``state`` parameter — Fernet-sealed,
# so the PKCE verifier inside it stays confidential even though the provider
# echoes state back in a URL, and no cookie or server-side record is needed
# (stateless, restart-safe). Fernet's timestamp gives the freshness bound.
_FLOW_TTL_SECONDS = 15 * 60


def _flow_fernet():
  import base64
  import hashlib

  from cryptography.fernet import Fernet

  material = f"mobius-connector-oauth-flow-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def seal_flow(payload: dict) -> str:
  import json

  return _flow_fernet().encrypt(
    json.dumps(payload, separators=(",", ":")).encode()
  ).decode()


def open_flow(state: str) -> dict | None:
  import json

  from cryptography.fernet import InvalidToken

  try:
    raw = _flow_fernet().decrypt(state.encode(), ttl=_FLOW_TTL_SECONDS)
    data = json.loads(raw)
    return data if isinstance(data, dict) else None
  except (InvalidToken, ValueError):
    return None


def _requested_scopes(discovery: Discovery) -> str:
  scopes = list(discovery.scopes)
  if _OFFLINE_SCOPE in scopes:
    # Keep it; helps public clients keep a refresh token.
    pass
  joined = " ".join(scopes)
  return joined[:_MAX_SCOPES_CHARS]


def authorization_url(
  discovery: Discovery, client_id: str, challenge: str, state: str,
) -> str:
  params = {
    "response_type": "code",
    "client_id": client_id,
    "redirect_uri": redirect_uri(),
    "state": state,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "resource": discovery.resource,
  }
  scope = _requested_scopes(discovery)
  if scope:
    params["scope"] = scope
  sep = "&" if "?" in discovery.authorization_endpoint else "?"
  return discovery.authorization_endpoint + sep + urlencode(params)


async def exchange_code(
  discovery: Discovery,
  client_id: str,
  client_secret: str | None,
  code: str,
  verifier: str,
) -> dict:
  """Authorization-code exchange. Returns the raw token response dict."""
  form = {
    "grant_type": "authorization_code",
    "code": code,
    "redirect_uri": redirect_uri(),
    "client_id": client_id,
    "code_verifier": verifier,
    "resource": discovery.resource,
  }
  if client_secret:
    form["client_secret"] = client_secret
  status, body, _ = await core.pinned_json_request(
    "POST", discovery.token_endpoint, form=form, timeout_seconds=15.0,
  )
  if status != 200 or not body.get("access_token"):
    raise OAuthError("The sign-in could not be completed.")
  return body


def _client_for_refresh(db, oauth_row):
  """Resolve stored client identity for an existing connection's refresh.

  A signed-in connection already has its client identity: either a stored DCR
  registration keyed by issuer, or (implicitly) this instance's CIMD URL.
  """
  from app import models

  reg = (
    db.query(models.OAuthClientRegistration)
    .filter(models.OAuthClientRegistration.issuer == oauth_row.issuer)
    .first()
  )
  if reg is not None:
    secret = (core.decrypt_oauth(reg.client_secret_encrypted)
              if reg.client_secret_encrypted else None)
    return reg.client_id, secret
  return client_metadata_url(), None


def _store_tokens(oauth_row, tokens: dict) -> None:
  """Seal a token response onto the row (write-only; caller commits)."""
  oauth_row.access_token_encrypted = core.encrypt_oauth(str(tokens["access_token"]))
  if tokens.get("refresh_token"):
    # Public clients get rotating refresh tokens; always store the newest.
    oauth_row.refresh_token_encrypted = core.encrypt_oauth(str(tokens["refresh_token"]))
  expires_in = tokens.get("expires_in")
  ttl = (
    timedelta(seconds=int(expires_in))
    if isinstance(expires_in, (int, float))
    and 0 < expires_in <= _MAX_ACCESS_TTL_SECONDS
    else _DEFAULT_ACCESS_TTL
  )
  oauth_row.access_expires_at = now_naive_utc() + ttl
  granted = tokens.get("scope")
  if isinstance(granted, str) and granted.strip():
    oauth_row.scopes_granted = granted.split()


async def usable_access_token(db, connector_id: int) -> str | None:
  """Return a valid access token for a signed-in OAuth connector, or None.

  Refresh-before-attach: if the stored token is near expiry it is refreshed
  (single-flighted per connector) BEFORE it is handed to a provider turn, so
  the broker never has to retry a mid-request 401 with a non-replayable body.
  A definitively revoked grant clears the tokens and latches the connector to
  ``oauth_required`` so the UI offers sign-in and turns stop including it. A
  transient refresh failure keeps the last token and lets this turn proceed on
  whatever validity remains (the provider will 401 if truly expired).
  """
  from app import models

  oauth_row = (
    db.query(models.ConnectorOAuth)
    .filter(models.ConnectorOAuth.connector_id == connector_id)
    .first()
  )
  if oauth_row is None or not oauth_row.access_token_encrypted:
    return None

  fresh_enough = (
    oauth_row.access_expires_at is not None
    and oauth_row.access_expires_at - now_naive_utc() > _REFRESH_SKEW
  )
  if fresh_enough:
    return core.decrypt_oauth(oauth_row.access_token_encrypted)

  if not oauth_row.refresh_token_encrypted:
    # No way to refresh and the access token is at/near expiry — hand back
    # what we have; a still-valid token works, an expired one 401s upstream.
    return core.decrypt_oauth(oauth_row.access_token_encrypted)

  async with _refresh_lock(connector_id):
    # Re-read under the lock: a concurrent turn may have just refreshed.
    db.refresh(oauth_row)
    if (
      oauth_row.access_expires_at is not None
      and oauth_row.access_expires_at - now_naive_utc() > _REFRESH_SKEW
    ):
      return core.decrypt_oauth(oauth_row.access_token_encrypted)

    discovery = Discovery.from_row(oauth_row)
    client_id, client_secret = _client_for_refresh(db, oauth_row)
    refresh_token = core.decrypt_oauth(oauth_row.refresh_token_encrypted)
    try:
      tokens = await refresh_tokens(
        discovery, client_id, client_secret, refresh_token,
      )
    except OAuthError:
      # Definitive: the owner revoked access. Latch signed-out.
      oauth_row.access_token_encrypted = None
      oauth_row.refresh_token_encrypted = None
      oauth_row.access_expires_at = None
      oauth_row.scopes_granted = []
      connector = (
        db.query(models.Connector)
        .filter(models.Connector.id == connector_id)
        .first()
      )
      if connector is not None:
        connector.status = "oauth_required"
        connector.status_detail = "Sign in again to reconnect."
      db.commit()
      return None
    except core.ConnectorError:
      # Transient (server unreachable): keep the current token for this turn.
      return core.decrypt_oauth(oauth_row.access_token_encrypted)

    _store_tokens(oauth_row, tokens)
    db.commit()
    return core.decrypt_oauth(oauth_row.access_token_encrypted)


async def refresh_tokens(
  discovery: Discovery,
  client_id: str,
  client_secret: str | None,
  refresh_token: str,
) -> dict:
  """Refresh-token grant. Returns the raw token response dict.

  The caller decides latch policy: a transport failure with a live refresh
  token is transient (keep last health); an ``invalid_grant`` means the owner
  revoked access and the row latches to signed-out.
  """
  form = {
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
    "client_id": client_id,
    "resource": discovery.resource,
  }
  if client_secret:
    form["client_secret"] = client_secret
  status, body, _ = await core.pinned_json_request(
    "POST", discovery.token_endpoint, form=form, timeout_seconds=30.0,
  )
  if status == 200 and body.get("access_token"):
    return body
  # Distinguish "grant revoked" (definitive) from transport/5xx (transient).
  if body.get("error") == "invalid_grant" or status in (400, 401, 403):
    raise OAuthError("This sign-in was revoked. Sign in again.")
  raise core.ConnectorError(
    "Could not reach the authorization server.", transient=True,
  )
