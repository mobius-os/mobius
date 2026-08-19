"""Identity app authorization and local-account behavior."""

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
  assert body["profile"]["display_name"] == "test"
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
