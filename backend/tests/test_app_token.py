"""Tests for app-scoped token creation and enforcement."""


def test_create_app_token(client, owner_token):
  # First create an app.
  r = client.post("/api/apps/", json={
    "name": "test-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]

  # Request scoped token.
  r = client.post("/api/auth/app-token", json={
    "app_id": app_id,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 200
  assert "token" in r.json()


def test_app_token_cannot_access_settings(client, owner_token):
  r = client.post("/api/apps/", json={
    "name": "test-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.post("/api/auth/app-token", json={
    "app_id": app_id,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_token = r.json()["token"]

  # App token should be rejected by settings endpoint.
  r = client.get("/api/settings", headers={
    "Authorization": f"Bearer {app_token}",
  })
  assert r.status_code == 403


def test_app_token_can_access_storage(client, owner_token):
  r = client.post("/api/apps/", json={
    "name": "test-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.post("/api/auth/app-token", json={
    "app_id": app_id,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_token = r.json()["token"]

  # App token should be accepted by storage endpoints.
  r = client.put(
    f"/api/storage/apps/{app_id}/test.json",
    json={"content": '{"hello": "world"}'},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 204


def test_app_token_cannot_create_apps(client, owner_token):
  # Create an app first to get a valid app token.
  r = client.post("/api/apps/", json={
    "name": "test-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.post("/api/auth/app-token", json={
    "app_id": app_id,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_token = r.json()["token"]

  # App token should not be able to create new apps.
  r = client.post("/api/apps/", json={
    "name": "another-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>no</div> }",
  }, headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 403


def test_app_token_requires_auth(client):
  # Requesting an app token without authentication should fail.
  r = client.post("/api/auth/app-token", json={"app_id": 1})
  assert r.status_code == 401


def test_app_token_invalid_app(client, owner_token):
  # Requesting a token for a non-existent app should fail.
  r = client.post("/api/auth/app-token", json={
    "app_id": 9999,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 404
