"""Identity app authorization and local-account behavior."""

import httpx

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
  assert body["account_mode"] == "signed_out"
  assert body["account_unavailable"] is False
  assert body["profile"] is None
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


def test_link_start_keeps_pkce_verifier_server_side_and_supersedes(
  client, auth,
):
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
  assert query["client_origin"] == ["http://localhost:5173"]
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


def test_link_start_does_not_create_a_throwaway_server_side_oauth_flow(
  client, auth, monkeypatch,
):
  class UnexpectedAuthorizationClient:
    def __init__(self, *args, **kwargs):
      raise AssertionError("link start must not call the authorization URL")

  monkeypatch.setattr(
    "app.routes.identity.httpx.AsyncClient", UnexpectedAuthorizationClient,
  )
  granted = _app_auth(client, auth, granted=True)
  response = client.post(
    "/api/identity/link/start", json={"provider": "google"}, headers=granted,
  )
  assert response.status_code == 200
  with SessionLocal() as session:
    assert session.query(models.IdentityLinkAttempt).count() == 1


def test_link_complete_consumes_attempt_and_stores_encrypted_grant(
  client, auth, monkeypatch,
):
  granted = _app_auth(client, auth, granted=True)
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
        "identity": {
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
        },
      })

    async def get(self, url, **kwargs):
      assert url.startswith("https://www.mobius.you/connect/mobius?")
      return Response(200)

    async def request(self, method, url, **kwargs):
      raise AssertionError("completion must not need a second remote request")

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  started = client.post(
    "/api/identity/link/start", json={"provider": "google"}, headers=granted,
  ).json()
  completed = client.post("/api/identity/link/complete", json={
    "code": "one-use-code-" + "c" * 32,
    "state": started["state"],
    "attempt": started["attempt"],
  }, headers=granted)
  assert completed.status_code == 200, completed.text
  body = completed.json()
  assert body["account_mode"] == "linked"
  assert body["profile"]["user_id"] == "usr_123"
  assert [item["id"] for item in body["deployments"]] == ["remote", "local"]

  with SessionLocal() as session:
    assert session.query(models.IdentityLinkAttempt).count() == 0
    link = session.query(models.IdentityAccountLink).one()
    assert plain_token not in link.access_token_encrypted
    assert link.scopes_json == [
      "identity:read", "identity:write", "deployments:read",
    ]

  replacement = client.post(
    "/api/identity/link/start", json={"provider": "apple"}, headers=granted,
  )
  assert replacement.status_code == 409
  assert replacement.json()["detail"] == (
    "Disconnect the current account before linking another."
  )

  replay = client.post("/api/identity/link/complete", json={
    "code": "one-use-code-" + "c" * 32,
    "state": started["state"],
    "attempt": started["attempt"],
  }, headers=granted)
  assert replay.status_code == 400


def test_unlink_removes_local_link_when_remote_grant_is_already_gone(
  client, auth, monkeypatch,
):
  from app.routes.identity import _seal

  granted = _app_auth(client, auth, granted=True)
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("expired-token-" + "x" * 40),
      scopes_json=["identity:read", "identity:write", "deployments:read"],
    ))
    session.commit()

  class Client:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def post(self, url, **kwargs):
      assert url == "https://www.mobius.you/api/account-links/revoke"
      return type("Response", (), {"status_code": 401})()

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  response = client.delete("/api/identity/link", headers=granted)

  assert response.status_code == 204
  with SessionLocal() as session:
    assert session.query(models.IdentityAccountLink).count() == 0


def test_link_completion_keeps_retry_state_when_host_response_is_lost(
  client, auth, monkeypatch,
):
  granted = _app_auth(client, auth, granted=True)

  class Client:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def get(self, url, **kwargs):
      return type("Response", (), {"status_code": 200})()

    async def post(self, url, **kwargs):
      raise httpx.ConnectError("response lost")

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  started = client.post(
    "/api/identity/link/start", json={"provider": "google"}, headers=granted,
  ).json()
  response = client.post("/api/identity/link/complete", json={
    "code": "one-use-code-" + "c" * 32,
    "state": started["state"],
    "attempt": started["attempt"],
  }, headers=granted)

  assert response.status_code == 502
  with SessionLocal() as session:
    assert session.query(models.IdentityLinkAttempt).count() == 1
    assert session.query(models.IdentityAccountLink).count() == 0


def test_corrupt_local_grant_self_heals_to_signed_out(client, auth):
  granted = _app_auth(client, auth, granted=True)
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted="not-a-fernet-token",
      scopes_json=["identity:read"],
    ))
    session.commit()

  response = client.get("/api/identity", headers=granted)

  assert response.status_code == 200
  assert response.json()["account_mode"] == "signed_out"
  with SessionLocal() as session:
    assert session.query(models.IdentityAccountLink).count() == 0


def test_linked_host_outage_preserves_local_deployment(client, auth, monkeypatch):
  from app.routes.identity import _seal

  granted = _app_auth(client, auth, granted=True)
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("linked-token-" + "x" * 40),
      scopes_json=["identity:read", "identity:write", "deployments:read"],
    ))
    session.commit()

  class Client:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def request(self, method, url, **kwargs):
      raise httpx.ConnectError("account host unavailable")

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  response = client.get("/api/identity", headers=granted)

  assert response.status_code == 200
  body = response.json()
  assert body["account_mode"] == "linked"
  assert body["account_unavailable"] is True
  assert body["profile"] is None
  assert body["deployments"] == [{
    "id": "local",
    "name": "This Möbius",
    "status": "Active",
    "current": True,
    "url": "http://localhost:5173",
  }]


def test_linked_profile_mutation_uses_the_authoritative_response_once(
  client, auth, monkeypatch,
):
  from app.routes.identity import _seal

  granted = _app_auth(client, auth, granted=True)
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("linked-token-" + "x" * 40),
      scopes_json=["identity:read", "identity:write", "deployments:read"],
    ))
    session.commit()

  calls = []

  class Response:
    status_code = 200

    @staticmethod
    def json():
      return {
        "profile": {
          "user_id": "usr_123",
          "email": "owner@example.com",
          "display_name": "Owner",
          "handle": "new_handle",
          "avatar_url": None,
        },
        "deployments": [],
      }

  class Client:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def request(self, method, url, **kwargs):
      calls.append((method, url, kwargs.get("json")))
      return Response()

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  response = client.patch(
    "/api/identity/profile",
    json={"handle": "NEW_HANDLE"},
    headers=granted,
  )

  assert response.status_code == 200, response.text
  assert response.json()["account_mode"] == "linked"
  assert response.json()["profile"]["handle"] == "new_handle"
  assert calls == [(
    "PATCH",
    "https://www.mobius.you/api/account/v1/identity/profile",
    {"handle": "new_handle"},
  )]
