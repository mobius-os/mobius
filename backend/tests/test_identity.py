"""Identity app authorization and local-account behavior."""

from urllib.parse import parse_qs, urlparse

from app import models
from app.database import SessionLocal
from test_app_fixtures import create_local_app


def _app_auth(client, auth, *, granted: bool):
  app_id = create_local_app(client, auth, name="identity-test", description="t")["id"]
  with SessionLocal() as session:
    app = session.query(models.App).filter(models.App.id == app_id).one()
    app.capability_contract = {
      "schema": 1,
      "data": {"identity_manage": granted},
    }
    session.commit()
  minted = client.post("/api/auth/app-token", json={"app_id": app_id}, headers=auth)
  assert minted.status_code == 200, minted.text
  return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_identity_app_requires_reviewed_capability(client, auth):
  denied = _app_auth(client, auth, granted=False)
  assert client.get("/api/identity", headers=denied).status_code == 403

  granted = _app_auth(client, auth, granted=True)
  response = client.get("/api/identity", headers=granted)
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["managed"] is False
  assert body["profile"] == {
    "user_id": None,
    "email": None,
    "display_name": None,
    "username": None,
    "handle": None,
    "avatar_url": None,
  }
  assert "test" not in response.text
  assert body["deployments"][0]["current"] is True


def test_identity_permission_is_part_of_review_contract():
  from app.app_capabilities import contract_from_app_state, contract_from_manifest

  contract = contract_from_manifest({
    "permissions": {"identity_manage": True},
  })
  assert contract["data"]["identity_manage"] is True
  local = contract_from_app_state(
    models.App(name="Identity", slug="identity", source_dir="/tmp/identity"),
    contract_permissions={"identity_manage": True},
  )
  assert local["data"]["identity_manage"] is True


def test_link_start_keeps_pkce_verifier_server_side_and_supersedes(client, auth):
  granted = _app_auth(client, auth, granted=True)

  first = client.post(
    "/api/identity/link/start", json={"provider": "google"}, headers=granted,
  )
  assert first.status_code == 200, first.text
  first_body = first.json()
  query = parse_qs(urlparse(first_body["authorization_url"]).query)
  assert query["provider"] == ["google"]
  assert query["code_challenge_method"] == ["S256"]
  assert query["state"] == [first_body["state"]]
  assert "code_verifier" not in first.text

  second = client.post(
    "/api/identity/link/start", json={"provider": "apple"}, headers=granted,
  )
  assert second.status_code == 200, second.text
  with SessionLocal() as session:
    rows = session.query(models.IdentityLinkAttempt).all()
    assert len(rows) == 1
    assert rows[0].attempt_id == second.json()["attempt"]
    assert first_body["state"] not in rows[0].state_digest
    assert "code_verifier" not in rows[0].verifier_encrypted


def test_link_complete_consumes_attempt_and_stores_encrypted_grant(
  client, auth, monkeypatch,
):
  granted = _app_auth(client, auth, granted=True)
  started = client.post(
    "/api/identity/link/start", json={"provider": "google"}, headers=granted,
  ).json()
  plain_token = "account-token-" + "x" * 40

  class Response:
    def __init__(self, status_code, body=None):
      self.status_code = status_code
      self._body = body

    def json(self):
      return self._body

  class Client:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def post(self, url, **kwargs):
      assert url.endswith("/api/account-links/token")
      assert len(kwargs["json"]["code_verifier"]) >= 43
      return Response(200, {
        "access_token": plain_token,
        "token_type": "Bearer",
        "scope": "identity:read identity:write deployments:read",
      })

    async def request(self, method, url, **kwargs):
      assert method == "GET"
      assert kwargs["headers"]["Authorization"] == f"Bearer {plain_token}"
      return Response(200, {
        "profile": {
          "user_id": "usr_123",
          "email": "owner@example.com",
          "display_name": "Owner",
          "handle": "owner",
          "avatar_url": None,
        },
        "deployments": [{
          "id": "remote", "name": "Production", "status": "Active",
          "url": "https://example.com", "current": False,
        }],
      })

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  completed = client.post("/api/identity/link/complete", json={
    "code": "one-use-code-" + "c" * 32,
    "state": started["state"],
    "attempt": started["attempt"],
  }, headers=granted)
  assert completed.status_code == 200, completed.text
  body = completed.json()
  assert body["profile"]["user_id"] == "usr_123"
  assert [item["id"] for item in body["deployments"]] == ["remote", "local"]

  with SessionLocal() as session:
    assert session.query(models.IdentityLinkAttempt).count() == 0
    link = session.query(models.IdentityAccountLink).one()
    assert plain_token not in link.access_token_encrypted
    assert link.scopes_json == [
      "identity:read", "identity:write", "deployments:read",
    ]

  replay = client.post("/api/identity/link/complete", json={
    "code": "one-use-code-" + "c" * 32,
    "state": started["state"],
    "attempt": started["attempt"],
  }, headers=granted)
  assert replay.status_code == 400
