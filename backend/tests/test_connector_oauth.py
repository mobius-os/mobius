"""OAuth sign-in for owner-managed MCP connections (spec 2026-07-28).

A single stateful mock stands in for both the OAuth-gated MCP resource server
and its authorization server, driven through the real handshake, discovery,
consent, token-custody, and broker code paths.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connector_oauth as cox
from app import connectors as core
from app import models
from app.database import SessionLocal
from app.timeutil import now_naive_utc

MCP_URL = "https://mcp.test/mcp"
AS_ISSUER = "https://as.test"
# The genuine client, captured before any fixture monkeypatches it.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class MockProvider:
  """Resource server + authorization server for one MCP endpoint."""

  def __init__(self, *, cimd=False, issue_secret=False, no_register=False,
               anon_open=False, slash_issuer=False):
    self.registered = 0
    self.codes = {}          # code -> {"challenge": ..., "resource": ...}
    self.refresh_tokens = {}  # refresh_token -> generation counter
    self.access_tokens = {}   # access_token -> valid bool
    self.issued = 0
    self.revoked = set()
    self.cimd = cimd                  # advertise Client ID Metadata Documents
    self.issue_secret = issue_secret  # DCR returns a client_secret (confidential)
    self.no_register = no_register    # pre-registered-camp: no DCR, no CIMD (BYO)
    self.anon_open = anon_open        # Google shape: handshake open, calls gated
    self.slash_issuer = slash_issuer  # Google shape: PRM issuer has trailing "/"
    self.secrets_seen = []            # client_secret values the token endpoint got
    self.client_ids_seen = []         # client_id values the token endpoint got

  def handler(self, request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    path = request.url.path
    try:
      body = json.loads(request.content) if request.content else {}
    except ValueError:
      body = {}
    form = {}
    if request.headers.get("content-type", "").startswith(
      "application/x-www-form-urlencoded"
    ):
      from urllib.parse import parse_qs
      form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    # ── MCP resource server ──
    if path == "/mcp":
      auth = request.headers.get("authorization", "")
      token = auth[7:] if auth.lower().startswith("bearer ") else ""
      if not token or token not in self.access_tokens or not self.access_tokens[token]:
        if not self.anon_open:
          return httpx.Response(
            401,
            headers={
              "www-authenticate":
                'Bearer resource_metadata='
                '"https://mcp.test/.well-known/oauth-protected-resource/mcp"',
            },
            json={"error": "invalid_token"},
          )
        # anon_open (Google shape): initialize and tools/list answer without
        # auth; only actual tool calls are gated — fall through to serving.
      # Signed in: normal MCP handshake.
      if body.get("method") == "initialize":
        return httpx.Response(200, headers={"content-type": "application/json"}, json={
          "jsonrpc": "2.0", "id": 1,
          "result": {"protocolVersion": "2025-11-25",
                     "serverInfo": {"name": "Test OAuth MCP"}},
        })
      if body.get("method") == "notifications/initialized":
        return httpx.Response(202)
      if body.get("method") == "tools/list":
        return httpx.Response(200, headers={"content-type": "application/json"}, json={
          "jsonrpc": "2.0", "id": body.get("id", 2),
          "result": {"tools": [{"name": "search"}]},
        })
      return httpx.Response(200, headers={"content-type": "application/json"},
                            json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    # ── protected-resource metadata (RFC 9728) ──
    if path == "/.well-known/oauth-protected-resource/mcp":
      return httpx.Response(200, json={
        "resource": "https://mcp.test/mcp",
        "authorization_servers": [
          AS_ISSUER + "/" if self.slash_issuer else AS_ISSUER,
        ],
        "scopes_supported": ["read", "write"],
      })

    # ── authorization-server metadata (RFC 8414) ──
    if path == "/.well-known/oauth-authorization-server":
      meta = {
        "issuer": AS_ISSUER,
        "authorization_endpoint": f"{AS_ISSUER}/authorize",
        "token_endpoint": f"{AS_ISSUER}/token",
        "registration_endpoint": f"{AS_ISSUER}/register",
        "revocation_endpoint": f"{AS_ISSUER}/revoke",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "scopes_supported": ["read", "write"],
      }
      if self.cimd:
        meta["client_id_metadata_document_supported"] = True
      if self.no_register:
        # Pre-registered camp (Google/GitHub/…): discovery + PKCE, but no way
        # to self-register — the owner must bring their own client.
        meta.pop("registration_endpoint")
      return httpx.Response(200, json=meta)

    # ── dynamic client registration (RFC 7591) ──
    if path == "/register":
      self.registered += 1
      reg = {"client_id": f"dcr-client-{self.registered}"}
      if self.issue_secret:
        reg["client_secret"] = f"dcr-secret-{self.registered}"
      return httpx.Response(201, json=reg)

    # ── token endpoint ──
    if path == "/token":
      self.secrets_seen.append(form.get("client_secret"))
      self.client_ids_seen.append(form.get("client_id"))
      if form.get("grant_type") == "authorization_code":
        return self._issue(form.get("code"))
      if form.get("grant_type") == "refresh_token":
        rt = form.get("refresh_token")
        if rt in self.revoked or rt not in self.refresh_tokens:
          return httpx.Response(400, json={"error": "invalid_grant"})
        return self._issue(None, rotate_from=rt)
      return httpx.Response(400, json={"error": "unsupported_grant_type"})

    # ── revocation (RFC 7009) ──
    if path == "/revoke":
      self.revoked.add(form.get("token"))
      return httpx.Response(200)

    return httpx.Response(404, json={"error": "not_found"})

  def _issue(self, code, rotate_from=None):
    self.issued += 1
    access = f"access-{self.issued}"
    refresh = f"refresh-{self.issued}"
    self.access_tokens[access] = True
    self.refresh_tokens[refresh] = self.issued
    if rotate_from:
      # Rotating refresh: invalidate the old one.
      self.refresh_tokens.pop(rotate_from, None)
    return httpx.Response(200, json={
      "access_token": access,
      "refresh_token": refresh,
      "token_type": "Bearer",
      "expires_in": 3600,
      "scope": "read write",
    })


def _wire(monkeypatch, mock):
  """Route all connector HTTP through the mock, echoing the SSRF validator."""
  monkeypatch.setattr(
    core, "_safe_endpoint",
    lambda url: (url, httpx.URL(url).host, httpx.URL(url).host),
  )
  monkeypatch.setattr(
    core.httpx, "AsyncClient",
    lambda **kwargs: _REAL_ASYNC_CLIENT(
      transport=httpx.MockTransport(mock.handler),
      timeout=kwargs.get("timeout"),
      follow_redirects=False,
    ),
  )
  return mock


@pytest.fixture
def provider(monkeypatch):
  return _wire(monkeypatch, MockProvider())


def _headers(auth, generation):
  return {**auth, "X-Mobius-Connector-Generation": generation}


# ── discovery unit ─────────────────────────────────────────────────────────


def test_canonical_resource_lowercases_host_not_path():
  assert cox.canonical_resource("HTTPS://MCP.Test/Mcp/") == "https://mcp.test/Mcp"


@pytest.mark.asyncio
async def test_discovery_requires_pkce_s256(monkeypatch):
  def no_pkce(request):
    if request.url.path == "/.well-known/oauth-protected-resource/mcp":
      return httpx.Response(200, json={
        "resource": "https://mcp.test/mcp", "authorization_servers": [AS_ISSUER],
      })
    if request.url.path == "/.well-known/oauth-authorization-server":
      return httpx.Response(200, json={
        "issuer": AS_ISSUER,
        "authorization_endpoint": f"{AS_ISSUER}/authorize",
        "token_endpoint": f"{AS_ISSUER}/token",
        # No code_challenge_methods_supported → client MUST refuse.
      })
    return httpx.Response(404, json={})

  monkeypatch.setattr(
    core, "_safe_endpoint",
    lambda url: (url, httpx.URL(url).host, httpx.URL(url).host),
  )
  monkeypatch.setattr(
    core.httpx, "AsyncClient",
    lambda **kw: _REAL_ASYNC_CLIENT(
      transport=httpx.MockTransport(no_pkce),
      timeout=kw.get("timeout"), follow_redirects=False),
  )
  with pytest.raises(cox.OAuthError, match="PKCE"):
    await cox.discover(MCP_URL, 'Bearer resource_metadata='
                       '"https://mcp.test/.well-known/oauth-protected-resource/mcp"')


# ── end-to-end sign-in ──────────────────────────────────────────────────────


def test_oauth_add_signin_broker_and_disconnect(client, auth, db, provider):
  # 1. Add an OAuth-gated endpoint → signed-out, discovery cached.
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL})
  assert created.status_code == 201, created.text
  row = created.json()
  assert row["status"] == "oauth_required"
  assert row["auth_kind"] == "oauth"
  assert row["signed_in"] is False
  cid, generation = row["id"], row["generation"]

  # Discovery persisted.
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert oauth.issuer == AS_ISSUER
  assert oauth.token_endpoint == f"{AS_ISSUER}/token"

  # 2. Client-metadata document is public and self-consistent.
  meta = client.get("/api/connectors/oauth/client-metadata.json")
  assert meta.status_code == 200
  assert meta.json()["client_id"] == meta.json()["client_id"]
  assert meta.json()["redirect_uris"][0].endswith("/api/connectors/oauth/callback")

  # 3. Start sign-in → authorize URL with PKCE + resource + sealed state.
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  )
  assert started.status_code == 200, started.text
  authorize_url = started.json()["authorize_url"]
  assert authorize_url.startswith(f"{AS_ISSUER}/authorize?")
  from urllib.parse import parse_qs, urlparse
  q = parse_qs(urlparse(authorize_url).query)
  assert q["code_challenge_method"] == ["S256"]
  assert q["resource"] == ["https://mcp.test/mcp"]
  assert q["scope"] == ["read write"]
  state = q["state"][0]

  # DCR happened once and was cached.
  assert provider.registered == 1

  # 4. Provider redirects to the callback with a code → tokens sealed, status ok.
  #    (No Bearer, cross-site — authorized solely by the sealed state.)
  provider.codes["auth-code"] = {}
  callback = client.get(
    "/api/connectors/oauth/callback",
    params={"code": "auth-code", "state": state, "iss": AS_ISSUER},
  )
  assert callback.status_code == 200
  assert "connected" in callback.text

  listed = client.get("/api/connectors", headers=auth).json()["connectors"]
  signed = next(c for c in listed if c["id"] == cid)
  assert signed["status"] == "ok"
  assert signed["signed_in"] is True
  assert signed["scopes"] == ["read", "write"]

  # Tokens are write-only: never present in any response body.
  assert "access-" not in callback.text
  assert "access-" not in json.dumps(listed)

  # 5. Broker attaches the token to a loopback MCP call.
  db.expire_all()
  cap = core.mint_broker_capability(
    cid, db.query(models.Connector).get(cid).capability_id,
  )
  with TestClient(client.app, client=("127.0.0.1", 43110)) as loopback:
    brokered = loopback.post(
      f"/api/connectors/{cid}/broker",
      headers={"Authorization": f"Bearer {cap}",
               "content-type": "application/json"},
      content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {}}),
    )
  assert brokered.status_code == 200
  assert "Test OAuth MCP" in brokered.text

  # 6. Disconnect → cleared, generation rotated, signed-out, upstream revoked.
  refresh_before = db.query(models.ConnectorOAuth).filter_by(
    connector_id=cid).one().refresh_token_encrypted
  assert refresh_before is not None
  gen_now = db.query(models.Connector).get(cid).capability_id
  disconnected = client.post(
    f"/api/connectors/{cid}/oauth/disconnect",
    headers=_headers(auth, gen_now),
  )
  assert disconnected.status_code == 200
  out = disconnected.json()
  assert out["status"] == "oauth_required"
  assert out["signed_in"] is False
  assert out["generation"] != gen_now  # revocation boundary
  db.expire_all()
  cleared = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert cleared.access_token_encrypted is None
  assert cleared.refresh_token_encrypted is None
  assert len(provider.revoked) == 1


@pytest.mark.asyncio
async def test_refresh_before_attach_renews_near_expiry(client, auth, db, provider):
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
  client.get("/api/connectors/oauth/callback",
             params={"code": "c1", "state": state, "iss": AS_ISSUER})

  # Force the stored token to look near-expiry.
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  first_access = core.decrypt_oauth(oauth.access_token_encrypted)
  oauth.access_expires_at = now_naive_utc()
  db.commit()

  session = SessionLocal()
  try:
    fresh = await cox.usable_access_token(session, cid)
  finally:
    session.close()
  assert fresh is not None and fresh != first_access  # refreshed
  assert provider.issued == 2


@pytest.mark.asyncio
async def test_refresh_revoked_grant_latches_signed_out(client, auth, db, provider):
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
  client.get("/api/connectors/oauth/callback",
             params={"code": "c1", "state": state, "iss": AS_ISSUER})

  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  provider.revoked.add(core.decrypt_oauth(oauth.refresh_token_encrypted))
  oauth.access_expires_at = now_naive_utc()
  db.commit()

  session = SessionLocal()
  try:
    token = await cox.usable_access_token(session, cid)
  finally:
    session.close()
  assert token is None
  db.expire_all()
  assert db.get(models.Connector, cid).status == "oauth_required"


def _signed_in_connection(client, auth, provider):
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
  client.get("/api/connectors/oauth/callback",
             params={"code": "c1", "state": state, "iss": AS_ISSUER})
  return cid, generation


def test_signin_populates_tools_immediately(client, auth, db, provider):
  """The callback probes with the fresh grant — no empty 0-tool row."""
  cid, _generation = _signed_in_connection(client, auth, provider)
  listed = client.get("/api/connectors", headers=auth).json()["connectors"]
  row = next(c for c in listed if c["id"] == cid)
  assert row["status"] == "ok"
  assert row["tools"] == ["search"]
  assert row["est_tokens"] > 0


def test_refresh_probes_signed_in_row_with_its_token(client, auth, db, provider):
  """2026-08-05 regression: the card auto-probe latched a signed-in row to
  'error' because it probed unauthenticated. A signed-in row's refresh must
  carry the token and stay healthy."""
  cid, generation = _signed_in_connection(client, auth, provider)
  refreshed = client.post(
    f"/api/connectors/{cid}/refresh", headers=_headers(auth, generation),
  )
  assert refreshed.status_code == 200, refreshed.text
  body = refreshed.json()
  assert body["status"] == "ok"
  assert body["signed_in"] is True
  assert body["tools"] == ["search"]


def test_refresh_after_revocation_latches_signed_out_not_error(
  client, auth, db, provider,
):
  cid, generation = _signed_in_connection(client, auth, provider)
  # The owner revokes everything provider-side: access + refresh both dead.
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  provider.access_tokens[core.decrypt_oauth(oauth.access_token_encrypted)] = False
  provider.revoked.add(core.decrypt_oauth(oauth.refresh_token_encrypted))
  refreshed = client.post(
    f"/api/connectors/{cid}/refresh", headers=_headers(auth, generation),
  )
  assert refreshed.status_code == 200
  body = refreshed.json()
  # Signed-out is the recoverable latch; "error" would dead-end the owner.
  assert body["status"] == "oauth_required"
  assert body["signed_in"] is False


def test_delete_removes_the_oauth_grant(client, auth, db, provider):
  """A deleted OAuth connector must not leave its sealed tokens behind — a
  reused SQLite row id would otherwise inherit the previous provider's grant."""
  cid, generation = _signed_in_connection(client, auth, provider)
  assert db.query(models.ConnectorOAuth).filter_by(connector_id=cid).count() == 1
  removed = client.request(
    "DELETE", f"/api/connectors/{cid}", headers=_headers(auth, generation),
  )
  assert removed.status_code == 200
  db.expire_all()
  assert db.query(models.ConnectorOAuth).filter_by(connector_id=cid).count() == 0


def test_callback_honors_the_sealed_generation(client, auth, db, provider):
  """A disconnect mid-consent rotates the generation; the stale callback must
  not revive the revoked grant onto the connector."""
  cid, generation = _signed_in_connection(client, auth, provider)
  # Start a fresh sign-in (seals the CURRENT generation into state)...
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  stale_state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
  # ...then disconnect, rotating capability_id and clearing tokens.
  db.expire_all()
  gen_now = db.get(models.Connector, cid).capability_id
  client.post(f"/api/connectors/{cid}/oauth/disconnect",
              headers=_headers(auth, gen_now))
  # The stale consent completes against the OLD generation.
  replayed = client.get("/api/connectors/oauth/callback",
                        params={"code": "c-late", "state": stale_state,
                                "iss": AS_ISSUER})
  assert "failed" in replayed.text
  db.expire_all()
  row = db.get(models.Connector, cid)
  assert row.status == "oauth_required"  # stayed revoked, not revived
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert oauth.access_token_encrypted is None


def test_cimd_path_registers_nothing_and_uses_the_metadata_url(
  client, auth, db, monkeypatch,
):
  """When the AS advertises CIMD, the instance uses its client-metadata URL as
  the client_id and stores NO registration row (the spec-preferred path)."""
  mock = _wire(monkeypatch, MockProvider(cimd=True))
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  q = parse_qs(urlparse(started["authorize_url"]).query)
  assert q["client_id"][0] == cox.client_metadata_url()
  assert mock.registered == 0  # CIMD → no dynamic registration
  assert db.query(models.OAuthClientRegistration).count() == 0
  # Sign-in completes with the CIMD client identity (no stored registration).
  state = q["state"][0]
  cb = client.get("/api/connectors/oauth/callback",
                  params={"code": "c1", "state": state, "iss": AS_ISSUER})
  assert "connected" in cb.text
  signed = next(c for c in client.get("/api/connectors", headers=auth).json()
                ["connectors"] if c["id"] == cid)
  assert signed["signed_in"] is True


@pytest.mark.asyncio
async def test_confidential_client_secret_is_stored_and_used_never_exposed(
  client, auth, db, monkeypatch,
):
  """A DCR-issued client_secret is encrypted at rest, sent to the token
  endpoint, and never appears in any API response."""
  mock = _wire(monkeypatch, MockProvider(issue_secret=True))
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]

  reg = db.query(models.OAuthClientRegistration).one()
  assert reg.client_secret_encrypted is not None
  assert "dcr-secret" not in reg.client_secret_encrypted  # encrypted at rest
  assert core.decrypt_oauth(reg.client_secret_encrypted) == "dcr-secret-1"

  cb = client.get("/api/connectors/oauth/callback",
                  params={"code": "c1", "state": state, "iss": AS_ISSUER})
  assert "connected" in cb.text
  # The token endpoint received the confidential secret...
  assert "dcr-secret-1" in mock.secrets_seen
  # ...but it never crosses the API boundary.
  listed = client.get("/api/connectors", headers=auth).text
  assert "dcr-secret" not in listed


def test_registration_is_reused_across_connectors_on_one_issuer(
  client, auth, db, provider,
):
  """Two connectors on the same authorization server share one registration."""
  a = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  client.post(f"/api/connectors/{a['id']}/oauth/start",
              headers=_headers(auth, a["generation"]))
  # A second OAuth connector resolving to the same authorization server.
  b = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  client.post(f"/api/connectors/{b['id']}/oauth/start",
              headers=_headers(auth, b["generation"]))
  assert a["id"] != b["id"]
  assert provider.registered == 1  # cached per issuer, not re-registered
  assert db.query(models.OAuthClientRegistration).count() == 1


def test_clear_client_does_not_remove_automatic_registration(
  client, auth, db, provider,
):
  created = client.post(
    "/api/connectors", headers=auth, json={"url": MCP_URL},
  ).json()
  client.post(
    f"/api/connectors/{created['id']}/oauth/start",
    headers=_headers(auth, created["generation"]),
  )
  reg = db.query(models.OAuthClientRegistration).one()
  assert reg.mode == "dcr"

  cleared = client.request(
    "DELETE", f"/api/connectors/{created['id']}/oauth/client",
    headers=_headers(auth, created["generation"]),
  )
  assert cleared.status_code == 200
  assert cleared.json() == {"ok": True, "removed": False}
  db.expire_all()
  assert db.query(models.OAuthClientRegistration).filter_by(
    issuer=AS_ISSUER, mode="dcr",
  ).count() == 1


def test_byo_setup_signin_and_recovery(client, auth, db, monkeypatch):
  """A pre-registered-camp provider: no DCR/CIMD. Sign-in first asks for the
  owner's client, then works end to end; credentials are recoverable."""
  mock = _wire(monkeypatch, MockProvider(no_register=True))
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  assert created["status"] == "oauth_required"

  # 1. Start sign-in → not a 422; a structured "needs setup" with the issuer
  #    and the server's authoritative redirect URI.
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  )
  assert started.status_code == 200, started.text
  body = started.json()
  assert body["needs_client_setup"] is True
  assert body["issuer"] == AS_ISSUER
  assert body["redirect_uri"].endswith("/api/connectors/oauth/callback")
  assert mock.registered == 0  # never tried to self-register

  # 2. Save the owner's client. Issuer is server-derived — a body issuer is
  #    ignored — and the secret never comes back.
  saved = client.post(
    f"/api/connectors/{cid}/oauth/client", headers=_headers(auth, generation),
    json={"client_id": "owner-client-42", "client_secret": "owner-secret",
          "issuer": "https://evil.test"},
  )
  assert saved.status_code == 200, saved.text
  assert "owner-secret" not in saved.text
  reg = db.query(models.OAuthClientRegistration).filter_by(issuer=AS_ISSUER).one()
  assert reg.mode == "byo" and reg.client_id == "owner-client-42"
  assert reg.client_secret_encrypted and "owner-secret" not in reg.client_secret_encrypted
  assert core.decrypt_oauth(reg.client_secret_encrypted) == "owner-secret"

  # 3. Now start returns a real authorize URL carrying the owner's client_id.
  started2 = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  q = parse_qs(urlparse(started2["authorize_url"]).query)
  assert q["client_id"] == ["owner-client-42"]
  state = q["state"][0]

  # 4. Callback exchanges with the owner's client_id + secret and signs in.
  cb = client.get("/api/connectors/oauth/callback",
                  params={"code": "c1", "state": state, "iss": AS_ISSUER})
  assert "connected" in cb.text
  assert "owner-client-42" in mock.client_ids_seen
  assert "owner-secret" in mock.secrets_seen
  listed = client.get("/api/connectors", headers=auth).json()["connectors"]
  assert next(c for c in listed if c["id"] == cid)["signed_in"] is True

  # 5. Re-saving a rotated secret upserts (no IntegrityError), and delete
  #    clears the shared credential.
  rotate = client.post(
    f"/api/connectors/{cid}/oauth/client", headers=_headers(auth, generation),
    json={"client_id": "owner-client-42", "client_secret": "rotated-secret"},
  )
  assert rotate.status_code == 200
  db.expire_all()
  reg = db.query(models.OAuthClientRegistration).filter_by(issuer=AS_ISSUER).one()
  assert core.decrypt_oauth(reg.client_secret_encrypted) == "rotated-secret"
  cleared = client.request(
    "DELETE", f"/api/connectors/{cid}/oauth/client",
    headers=_headers(auth, generation),
  )
  assert cleared.status_code == 200 and cleared.json()["removed"] is True
  assert db.query(models.OAuthClientRegistration).filter_by(issuer=AS_ISSUER).count() == 0


def test_anon_open_gate_detected_and_slash_issuer_normalized(
  client, auth, db, monkeypatch,
):
  """Google's exact shape: the whole anonymous handshake succeeds (initialize
  and tools/list are open; only tool calls are gated) and the protected-
  resource metadata names the issuer with a bare trailing slash. The add must
  classify this as a signed-out OAuth connection — not a healthy keyless one
  that would start failing mid-conversation — and store the issuer in its
  canonical slash-free form so one credential row serves every connection to
  that authority."""
  _wire(monkeypatch, MockProvider(
    anon_open=True, slash_issuer=True, no_register=True,
  ))
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL})
  assert created.status_code == 201, created.text
  row = created.json()
  assert row["status"] == "oauth_required"
  assert row["auth_kind"] == "oauth"
  assert row["signed_in"] is False
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=row["id"]).one()
  assert oauth.issuer == AS_ISSUER  # "…/" in the metadata, normalized here


def test_keyless_open_server_without_prm_stays_keyless(
  client, auth, monkeypatch,
):
  """The add-time gate check must not reclassify a genuinely open server: no
  protected-resource document → plain keyless add with its tool catalog."""
  base = MockProvider(anon_open=True)
  def no_prm(request):
    if request.url.path.startswith("/.well-known/"):
      return httpx.Response(404, json={})
    return base.handler(request)
  monkeypatch.setattr(
    core, "_safe_endpoint",
    lambda url: (url, httpx.URL(url).host, httpx.URL(url).host),
  )
  monkeypatch.setattr(
    core.httpx, "AsyncClient",
    lambda **kw: _REAL_ASYNC_CLIENT(
      transport=httpx.MockTransport(no_prm),
      timeout=kw.get("timeout"), follow_redirects=False),
  )
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL})
  assert created.status_code == 201, created.text
  row = created.json()
  assert row["status"] == "ok"
  assert row["auth_kind"] != "oauth"
  assert row["tools"] == ["search"]


def test_keyless_open_server_survives_unavailable_prm(
  client, auth, monkeypatch,
):
  """The opportunistic anonymous-gate discovery is not allowed to turn a
  healthy keyless add into a 500 when its well-known route is unavailable."""
  base = MockProvider(anon_open=True)

  def unavailable_prm(request):
    if request.url.path.startswith("/.well-known/"):
      raise httpx.ConnectError("metadata unavailable", request=request)
    return base.handler(request)

  monkeypatch.setattr(
    core, "_safe_endpoint",
    lambda url: (url, httpx.URL(url).host, httpx.URL(url).host),
  )
  monkeypatch.setattr(
    core.httpx, "AsyncClient",
    lambda **kw: _REAL_ASYNC_CLIENT(
      transport=httpx.MockTransport(unavailable_prm),
      timeout=kw.get("timeout"), follow_redirects=False,
    ),
  )
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL})
  assert created.status_code == 201, created.text
  row = created.json()
  assert row["status"] == "ok"
  assert row["auth_kind"] != "oauth"


def test_byo_client_secret_never_echoed_in_422(client, auth, db, monkeypatch):
  """A non-string secret is rejected value-free — pydantic must not echo it."""
  _wire(monkeypatch, MockProvider(no_register=True))
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  bad = client.post(
    f"/api/connectors/{cid}/oauth/client", headers=_headers(auth, generation),
    json={"client_id": "x", "client_secret": {"leak": "never-echo-secret"}},
  )
  assert bad.status_code == 400
  assert "never-echo-secret" not in bad.text


def test_google_issuer_requests_offline_access():
  """Google needs access_type=offline to return a refresh token; gated on the
  issuer host and applied to no one else."""
  from app import connector_oauth as cox
  from urllib.parse import parse_qs, urlparse

  google = cox.Discovery(
    resource="https://bigquery.googleapis.com/mcp",
    issuer="https://accounts.google.com",
    authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/bigquery"],
  )
  q = parse_qs(urlparse(cox.authorization_url(google, "cid", "chal", "st")).query)
  assert q["access_type"] == ["offline"]
  assert q["prompt"] == ["consent"]

  other = cox.Discovery(
    resource="https://mcp.test/mcp", issuer="https://as.test",
    authorization_endpoint="https://as.test/authorize",
    token_endpoint="https://as.test/token", scopes=["read"],
  )
  q2 = parse_qs(urlparse(cox.authorization_url(other, "cid", "chal", "st")).query)
  assert "access_type" not in q2 and "prompt" not in q2


def test_callback_rejects_forged_state(client, auth, db, provider):
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  forged = client.get("/api/connectors/oauth/callback",
                      params={"code": "x", "state": "not-a-sealed-token"})
  assert forged.status_code == 200
  assert "failed" in forged.text
  db.expire_all()
  assert db.query(models.Connector).get(created["id"]).status == "oauth_required"


def test_callback_rejects_issuer_mismatch(client, auth, db, provider):
  created = client.post("/api/connectors", headers=auth, json={"url": MCP_URL}).json()
  cid, generation = created["id"], created["generation"]
  started = client.post(
    f"/api/connectors/{cid}/oauth/start", headers=_headers(auth, generation),
  ).json()
  from urllib.parse import parse_qs, urlparse
  state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
  # RFC 9207: a mismatched iss must abort before the code is exchanged.
  bad = client.get("/api/connectors/oauth/callback",
                   params={"code": "c1", "state": state, "iss": "https://evil.test"})
  assert "failed" in bad.text
  assert provider.issued == 0
