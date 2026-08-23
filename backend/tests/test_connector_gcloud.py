"""Google-account (gcloud) sign-in for Google Cloud MCP connections.

Google supports neither dynamic client registration nor client-metadata
documents, so the one-click path can't serve its endpoints. This flow reuses
the Cloud SDK's published installed-app client and Google's hosted code page:
the owner opens a link, pastes a code, and the tokens flow through the same
sealed storage, refresh, and broker attach as every other connection — plus
the one Google-specific ``x-goog-user-project`` header.

A host-routed mock stands in for Google's token endpoint, Cloud Resource
Manager, and the BigQuery MCP server, driven through the real routes.
"""

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connector_oauth as cox
from app import connectors as core
from app import models

GCLOUD_MCP_URL = "https://bigquery.googleapis.com/mcp"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class GcloudMock:
  """Token endpoint + Resource Manager + BigQuery MCP for one owner."""

  def __init__(self, *, project_count=2, project_page_size=None):
    self.project_count = project_count
    self.project_page_size = project_page_size
    self.issued = 0
    self.access_tokens = {}
    self.refresh_tokens = {}
    self.token_client_ids = []
    self.token_secrets = []
    self.revoked = set()  # refresh tokens sent to the revoke endpoint
    self.broker_headers = []  # headers seen on MCP calls carrying a bearer
    self.project_queries = []  # exact CRM query parameters per page
    self.fail_projects = False
    self.fail_revoke = False
    self.omit_code_refresh = False
    self.omit_code_expiry = False

  def handler(self, request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    form = {}
    if request.headers.get("content-type", "").startswith(
      "application/x-www-form-urlencoded"
    ):
      form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    body = {}
    if request.content and request.headers.get(
      "content-type", ""
    ).startswith("application/json"):
      try:
        body = json.loads(request.content)
      except ValueError:
        body = {}

    # ── Google token endpoint ──
    if host == "oauth2.googleapis.com" and path == "/token":
      self.token_client_ids.append(form.get("client_id"))
      self.token_secrets.append(form.get("client_secret"))
      grant = form.get("grant_type")
      if grant == "authorization_code":
        return self._issue(
          include_refresh=not self.omit_code_refresh,
          include_expiry=not self.omit_code_expiry,
        )
      if grant == "refresh_token":
        rt = form.get("refresh_token")
        if rt not in self.refresh_tokens:
          return httpx.Response(400, json={"error": "invalid_grant"})
        return self._issue(rotate_from=rt)
      return httpx.Response(400, json={"error": "unsupported_grant_type"})

    # ── Google token revocation (RFC 7009) — should stay untouched for gcloud ──
    if host == "oauth2.googleapis.com" and path == "/revoke":
      if self.fail_revoke:
        raise httpx.ConnectError("revoke unavailable", request=request)
      self.revoked.add(form.get("token"))
      return httpx.Response(200)

    # ── Cloud Resource Manager: list the owner's projects (paginated) ──
    if host == "cloudresourcemanager.googleapis.com" and path == "/v1/projects":
      query = dict(request.url.params)
      self.project_queries.append(query)
      if self.fail_projects:
        raise httpx.ConnectError("projects unavailable", request=request)
      requested_page_size = int(query.get("pageSize", "50"))
      page_size = min(
        requested_page_size,
        self.project_page_size or requested_page_size,
      )
      start = int(query.get("pageToken", "0"))
      allp = [
        {"projectId": f"proj-{i}", "name": f"Project {i}",
         "lifecycleState": "ACTIVE"}
        for i in range(1, self.project_count + 1)
      ]
      page = allp[start:start + page_size]
      out = {"projects": page}
      if start + page_size < len(allp):
        out["nextPageToken"] = str(start + page_size)
      return httpx.Response(200, json=out)

    # ── Google Cloud MCP servers (BigQuery, Storage, …) — handshake open ──
    if host.endswith("googleapis.com") and path.endswith("/mcp"):
      auth = request.headers.get("authorization", "")
      if auth.lower().startswith("bearer "):
        self.broker_headers.append(dict(request.headers))
      method = body.get("method")
      if method == "initialize":
        return httpx.Response(200, headers={"content-type": "application/json"},
          json={"jsonrpc": "2.0", "id": body.get("id", 1),
                "result": {"protocolVersion": "2025-11-25",
                           "serverInfo": {"name": "BigQuery MCP"}}})
      if method == "notifications/initialized":
        return httpx.Response(202)
      if method == "tools/list":
        return httpx.Response(200, headers={"content-type": "application/json"},
          json={"jsonrpc": "2.0", "id": body.get("id", 2),
                "result": {"tools": [{"name": "list_dataset_ids"}]}})
      return httpx.Response(200, headers={"content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    return httpx.Response(404, json={"error": "not_found", "host": host})

  def _issue(
    self, rotate_from=None, *, include_refresh=True, include_expiry=True,
  ):
    self.issued += 1
    access, refresh = f"ya29.access-{self.issued}", f"refresh-{self.issued}"
    self.access_tokens[access] = True
    payload = {
      "access_token": access,
      "token_type": "Bearer",
      "scope": "openid https://www.googleapis.com/auth/cloud-platform",
    }
    if include_expiry:
      payload["expires_in"] = 3600
    if include_refresh:
      self.refresh_tokens[refresh] = self.issued
      payload["refresh_token"] = refresh
      if rotate_from:
        self.refresh_tokens.pop(rotate_from, None)
    return httpx.Response(200, json=payload)


def _wire(monkeypatch, mock):
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


def _headers(auth, generation):
  return {**auth, "X-Mobius-Connector-Generation": generation}


GCS_MCP_URL = "https://storage.googleapis.com/storage/mcp"


def _seed_google_connector(db, *, slug="google_bigquery_test",
                           name="Google BigQuery", url=GCLOUD_MCP_URL):
  """Insert a signed-out Google Cloud connector as add_connector would."""
  row = models.Connector(
    slug=slug,
    name=name,
    url=url,
    enabled=True,
    tools_json=[],
    est_tokens=0,
    status="oauth_required",
    status_detail="Sign in to finish connecting.",
  )
  db.add(row)
  db.flush()
  db.add(models.ConnectorOAuth(
    connector_id=row.id,
    resource=url,
    issuer="https://accounts.google.com",
    authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    scopes_advertised=["https://www.googleapis.com/auth/cloud-platform"],
  ))
  db.commit()
  return row.id, row.capability_id


def _complete_signin(client, auth, cid, gen, project_id=""):
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  return client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/x", "project_id": project_id},
  ).json()


# ── unit ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", [
  "bigquery.googleapis.com",
  "firestore.googleapis.com",
  "logging.googleapis.com",
  "pubsub.googleapis.com",
  "run.googleapis.com",
  "storage.googleapis.com",
])
def test_is_google_cloud_mcp_url_exact_allowlist(host):
  assert cox.is_google_cloud_mcp_url(f"https://{host}/any/mcp/path")


@pytest.mark.parametrize("url", [
  "https://googleapis.com/mcp",
  "https://drivemcp.googleapis.com/mcp/v1",
  "https://drive.googleapis.com/mcp",
  "https://mapstools.googleapis.com/mcp",
  "https://googleapis.com.evil.test/mcp",
  "https://mcp.notion.com/mcp",
])
def test_is_google_cloud_mcp_url_rejects_other_hosts(url):
  assert not cox.is_google_cloud_mcp_url(url)


def test_gcloud_client_identity_reads_sdk():
  cid, secret = cox.gcloud_client_identity()
  assert cid.endswith(".apps.googleusercontent.com")
  assert secret and len(secret) > 8


def test_gcloud_authorization_url_shape():
  url = cox.gcloud_authorization_url("client-x", "chal", "state-y")
  parsed = urlparse(url)
  q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
  assert parsed.netloc == "accounts.google.com"
  assert q["redirect_uri"] == "https://sdk.cloud.google.com/authcode.html"
  assert q["token_usage"] == "remote"
  assert q["access_type"] == "offline"
  assert q["code_challenge_method"] == "S256"
  assert q["client_id"] == "client-x"
  # The minted Cloud token is audience-less, like gcloud's — no resource param.
  assert "resource" not in q


# ── flow ──────────────────────────────────────────────────────────────────


def test_gcloud_start_returns_link_and_state(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock())
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  )
  assert started.status_code == 200
  data = started.json()
  assert "token_usage=remote" in data["authorize_url"]
  assert data["state"]


def test_gcloud_start_rejected_for_non_google(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock())
  row = models.Connector(
    slug="notion_test", name="Notion", url="https://mcp.notion.com/mcp",
    enabled=True, tools_json=[], est_tokens=0, status="oauth_required",
  )
  db.add(row)
  db.flush()
  db.add(models.ConnectorOAuth(
    connector_id=row.id, resource="https://mcp.notion.com/mcp",
    issuer="https://mcp.notion.com",
    authorization_endpoint="https://mcp.notion.com/authorize",
    token_endpoint="https://mcp.notion.com/token", scopes_advertised=[],
  ))
  db.commit()
  resp = client.post(
    f"/api/connectors/{row.id}/oauth/gcloud/start",
    headers=_headers(auth, row.capability_id),
  )
  assert resp.status_code == 409


def test_gcloud_complete_multi_project_then_set_and_broker(
  client, auth, db, monkeypatch,
):
  mock = _wire(monkeypatch, GcloudMock(project_count=2))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()

  done = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/pasted-code"},
  )
  assert done.status_code == 200
  out = done.json()
  # Two projects → the owner must choose; tokens are sealed regardless.
  assert out["needs_project"] is True
  assert {p["project_id"] for p in out["projects"]} == {"proj-1", "proj-2"}
  assert out["connection"]["signed_in"] is True
  assert out["connection"]["oauth_flavor"] == "gcloud"
  # Withheld from turns until a project is chosen (can't call tools without it).
  assert out["connection"]["status"] == "oauth_required"

  # A project-less gcloud grant must NOT be brokered even if reached directly.
  db.expire_all()
  cap0 = core.mint_broker_capability(
    cid, db.get(models.Connector, cid).capability_id,
  )
  with TestClient(client.app, client=("127.0.0.1", 43110)) as loopback:
    refused = loopback.post(
      f"/api/connectors/{cid}/broker",
      headers={"Authorization": f"Bearer {cap0}",
               "content-type": "application/json"},
      content=json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": "initialize", "params": {}}),
    )
  assert refused.status_code == 404

  # The exchange used the Cloud SDK client (confidential) — not our CIMD URL.
  gcloud_id, gcloud_secret = cox.gcloud_client_identity()
  assert mock.token_client_ids[0] == gcloud_id
  assert mock.token_secrets[0] == gcloud_secret

  db.expire_all()
  row = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert row.auth_mode == "gcloud"
  assert row.client_id == gcloud_id
  assert row.client_secret_encrypted  # sealed
  assert row.access_token_encrypted and row.refresh_token_encrypted
  assert row.user_project is None  # not chosen yet
  # Tokens never cross the API boundary.
  assert "ya29" not in json.dumps(out)

  # Choose a project.
  gen2 = db.get(models.Connector, cid).capability_id
  chosen = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/project",
    headers=_headers(auth, gen2), json={"project_id": "proj-2"},
  )
  assert chosen.status_code == 200
  assert chosen.json()["connection"]["user_project"] == "proj-2"
  db.expire_all()
  assert db.query(models.ConnectorOAuth).filter_by(
    connector_id=cid).one().user_project == "proj-2"

  # Broker attaches BOTH the bearer token and the quota-project header.
  cap = core.mint_broker_capability(
    cid, db.get(models.Connector, cid).capability_id,
  )
  with TestClient(client.app, client=("127.0.0.1", 43110)) as loopback:
    brokered = loopback.post(
      f"/api/connectors/{cid}/broker",
      headers={"Authorization": f"Bearer {cap}",
               "content-type": "application/json"},
      content=json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": "initialize", "params": {}}),
    )
  assert brokered.status_code == 200
  assert "BigQuery MCP" in brokered.text
  seen = mock.broker_headers[-1]
  assert seen.get("x-goog-user-project") == "proj-2"
  assert seen.get("authorization", "").startswith("Bearer ya29.")


def test_gcloud_reauthorization_clears_a_prior_refresh_token(
  client, auth, db, monkeypatch,
):
  """An auth-code response for account B may omit a refresh token. It must
  never retain account A's old refresh token and silently switch back later."""
  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  db.expire_all()
  first = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert core.decrypt_oauth(first.refresh_token_encrypted) == "refresh-1"
  prior_expiry = datetime(2099, 1, 1)
  first.access_expires_at = prior_expiry
  db.commit()

  mock.omit_code_refresh = True
  mock.omit_code_expiry = True
  second = _complete_signin(client, auth, cid, gen, project_id="proj-1")
  assert second["connection"]["signed_in"] is True
  db.expire_all()
  current = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert core.decrypt_oauth(current.access_token_encrypted) == "ya29.access-2"
  assert current.refresh_token_encrypted is None
  assert current.access_expires_at != prior_expiry


def test_gcloud_complete_survives_project_transport_failure(
  client, auth, db, monkeypatch,
):
  """Project listing is best-effort: a network failure after code exchange
  must not lose the consumed one-shot code or the freshly issued grant."""
  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  mock.fail_projects = True
  done = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/pasted-code"},
  )
  assert done.status_code == 200, done.text
  assert done.json()["projects"] == []
  assert done.json()["needs_project"] is True
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert oauth.access_token_encrypted and oauth.refresh_token_encrypted


@pytest.mark.asyncio
async def test_oauth_transport_errors_are_normalized(monkeypatch):
  request = httpx.Request("GET", "https://oauth2.googleapis.com/token")
  failures = (
    TimeoutError("timed out"),
    httpx.ConnectError("connect failed", request=request),
  )
  for failure in failures:
    async def fail(*_args, _failure=failure, **_kwargs):
      raise _failure

    monkeypatch.setattr(core, "pinned_json_request", fail)
    with pytest.raises(core.ConnectorError) as raised:
      await cox.oauth_json_request(
        "GET", "https://oauth2.googleapis.com/token",
      )
    assert raised.value.transient is True


def test_gcloud_list_projects_for_signed_in_connection(
  client, auth, db, monkeypatch,
):
  # After signing in (single project auto-selected), the picker endpoint
  # returns the full live list plus the currently-selected project.
  _wire(monkeypatch, GcloudMock(project_count=3))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/x", "project_id": "proj-2"},
  )
  db.expire_all()
  gen2 = db.get(models.Connector, cid).capability_id
  listed = client.get(
    f"/api/connectors/{cid}/oauth/gcloud/projects", headers=_headers(auth, gen2),
  )
  assert listed.status_code == 200
  body = listed.json()
  assert {p["project_id"] for p in body["projects"]} == {"proj-1", "proj-2", "proj-3"}
  assert body["current"] == "proj-2"


def test_gcloud_list_projects_requires_signin(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock())
  cid, gen = _seed_google_connector(db)  # signed out
  resp = client.get(
    f"/api/connectors/{cid}/oauth/gcloud/projects", headers=_headers(auth, gen),
  )
  assert resp.status_code == 409


@pytest.mark.asyncio
async def test_gcloud_set_project_releases_request_session_before_crm(
  client, auth, db, monkeypatch,
):
  """The request Session must not remain checked out across CRM and then sit
  beside the fresh generation-guarded write Session."""
  from app.database import SessionLocal
  from app.routes import connectors as connector_routes

  _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  db.close()

  request_session = SessionLocal()

  async def assert_released(_token):
    assert not request_session.in_transaction()
    return [{"project_id": "proj-1", "name": "Project 1"}]

  monkeypatch.setattr(cox, "list_google_projects", assert_released)
  try:
    result = await connector_routes.oauth_gcloud_set_project(
      cid,
      connector_routes.GcloudProjectBody(project_id="proj-1"),
      generation=gen,
      _owner=None,
      db=request_session,
    )
  finally:
    request_session.close()
  assert result["connection"]["user_project"] == "proj-1"


def test_gcloud_set_project_aborts_when_signin_changes_during_lookup(
  client, auth, db, monkeypatch,
):
  """A project verified with account A must not be written onto account B
  when a fresh sign-in completes under the same connector generation."""
  from app.database import SessionLocal

  _wire(monkeypatch, GcloudMock(project_count=2))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen, project_id="proj-1")

  async def resign_during_lookup(_token):
    with SessionLocal() as concurrent:
      oauth = concurrent.query(models.ConnectorOAuth).filter_by(
        connector_id=cid,
      ).one()
      oauth.access_token_encrypted = core.encrypt_oauth("account-b-access")
      oauth.refresh_token_encrypted = core.encrypt_oauth("account-b-refresh")
      oauth.user_project = None
      concurrent.commit()
    return [{"project_id": "proj-2", "name": "Project 2"}]

  monkeypatch.setattr(cox, "list_google_projects", resign_during_lookup)
  changed = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/project",
    headers=_headers(auth, gen),
    json={"project_id": "proj-2"},
  )
  assert changed.status_code == 409, changed.text
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert core.decrypt_oauth(oauth.access_token_encrypted) == "account-b-access"
  assert oauth.user_project is None


def test_gcloud_reuse_signin_across_connections(client, auth, db, monkeypatch):
  # Sign in one Google connection, then a second Google Cloud connection adopts
  # that sign-in with no re-approval — only a project pick.
  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  a_id, a_gen = _seed_google_connector(db)
  _complete_signin(client, auth, a_id, a_gen)  # single project auto-selected

  b_id, b_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Google Cloud Storage", url=GCS_MCP_URL,
  )
  # The second connection can see the first as a reusable source.
  reusable = client.get(
    f"/api/connectors/{b_id}/oauth/gcloud/reusable", headers=_headers(auth, b_gen),
  ).json()
  assert [s["connector_id"] for s in reusable["sources"]] == [a_id]
  assert reusable["sources"][0]["generation"] == a_gen

  reused = client.post(
    f"/api/connectors/{b_id}/oauth/gcloud/reuse",
    headers=_headers(auth, b_gen),
    json={"source_connector_id": a_id, "source_generation": a_gen},
  )
  assert reused.status_code == 200
  out = reused.json()
  assert out["connection"]["signed_in"] is True
  assert out["connection"]["oauth_flavor"] == "gcloud"
  assert out["connection"]["user_project"] == "proj-1"  # single → auto
  assert out["needs_project"] is False
  # No fresh Google consent happened: reuse does not mint a new grant via the
  # authorization-code exchange (issued count only moves for token refreshes).
  assert "ya29" not in json.dumps(out)

  # The adopted credential is the same one (copied), and B now brokers calls
  # with its own project header + the shared bearer token.
  db.expire_all()
  a = db.query(models.ConnectorOAuth).filter_by(connector_id=a_id).one()
  b = db.query(models.ConnectorOAuth).filter_by(connector_id=b_id).one()
  assert b.refresh_token_encrypted and b.client_id == a.client_id
  cap = core.mint_broker_capability(
    b_id, db.get(models.Connector, b_id).capability_id,
  )
  with TestClient(client.app, client=("127.0.0.1", 43110)) as loopback:
    brokered = loopback.post(
      f"/api/connectors/{b_id}/broker",
      headers={"Authorization": f"Bearer {cap}", "content-type": "application/json"},
      content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    )
  assert brokered.status_code == 200
  seen = mock.broker_headers[-1]
  assert seen.get("x-goog-user-project") == "proj-1"
  assert seen.get("authorization", "").startswith("Bearer ya29.")


def test_gcloud_reuse_rejects_unavailable_source(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock())
  a_id, a_gen = _seed_google_connector(db)  # signed OUT
  b_id, b_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Cloud Storage", url=GCS_MCP_URL,
  )
  # Source is not signed in → nothing to reuse.
  resp = client.post(
    f"/api/connectors/{b_id}/oauth/gcloud/reuse",
    headers=_headers(auth, b_gen),
    json={"source_connector_id": a_id, "source_generation": a_gen},
  )
  assert resp.status_code == 409
  # And it cannot reuse itself.
  self_resp = client.post(
    f"/api/connectors/{b_id}/oauth/gcloud/reuse",
    headers=_headers(auth, b_gen),
    json={"source_connector_id": b_id, "source_generation": b_gen},
  )
  assert self_resp.status_code == 400


def test_gcloud_reuse_rejects_a_stale_source_generation(
  client, auth, db, monkeypatch,
):
  """A stale reusable choice must not follow a SQLite id into a newer grant."""
  _wire(monkeypatch, GcloudMock(project_count=1))
  source_id, old_source_gen = _seed_google_connector(db)
  _complete_signin(client, auth, source_id, old_source_gen)
  target_id, target_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Cloud Storage", url=GCS_MCP_URL,
  )

  disconnected = client.post(
    f"/api/connectors/{source_id}/oauth/disconnect",
    headers=_headers(auth, old_source_gen),
  )
  assert disconnected.status_code == 200
  new_source_gen = disconnected.json()["generation"]
  _complete_signin(client, auth, source_id, new_source_gen)

  stale = client.post(
    f"/api/connectors/{target_id}/oauth/gcloud/reuse",
    headers=_headers(auth, target_gen),
    json={
      "source_connector_id": source_id,
      "source_generation": old_source_gen,
    },
  )
  assert stale.status_code == 409
  db.expire_all()
  target = db.query(models.ConnectorOAuth).filter_by(
    connector_id=target_id,
  ).one()
  assert target.access_token_encrypted is None
  assert target.refresh_token_encrypted is None


def test_gcloud_reuse_aborts_when_source_changes_during_project_lookup(
  client, auth, db, monkeypatch,
):
  """Deterministically exercise the snapshot→network→commit gap: a source
  disconnect/replacement during CRM lookup must abort, never copy stale tokens."""
  from app.database import SessionLocal
  from app.routes import connectors as connector_routes

  _wire(monkeypatch, GcloudMock(project_count=1))
  source_id, source_gen = _seed_google_connector(db)
  _complete_signin(client, auth, source_id, source_gen)
  target_id, target_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Cloud Storage", url=GCS_MCP_URL,
  )
  real_resolve = connector_routes._resolve_gcloud_project

  async def mutate_source(access_token, requested):
    result = await real_resolve(access_token, requested)
    with SessionLocal() as concurrent:
      source_row = concurrent.query(models.Connector).filter_by(
        id=source_id,
      ).one()
      source_oauth = concurrent.query(models.ConnectorOAuth).filter_by(
        connector_id=source_id,
      ).one()
      source_row.capability_id = "f" * 64
      source_row.status = "oauth_required"
      source_oauth.access_token_encrypted = None
      source_oauth.refresh_token_encrypted = None
      concurrent.commit()
    return result

  monkeypatch.setattr(
    connector_routes, "_resolve_gcloud_project", mutate_source,
  )
  reused = client.post(
    f"/api/connectors/{target_id}/oauth/gcloud/reuse",
    headers=_headers(auth, target_gen),
    json={
      "source_connector_id": source_id,
      "source_generation": source_gen,
    },
  )
  assert reused.status_code == 409, reused.text
  db.expire_all()
  target = db.query(models.ConnectorOAuth).filter_by(
    connector_id=target_id,
  ).one()
  assert target.access_token_encrypted is None
  assert target.refresh_token_encrypted is None


def test_gcloud_reuse_aborts_when_target_signs_in_during_project_lookup(
  client, auth, db, monkeypatch,
):
  """A stale reuse must not overwrite a same-generation target completion."""
  from app.database import SessionLocal
  from app.routes import connectors as connector_routes

  _wire(monkeypatch, GcloudMock(project_count=1))
  source_id, source_gen = _seed_google_connector(db)
  _complete_signin(client, auth, source_id, source_gen)
  target_id, target_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Cloud Storage", url=GCS_MCP_URL,
  )
  real_resolve = connector_routes._resolve_gcloud_project

  async def complete_target(access_token, requested):
    result = await real_resolve(access_token, requested)
    with SessionLocal() as concurrent:
      target = concurrent.query(models.ConnectorOAuth).filter_by(
        connector_id=target_id,
      ).one()
      target.auth_mode = "gcloud"
      target.client_id = "account-b-client"
      target.access_token_encrypted = core.encrypt_oauth("account-b-access")
      target.refresh_token_encrypted = core.encrypt_oauth("account-b-refresh")
      target.user_project = "proj-2"
      concurrent.commit()
    return result

  monkeypatch.setattr(connector_routes, "_resolve_gcloud_project", complete_target)
  reused = client.post(
    f"/api/connectors/{target_id}/oauth/gcloud/reuse",
    headers=_headers(auth, target_gen),
    json={
      "source_connector_id": source_id,
      "source_generation": source_gen,
    },
  )
  assert reused.status_code == 409, reused.text
  db.expire_all()
  target = db.query(models.ConnectorOAuth).filter_by(
    connector_id=target_id,
  ).one()
  assert core.decrypt_oauth(target.access_token_encrypted) == "account-b-access"
  assert target.client_id == "account-b-client"
  assert target.user_project == "proj-2"


def test_gcloud_disconnect_does_not_revoke_shared_credential(
  client, auth, db, monkeypatch,
):
  # Signing out one connection must NOT revoke the Google credential a sibling
  # adopted — Google would kill it for every holder.
  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  a_id, a_gen = _seed_google_connector(db)
  _complete_signin(client, auth, a_id, a_gen)
  b_id, b_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Cloud Storage", url=GCS_MCP_URL,
  )
  client.post(
    f"/api/connectors/{b_id}/oauth/gcloud/reuse",
    headers=_headers(auth, b_gen),
    json={"source_connector_id": a_id, "source_generation": a_gen},
  )
  db.expire_all()
  a_gen_now = db.get(models.Connector, a_id).capability_id
  out = client.post(
    f"/api/connectors/{a_id}/oauth/disconnect", headers=_headers(auth, a_gen_now),
  )
  assert out.status_code == 200
  assert out.json()["signed_in"] is False
  # Crucially: no upstream revocation, because the sibling still holds the token.
  assert len(mock.revoked) == 0
  # The sibling that adopted the credential is still signed in and usable.
  db.expire_all()
  b = db.query(models.ConnectorOAuth).filter_by(connector_id=b_id).one()
  assert b.access_token_encrypted is not None


def test_gcloud_disconnect_revokes_when_unshared(client, auth, db, monkeypatch):
  # A lone gcloud connection whose token nobody else holds SHOULD revoke
  # upstream on sign-out, so the grant truly ends.
  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  db.expire_all()
  refresh = core.decrypt_oauth(
    db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
    .refresh_token_encrypted
  )
  gen_now = db.get(models.Connector, cid).capability_id
  client.post(
    f"/api/connectors/{cid}/oauth/disconnect", headers=_headers(auth, gen_now),
  )
  assert refresh in mock.revoked


def test_gcloud_disconnect_is_locally_successful_when_revoke_transport_fails(
  client, auth, db, monkeypatch,
):
  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  mock.fail_revoke = True
  signed_out = client.post(
    f"/api/connectors/{cid}/oauth/disconnect", headers=_headers(auth, gen),
  )
  assert signed_out.status_code == 200, signed_out.text
  assert signed_out.json()["signed_in"] is False
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert oauth.access_token_encrypted is None
  assert oauth.refresh_token_encrypted is None


def test_gcloud_projectless_stays_withheld_after_recheck(
  client, auth, db, monkeypatch,
):
  # Two projects, none chosen → withheld. A later Recheck must NOT flip it to
  # "ok" (the handshake succeeds without a project, but tool calls can't).
  _wire(monkeypatch, GcloudMock(project_count=2))
  cid, gen = _seed_google_connector(db)
  out = _complete_signin(client, auth, cid, gen)
  assert out["needs_project"] is True
  assert out["connection"]["status"] == "oauth_required"
  db.expire_all()
  gen_now = db.get(models.Connector, cid).capability_id
  rechecked = client.post(
    f"/api/connectors/{cid}/refresh", headers=_headers(auth, gen_now),
  )
  assert rechecked.status_code == 200
  assert rechecked.json()["status"] == "oauth_required"  # still withheld


def test_gcloud_reuse_clears_stale_project(client, auth, db, monkeypatch):
  # A connection that once had a project, signed out, then reuses a multi-project
  # account with none named must not keep the stale project.
  _wire(monkeypatch, GcloudMock(project_count=1))
  a_id, a_gen = _seed_google_connector(db)
  _complete_signin(client, auth, a_id, a_gen)  # A: single project auto
  b_id, b_gen = _seed_google_connector(
    db, slug="google_storage_test", name="Cloud Storage", url=GCS_MCP_URL,
  )
  # Give B a stale project directly, as a prior sign-in would have.
  b_oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=b_id).one()
  b_oauth.user_project = "old-stale-proj"
  db.commit()
  # Source A has one project; reuse WITHOUT naming one auto-selects A's sole
  # project, so to exercise the clear path we point B at a 2-project account.
  # Re-wire the mock to 2 projects for the reuse call.
  _wire(monkeypatch, GcloudMock(project_count=2))
  # A must look signed-in to the 2-project mock too; re-sign A so its token is
  # valid against the new mock.
  _complete_signin(client, auth, a_id,
                   db.get(models.Connector, a_id).capability_id,
                   project_id="proj-1")
  db.expire_all()
  reused = client.post(
    f"/api/connectors/{b_id}/oauth/gcloud/reuse",
    headers=_headers(auth, db.get(models.Connector, b_id).capability_id),
    json={
      "source_connector_id": a_id,
      "source_generation": db.get(models.Connector, a_id).capability_id,
    },  # no project_id → none auto-selected
  )
  assert reused.status_code == 200
  assert reused.json()["needs_project"] is True
  db.expire_all()
  assert db.query(models.ConnectorOAuth).filter_by(
    connector_id=b_id).one().user_project is None  # stale value cleared


def test_gcloud_complete_single_project_auto_selects(
  client, auth, db, monkeypatch,
):
  _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  out = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/pasted-code"},
  ).json()
  assert out["needs_project"] is False
  assert out["connection"]["user_project"] == "proj-1"


def test_valid_gcloud_project_id():
  assert cox.valid_gcloud_project_id("my-proj-123")
  assert not cox.valid_gcloud_project_id("Bad_Upper")
  assert not cox.valid_gcloud_project_id("ab")           # too short
  assert not cox.valid_gcloud_project_id("ends-")        # trailing hyphen
  assert not cox.valid_gcloud_project_id("p\r\nx: y")    # control chars


def test_gcloud_complete_requests_max_project_page(
  client, auth, db, monkeypatch,
):
  # Ask Resource Manager for its 500-item maximum so an owner with a large
  # project list does not pay avoidable sequential page round-trips.
  mock = _wire(monkeypatch, GcloudMock(project_count=120))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  out = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/x", "project_id": "proj-119"},
  )
  assert out.status_code == 200
  assert len(out.json()["projects"]) == 120
  assert out.json()["connection"]["user_project"] == "proj-119"
  assert mock.project_queries == [{"pageSize": "500"}]


def test_gcloud_project_list_honors_500_result_safety_cap(
  client, auth, db, monkeypatch,
):
  # CRM may return fewer rows than requested and advertise a next page. Stop
  # midway through that later page rather than exceeding the documented cap.
  mock = _wire(
    monkeypatch,
    GcloudMock(project_count=800, project_page_size=400),
  )
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  out = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/x", "project_id": "proj-499"},
  )
  assert out.status_code == 200, out.text
  assert len(out.json()["projects"]) == 500
  assert out.json()["projects"][-1]["project_id"] == "proj-500"
  assert mock.project_queries == [
    {"pageSize": "500"},
    {"pageSize": "500", "pageToken": "400"},
  ]


def test_gcloud_complete_rejects_malformed_project_id(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock(project_count=2))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  resp = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/x", "project_id": "Bad Id!"},
  )
  assert resp.status_code == 400


def test_gcloud_complete_rejects_unknown_project(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock(project_count=2))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  resp = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/x",
          "project_id": "not-my-project"},  # valid shape, not a member → 422
  )
  assert resp.status_code == 422


def test_gcloud_complete_rejects_stale_state(client, auth, db, monkeypatch):
  _wire(monkeypatch, GcloudMock())
  cid, gen = _seed_google_connector(db)
  # A sealed state for a different connector id must not be accepted here.
  foreign = cox.seal_flow({
    "connector_id": cid + 999, "generation": gen,
    "verifier": "v", "client_id": "c", "mode": "gcloud",
  })
  resp = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": foreign, "code": "4/x"},
  )
  assert resp.status_code == 400


@pytest.mark.asyncio
async def test_gcloud_refresh_uses_stored_client(client, auth, db, monkeypatch):
  from app.database import SessionLocal
  from app.timeutil import now_naive_utc

  mock = _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  started = client.post(
    f"/api/connectors/{cid}/oauth/gcloud/start", headers=_headers(auth, gen),
  ).json()
  client.post(
    f"/api/connectors/{cid}/oauth/gcloud/complete",
    headers=_headers(auth, gen),
    json={"state": started["state"], "code": "4/pasted-code"},
  )
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  first = core.decrypt_oauth(oauth.access_token_encrypted)
  oauth.access_expires_at = now_naive_utc()  # force near-expiry
  db.commit()

  session = SessionLocal()
  try:
    fresh = await cox.usable_access_token(session, cid)
  finally:
    session.close()
  assert fresh and fresh != first
  # The refresh authenticated with the stored gcloud client, not CIMD.
  gcloud_id, _ = cox.gcloud_client_identity()
  assert mock.token_client_ids[-1] == gcloud_id


@pytest.mark.asyncio
async def test_gcloud_refresh_does_not_overwrite_a_replacement_generation(
  client, auth, db, monkeypatch,
):
  """A refresh result in flight for a stale generation cannot land on a
  disconnect/re-sign replacement that retained the same numeric id."""
  from app.database import SessionLocal

  _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  oauth.access_expires_at = datetime(2000, 1, 1)
  db.commit()

  async def replace_during_refresh(*_args, **_kwargs):
    with SessionLocal() as concurrent:
      row = concurrent.query(models.Connector).filter_by(id=cid).one()
      replacement = concurrent.query(models.ConnectorOAuth).filter_by(
        connector_id=cid,
      ).one()
      row.capability_id = "f" * 64
      replacement.access_token_encrypted = core.encrypt_oauth(
        "replacement-access",
      )
      replacement.refresh_token_encrypted = core.encrypt_oauth(
        "replacement-refresh",
      )
      replacement.access_expires_at = datetime(2099, 1, 1)
      concurrent.commit()
    return {
      "access_token": "stale-refresh-access",
      "refresh_token": "stale-refresh-token",
      "expires_in": 3600,
    }

  monkeypatch.setattr(cox, "refresh_tokens", replace_during_refresh)
  session = SessionLocal()
  try:
    fresh = await cox.usable_access_token(session, cid, generation=gen)
  finally:
    session.close()
  assert fresh is None
  db.expire_all()
  replacement = db.query(models.ConnectorOAuth).filter_by(
    connector_id=cid,
  ).one()
  assert core.decrypt_oauth(
    replacement.access_token_encrypted,
  ) == "replacement-access"
  assert core.decrypt_oauth(
    replacement.refresh_token_encrypted,
  ) == "replacement-refresh"


@pytest.mark.asyncio
async def test_gcloud_refresh_releases_request_session_before_waits(
  client, auth, db, monkeypatch,
):
  """Near-expiry refresh must release the request DB lease before both the
  single-flight wait and the token-endpoint await."""
  from app.database import SessionLocal

  _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  oauth.access_expires_at = datetime(2000, 1, 1)
  db.commit()

  request_session = SessionLocal()

  class AssertReleasedLock:
    async def __aenter__(self):
      assert not request_session.in_transaction()

    async def __aexit__(self, *_args):
      return False

  real_refresh = cox.refresh_tokens

  async def assert_released_refresh(*args, **kwargs):
    assert not request_session.in_transaction()
    return await real_refresh(*args, **kwargs)

  monkeypatch.setattr(cox, "_refresh_lock", lambda _cid: AssertReleasedLock())
  monkeypatch.setattr(cox, "refresh_tokens", assert_released_refresh)
  try:
    fresh = await cox.usable_access_token(
      request_session, cid, generation=gen,
    )
  finally:
    request_session.close()
  assert fresh and fresh.startswith("ya29.access-")


def test_refresh_route_does_not_latch_a_replacement_signin(
  client, auth, db, monkeypatch,
):
  """A superseded refresh returning None must not mark a newer grant signed
  out merely because reauthorization kept the same connector generation."""
  from app.database import SessionLocal

  _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)

  async def replace_then_return_none(*_args, **_kwargs):
    with SessionLocal() as concurrent:
      row = concurrent.query(models.Connector).filter_by(id=cid).one()
      oauth = concurrent.query(models.ConnectorOAuth).filter_by(
        connector_id=cid,
      ).one()
      oauth.access_token_encrypted = core.encrypt_oauth("replacement-access")
      oauth.refresh_token_encrypted = core.encrypt_oauth("replacement-refresh")
      row.status = "ok"
      row.status_detail = "replacement sign-in"
      row.tools_json = ["replacement_tool"]
      concurrent.commit()
    return None

  monkeypatch.setattr(cox, "usable_access_token", replace_then_return_none)
  refreshed = client.post(
    f"/api/connectors/{cid}/refresh", headers=_headers(auth, gen),
  )
  assert refreshed.status_code == 200, refreshed.text
  body = refreshed.json()
  assert body["signed_in"] is True
  assert body["status"] == "ok"
  assert body["status_detail"] == "replacement sign-in"
  assert body["tools"] == ["replacement_tool"]
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert core.decrypt_oauth(oauth.access_token_encrypted) == "replacement-access"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_rejected", [False, True])
async def test_signed_in_probe_cannot_overwrite_a_reauthorization(
  client, auth, db, monkeypatch, auth_rejected,
):
  """Both stale success and stale 401 outcomes must lose to a newer grant."""
  from app.database import SessionLocal
  from app.routes import connectors as connector_routes

  _wire(monkeypatch, GcloudMock(project_count=1))
  cid, gen = _seed_google_connector(db)
  _complete_signin(client, auth, cid, gen)
  db.expire_all()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  old_token = core.decrypt_oauth(oauth.access_token_encrypted)

  async def reauthorize_during_probe(*_args, **_kwargs):
    with SessionLocal() as concurrent:
      row = concurrent.query(models.Connector).filter_by(id=cid).one()
      current = concurrent.query(models.ConnectorOAuth).filter_by(
        connector_id=cid,
      ).one()
      current.access_token_encrypted = core.encrypt_oauth("replacement-access")
      current.refresh_token_encrypted = core.encrypt_oauth("replacement-refresh")
      row.status = "ok"
      row.status_detail = "replacement sign-in"
      row.tools_json = ["replacement_tool"]
      row.est_tokens = 777
      concurrent.commit()
    if auth_rejected:
      raise core.ConnectorError("old grant rejected", auth_rejected=True)
    return {"tools": ["stale_tool"], "est_tokens": 1}

  monkeypatch.setattr(core, "handshake", reauthorize_during_probe)
  await connector_routes._probe_signed_in(
    GCLOUD_MCP_URL, cid, gen, old_token, "ok",
  )

  db.expire_all()
  row = db.query(models.Connector).filter_by(id=cid).one()
  oauth = db.query(models.ConnectorOAuth).filter_by(connector_id=cid).one()
  assert core.decrypt_oauth(oauth.access_token_encrypted) == "replacement-access"
  assert core.decrypt_oauth(oauth.refresh_token_encrypted) == "replacement-refresh"
  assert row.status == "ok"
  assert row.status_detail == "replacement sign-in"
  assert row.tools_json == ["replacement_tool"]
  assert row.est_tokens == 777
