"""The light platform projects registry (named project workspaces).

Registry only: list/create/rename/remove of {id, name, ...}, scoped to the
calling app. No files/build/publish here.
"""

from test_app_fixtures import create_local_app


def _app(client, owner_token, name="Studio"):
  app = create_local_app(client, {"Authorization": f"Bearer {owner_token}"}, name=name)
  tok = client.post(
    "/api/auth/app-token", json={"app_id": app["id"]},
    headers={"Authorization": f"Bearer {owner_token}"},
  ).json()["token"]
  return app, {"Authorization": f"Bearer {tok}"}


def test_crud(client, owner_token, db):
  app, h = _app(client, owner_token)
  assert client.get("/api/projects", headers=h).json() == []

  # Auto id.
  a = client.post("/api/projects", json={"name": "Site A"}, headers=h)
  assert a.status_code == 201, a.text
  aid = a.json()["id"]
  assert a.json()["name"] == "Site A"

  # Fixed id, idempotent.
  d1 = client.post("/api/projects", json={"name": "Project 1", "id": "default"}, headers=h).json()
  d2 = client.post("/api/projects", json={"name": "Renamed?", "id": "default"}, headers=h).json()
  assert d1["id"] == "default" and d2["id"] == "default"
  assert d2["name"] == "Project 1"  # idempotent — not overwritten

  ids = {p["id"] for p in client.get("/api/projects", headers=h).json()}
  assert ids == {aid, "default"}

  # Rename.
  r = client.patch(f"/api/projects/{aid}", json={"name": "Site A2"}, headers=h)
  assert r.status_code == 200 and r.json()["name"] == "Site A2"

  # Remove.
  assert client.delete(f"/api/projects/{aid}", headers=h).json()["removed"] is True
  assert {p["id"] for p in client.get("/api/projects", headers=h).json()} == {"default"}
  assert client.delete(f"/api/projects/{aid}", headers=h).status_code == 404


def test_scoped_per_app(client, owner_token, db):
  app1, h1 = _app(client, owner_token, name="One")
  app2, h2 = _app(client, owner_token, name="Two")
  client.post("/api/projects", json={"name": "p1"}, headers=h1)
  # app2 sees only its own (empty) list.
  assert client.get("/api/projects", headers=h2).json() == []


def test_owner_needs_app_id(client, owner_token, db):
  owner_h = {"Authorization": f"Bearer {owner_token}"}
  # Bare owner token has no app scope.
  assert client.get("/api/projects", headers=owner_h).status_code == 403
  app, _ = _app(client, owner_token)
  # Owner may name the app explicitly.
  assert client.get(f"/api/projects?app_id={app['id']}", headers=owner_h).status_code == 200


def test_invalid_id_rejected(client, owner_token, db):
  _, h = _app(client, owner_token)
  assert client.post("/api/projects", json={"name": "x", "id": "../../etc"}, headers=h).status_code == 422
