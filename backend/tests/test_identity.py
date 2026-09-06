"""Identity app authorization and local-account behavior."""

import httpx
import pytest
from fastapi import HTTPException

from urllib.parse import parse_qs, urlparse

from app import models
from app.database import SessionLocal
from test_app_fixtures import create_local_app


@pytest.mark.asyncio
async def test_linked_instance_resolves_another_accounts_handle(db, monkeypatch):
  from app.routes import identity

  owner = models.Owner(username="owner", hashed_password="test")
  db.add(owner)
  db.flush()
  db.add(models.IdentityAccountLink(
    owner_id=owner.id,
    access_token_encrypted=identity._seal("linked-account-token-" + "x" * 32),
    scopes_json=["identity:read"],
  ))
  db.commit()
  request_seen = {}

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def get(self, url, *, headers):
      request_seen.update(url=url, headers=headers)
      request = httpx.Request("GET", url)
      return httpx.Response(
        200,
        json={"handle": "collaborator", "hosts": ["collaborator.example"]},
        request=request,
      )

  monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **_kwargs: Client())

  hosts = await identity.resolve_handle_hosts(db, owner.id, "@Collaborator")

  assert hosts == ["collaborator.example"]
  assert request_seen["url"].endswith("/api/account/v1/identity/handles/collaborator")
  assert request_seen["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_handle_resolution_falls_back_until_account_route_is_deployed(db, monkeypatch):
  from app.routes import identity

  owner = models.Owner(username="owner", hashed_password="test")
  db.add(owner)
  db.flush()
  db.add(models.IdentityAccountLink(
    owner_id=owner.id,
    access_token_encrypted=identity._seal("linked-account-token-" + "x" * 32),
    scopes_json=["identity:read"],
  ))
  db.commit()

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def get(self, url, *, headers):
      return httpx.Response(
        404,
        text="<!doctype html><title>Not Found</title>",
        request=httpx.Request("GET", url),
      )

  monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **_kwargs: Client())

  assert await identity.resolve_handle_hosts(db, owner.id, "collaborator") is None


@pytest.mark.asyncio
async def test_handle_resolution_preserves_explicit_missing_handle(db, monkeypatch):
  from app.routes import identity

  owner = models.Owner(username="owner", hashed_password="test")
  db.add(owner)
  db.flush()
  db.add(models.IdentityAccountLink(
    owner_id=owner.id,
    access_token_encrypted=identity._seal("linked-account-token-" + "x" * 32),
    scopes_json=["identity:read"],
  ))
  db.commit()

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def get(self, url, *, headers):
      return httpx.Response(
        404,
        json={"detail": "No one has claimed that mobius.you handle."},
        request=httpx.Request("GET", url),
      )

  monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **_kwargs: Client())

  with pytest.raises(identity.HTTPException) as exc:
    await identity.resolve_handle_hosts(db, owner.id, "collaborator")
  assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_handle_resolution_rejects_invalid_success_payload(db, monkeypatch):
  from app.routes import identity

  owner = models.Owner(username="owner", hashed_password="test")
  db.add(owner)
  db.flush()
  db.add(models.IdentityAccountLink(
    owner_id=owner.id,
    access_token_encrypted=identity._seal("linked-account-token-" + "x" * 32),
    scopes_json=["identity:read"],
  ))
  db.commit()

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def get(self, url, *, headers):
      return httpx.Response(
        200, json=["not", "a", "handle", "result"],
        request=httpx.Request("GET", url),
      )

  monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **_kwargs: Client())

  with pytest.raises(identity.HTTPException) as exc:
    await identity.resolve_handle_hosts(db, owner.id, "collaborator")
  assert exc.value.status_code == 502


def _app_auth(client, auth, *, granted: bool, railway_granted: bool = False):
  app_id = create_local_app(client, auth, name="identity-test", description="t")["id"]
  with SessionLocal() as session:
    app = session.query(models.App).filter(models.App.id == app_id).one()
    app.capability_contract = {
      "schema": 1,
      "data": {
        "identity_manage": granted,
        "railway_manage": railway_granted,
      },
    }
    session.commit()
  minted = client.post("/api/auth/app-token", json={"app_id": app_id}, headers=auth)
  assert minted.status_code == 200, minted.text
  return {"Authorization": f"Bearer {minted.json()['token']}"}


@pytest.fixture
def account_service(monkeypatch):
  """Stub the account-service HTTP client behind the identity bridge.

  ``account_service(handler)`` routes every ``client.request`` to
  ``handler(method, url, **kwargs) -> (status_code, json_payload)``.
  """
  def install(handler):
    class Response:
      def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

      def json(self):
        return self._payload

    class Client:
      def __init__(self, *args, **kwargs):
        pass

      async def __aenter__(self):
        return self

      async def __aexit__(self, *args):
        return None

      async def request(self, method, url, **kwargs):
        return Response(*handler(method, url, **kwargs))

    monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)

  return install


def _link_account(client, auth):
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
  return granted


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


def test_railway_permission_is_separate_from_identity_management(client, auth):
  identity_only = _app_auth(client, auth, granted=True)
  assert client.get("/api/identity/railway", headers=identity_only).status_code == 403

  railway = _app_auth(
    client, auth, granted=True, railway_granted=True,
  )
  response = client.get("/api/identity/railway", headers=railway)
  assert response.status_code == 200, response.text
  assert response.json() == {
    "railway_access": "signed_out",
    "connection": None,
    "instances": [],
  }


def test_linked_railway_inventory_requires_reconsent_for_legacy_grants(
  client, auth,
):
  from app.routes.identity import _seal

  granted = _app_auth(
    client, auth, granted=True, railway_granted=True,
  )
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("legacy-token-" + "x" * 40),
      scopes_json=["identity:read", "identity:write", "deployments:read"],
    ))
    session.commit()

  response = client.get("/api/identity/railway", headers=granted)

  assert response.status_code == 200
  assert response.json() == {
    "railway_access": "reconnect",
    "connection": None,
    "instances": [],
  }


def test_linked_railway_inventory_is_proxied_without_credentials(
  client, auth, monkeypatch,
):
  from app.routes.identity import _seal

  granted = _app_auth(
    client, auth, granted=True, railway_granted=True,
  )
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("railway-token-" + "x" * 40),
      scopes_json=[
        "deployments:delete", "deployments:read", "identity:read",
        "identity:write", "railway:read", "railway:write",
      ],
    ))
    session.commit()

  class Response:
    status_code = 200

    @staticmethod
    def json():
      return {
        "connection": {
          "connected": True,
          "account": "owner@example.com",
          "workspace": "Personal",
          "plan": "hobby",
          "deploy_blocked": "",
        },
        "instances": [{"id": "mob_one"}],
      }

  class Client:
    def __init__(self, *args, **kwargs):
      assert kwargs.get("follow_redirects") is False

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def get(self, url, **kwargs):
      assert url == "https://www.mobius.you/api/account/v1/railway"
      assert kwargs["headers"]["Authorization"].startswith("Bearer railway-token-")
      return Response()

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  response = client.get("/api/identity/railway", headers=granted)

  assert response.status_code == 200
  assert response.json()["railway_access"] == "available"
  assert response.json()["instances"] == [{"id": "mob_one"}]
  assert "token" not in response.text


def test_linked_railway_mutations_use_the_scoped_server_bridge(
  client, auth, monkeypatch,
):
  from app.routes.identity import _seal

  granted = _app_auth(
    client, auth, granted=True, railway_granted=True,
  )
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("railway-token-" + "x" * 40),
      scopes_json=[
        "deployments:delete", "deployments:read", "identity:read",
        "identity:write", "railway:read", "railway:write",
      ],
    ))
    session.commit()

  calls = []

  class Response:
    status_code = 202

    @staticmethod
    def json():
      return {"instance": {"id": "mob_example", "status": "queued"}}

  class ConnectResponse:
    status_code = 200

    @staticmethod
    def json():
      return {
        "authorization_url": (
          "https://www.mobius.you/railway/connect?popup=1&account_request=signed"
        ),
      }

  class Client:
    def __init__(self, *args, **kwargs):
      assert kwargs.get("follow_redirects") is False

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def request(self, method, url, **kwargs):
      calls.append((method, url, kwargs.get("json")))
      assert kwargs["headers"]["Authorization"].startswith("Bearer railway-token-")
      return Response()

    async def post(self, url, **kwargs):
      assert url == "https://www.mobius.you/api/account/v1/railway/connect/start"
      assert kwargs["headers"]["Authorization"].startswith("Bearer railway-token-")
      return ConnectResponse()

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  connect = client.post("/api/identity/railway/connect/start", headers=granted)
  created = client.post(
    "/api/identity/railway/deployments",
    json={
      "name": "Writing room",
      "managed_auth": True,
      "cpu": None,
      "memory_mb": None,
      "volume_mb": None,
      "update_policy": "manual",
    },
    headers=granted,
  )
  deleted = client.delete(
    "/api/identity/railway/deployments/mob_example", headers=granted,
  )
  storage = client.patch(
    "/api/identity/railway/deployments/mob_example/storage",
    json={"volume_mb": 2048},
    headers=granted,
  )
  updates = client.patch(
    "/api/identity/railway/deployments/mob_example/updates",
    json={"update_policy": "manual"},
    headers=granted,
  )

  assert connect.status_code == 200
  assert connect.json()["authorization_url"].startswith("https://www.mobius.you/")
  assert created.status_code == 202
  assert deleted.status_code == 202
  assert storage.status_code == 200
  assert updates.status_code == 202
  assert calls == [
    (
      "POST",
      "https://www.mobius.you/api/account/v1/railway/instances",
      {
        "name": "Writing room",
        "managed_auth": True,
        "cpu": None,
        "memory_mb": None,
        "volume_mb": None,
        "update_policy": "manual",
      },
    ),
    (
      "DELETE",
      "https://www.mobius.you/api/account/v1/railway/instances/mob_example",
      None,
    ),
    (
      "PATCH",
      "https://www.mobius.you/api/account/v1/railway/instances/mob_example/storage",
      {"volume_mb": 2048},
    ),
    (
      "PATCH",
      "https://www.mobius.you/api/account/v1/railway/instances/mob_example/updates",
      {"update_policy": "manual"},
    ),
  ]


def test_unrelated_railway_deployments_cannot_be_imported(client, auth):
  granted = _app_auth(
    client, auth, granted=True, railway_granted=True,
  )

  response = client.post(
    "/api/identity/railway/deployments/adopt-current", headers=granted,
  )

  assert response.status_code == 404


def test_linked_railway_deletion_recovery_requires_fresh_bounded_state(
  client, auth, monkeypatch,
):
  from app.routes.identity import _seal

  granted = _app_auth(
    client, auth, granted=True, railway_granted=True,
  )
  with SessionLocal() as session:
    owner = session.query(models.Owner).one()
    session.add(models.IdentityAccountLink(
      owner_id=owner.id,
      access_token_encrypted=_seal("railway-token-" + "x" * 40),
      scopes_json=[
        "deployments:delete", "deployments:read", "identity:read",
        "identity:write", "railway:read", "railway:write",
      ],
    ))
    session.commit()

  calls = []

  class Response:
    status_code = 200

    def __init__(self, payload):
      self.payload = payload

    def json(self):
      return self.payload

  class Client:
    def __init__(self, *args, **kwargs):
      assert kwargs.get("follow_redirects") is False

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def request(self, method, url, **kwargs):
      calls.append((method, url, kwargs.get("json")))
      assert kwargs["headers"]["Authorization"].startswith("Bearer railway-token-")
      if method == "GET":
        return Response({
          "state": "missing_unconfirmed",
          "message": "Railway says this project may already be gone.",
          "can_confirm_absent": True,
        })
      return Response({"instance": {"id": "mob_example", "status": "deleted"}})

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  diagnosis = client.get(
    "/api/identity/railway/deployments/mob_example/deletion", headers=granted,
  )
  rejected = client.post(
    "/api/identity/railway/deployments/mob_example/confirm-absent",
    json={"confirmed_absent": False},
    headers=granted,
  )
  confirmed = client.post(
    "/api/identity/railway/deployments/mob_example/confirm-absent",
    json={"confirmed_absent": True},
    headers=granted,
  )

  assert diagnosis.status_code == 200
  assert diagnosis.json()["state"] == "missing_unconfirmed"
  assert diagnosis.json()["can_confirm_absent"] is True
  assert rejected.status_code == 422
  assert confirmed.status_code == 200
  assert confirmed.json()["instance"]["status"] == "deleted"
  assert calls == [
    (
      "GET",
      "https://www.mobius.you/api/account/v1/railway/instances/mob_example/deletion",
      None,
    ),
    (
      "POST",
      "https://www.mobius.you/api/account/v1/railway/instances/mob_example/confirm-absent",
      {"confirmed_absent": True},
    ),
  ]


def test_railway_deletion_recovery_rejects_unbounded_upstream_state():
  from app.routes.identity import _railway_deletion_contract
  from fastapi import HTTPException

  invalid = {
    "state": "provider-invented-state",
    "message": "x",
    "can_confirm_absent": True,
  }
  try:
    _railway_deletion_contract(invalid)
  except HTTPException as exc:
    assert exc.status_code == 502
  else:
    raise AssertionError("invalid deletion state was accepted")


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
  broker_calls = []

  async def broker_request(method, route, payload=None, *, timeout=10.0):
    broker_calls.append((method, route, payload, timeout))
    if (method, route) == ("GET", "/identity"):
      return {
        "linked": False,
        "instance_id": "mob_self_test",
        "public_key_jwk": {"kty": "OKP", "crv": "Ed25519", "x": "key"},
        "key_thumbprint": "a" * 64,
      }
    assert (method, route) == ("POST", "/identity/enroll")
    assert payload == {"receipt": "header.payload.signature"}
    return {
      "linked": True,
      "subject": "usr_123",
      "instance_id": "mob_self_test",
    }

  monkeypatch.setattr(
    "app.routes.identity.runtime_identity_broker_request", broker_request,
  )

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
      assert kwargs["json"]["runtime_identity"] == {
        "instance_id": "mob_self_test",
        "public_key_jwk": {"kty": "OKP", "crv": "Ed25519", "x": "key"},
        "key_thumbprint": "a" * 64,
      }
      return Response(200, {
        "access_token": plain_token,
        "token_type": "Bearer",
        "scope": (
          "deployments:delete deployments:read identity:read identity:write "
          "railway:read railway:write"
        ),
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
        "enrollment_receipt": "header.payload.signature",
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
  # The local link row is the only truthful "since" date the account card can
  # show (the remote profile carries no creation date), so linked responses
  # must expose the UTC link instant.
  assert isinstance(body["linked_at"], str) and body["linked_at"].endswith("Z")
  assert [call[:2] for call in broker_calls] == [
    ("GET", "/identity"),
    ("POST", "/identity/enroll"),
  ]

  with SessionLocal() as session:
    assert session.query(models.IdentityLinkAttempt).count() == 0
    link = session.query(models.IdentityAccountLink).one()
    assert plain_token not in link.access_token_encrypted
    assert link.scopes_json == [
      "deployments:delete", "deployments:read", "identity:read",
      "identity:write", "railway:read", "railway:write",
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

  async def broker_request(method, route, payload=None, *, timeout=10.0):
    assert (method, route, payload, timeout) == (
      "GET", "/identity", None, 10.0,
    )
    return {
      "linked": False,
      "instance_id": "mob_self_test",
      "public_key_jwk": {"kty": "OKP", "crv": "Ed25519", "x": "key"},
      "key_thumbprint": "a" * 64,
    }

  monkeypatch.setattr(
    "app.routes.identity.runtime_identity_broker_request", broker_request,
  )

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


def test_linked_avatar_is_proxied_through_the_authenticated_bridge(
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

  class IdentityResponse:
    status_code = 200

    @staticmethod
    def json():
      return {
        "profile": {
          "user_id": "usr_123",
          "email": "owner@example.com",
          "display_name": "Owner",
          "handle": "owner",
          "avatar_url": "https://www.mobius.you/api/account/v1/avatars/key/256.webp",
        },
        "deployments": [],
      }

  class AvatarResponse:
    status_code = 200
    headers = {"content-type": "image/png"}

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def aiter_bytes(self):
      yield b"safe-avatar-bytes"

  class Client:
    def __init__(self, *args, **kwargs):
      assert kwargs.get("follow_redirects") is False

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def request(self, method, url, **kwargs):
      assert method == "GET"
      assert url.endswith("/api/account/v1/identity")
      return IdentityResponse()

    def stream(self, method, url, **kwargs):
      assert method == "GET"
      assert url == "https://www.mobius.you/api/account/v1/avatars/key/256.webp"
      return AvatarResponse()

  monkeypatch.setattr("app.routes.identity.httpx.AsyncClient", Client)
  response = client.get("/api/identity/avatar", headers=granted)

  assert response.status_code == 200
  assert response.content == b"safe-avatar-bytes"
  assert response.headers["content-type"] == "image/png"
  assert response.headers["cache-control"] == "private, max-age=300"
  assert response.headers["x-content-type-options"] == "nosniff"


def test_avatar_proxy_rejects_unsafe_remote_urls():
  from app.routes.identity import _remote_avatar_url

  assert _remote_avatar_url({
    "profile": {"avatar_url": "https://www.mobius.you/avatar.webp"},
  }) == "https://www.mobius.you/avatar.webp"
  assert _remote_avatar_url({
    "profile": {"avatar_url": "https://lh3.googleusercontent.com/avatar"},
  }) == "https://lh3.googleusercontent.com/avatar"
  for value in (
    "http://images.example/avatar.webp",
    "https://user:pass@images.example/avatar.webp",
    "https://images.example:444/avatar.webp",
    "https://images.example/avatar.webp#fragment",
    "https://private.example/avatar.webp",
  ):
    assert _remote_avatar_url({"profile": {"avatar_url": value}}) is None


@pytest.mark.parametrize(
  ("remote_status", "remote_detail"),
  (
    (413, "Profile pictures must be 5 MB or smaller."),
    (415, "Choose a valid JPEG, PNG, or WebP image."),
    (422, "Send one avatar field."),
  ),
)
def test_linked_avatar_preserves_account_validation_errors(
  client, auth, account_service, remote_status, remote_detail,
):
  granted = _link_account(client, auth)

  def handler(method, url, **kwargs):
    assert method == "POST"
    assert url == "https://www.mobius.you/api/account/v1/identity/avatar"
    assert kwargs["files"]["avatar"][2] == "image/png"
    return remote_status, {"detail": remote_detail}

  account_service(handler)
  response = client.post(
    "/api/identity/avatar",
    files={"avatar": ("avatar.png", b"image-bytes", "image/png")},
    headers=granted,
  )

  assert response.status_code == remote_status
  assert response.json() == {"detail": remote_detail}


@pytest.mark.parametrize("detail", [None, "", "x" * 501, ["not", "text"]])
def test_remote_validation_error_uses_bounded_fallback(detail):
  from app.routes.identity import _raise_remote_request_error

  class Response:
    status_code = 415

    @staticmethod
    def json():
      return {"detail": detail}

  with pytest.raises(HTTPException) as exc:
    _raise_remote_request_error(Response())

  assert exc.value.status_code == 415
  assert exc.value.detail == "Choose a JPEG, PNG, or WebP image."


def test_remote_server_errors_collapse_to_bad_gateway():
  from app.routes.identity import _raise_remote_request_error

  class Response:
    status_code = 500

    @staticmethod
    def json():
      return {"detail": "Internal account-service stack trace."}

  with pytest.raises(HTTPException) as exc:
    _raise_remote_request_error(Response())

  assert exc.value.status_code == 502
  assert exc.value.detail == (
    "The Möbius account service could not complete that request."
  )


def test_linked_profile_mutation_surfaces_account_rejection(
  client, auth, account_service,
):
  granted = _link_account(client, auth)
  account_service(lambda method, url, **kwargs: (422, {"detail": "Handle is reserved."}))

  response = client.patch(
    "/api/identity/profile", json={"handle": "reserved"}, headers=granted,
  )

  assert response.status_code == 422
  assert response.json() == {"detail": "Handle is reserved."}


def test_linked_profile_mutation_uses_the_authoritative_response_once(
  client, auth, account_service,
):
  granted = _link_account(client, auth)
  calls = []

  def handler(method, url, **kwargs):
    calls.append((method, url, kwargs.get("json")))
    return 200, {
      "profile": {
        "user_id": "usr_123",
        "email": "owner@example.com",
        "display_name": "Owner",
        "handle": "new_handle",
        "avatar_url": None,
      },
      "deployments": [],
    }

  account_service(handler)
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
