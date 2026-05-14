"""Tests for app-scoped token creation and enforcement."""

import io


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


def _make_app_and_token(client, owner_token):
  """Helper: creates an app and returns (app_id, app_token)."""
  r = client.post("/api/apps/", json={
    "name": "test-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]
  r = client.post("/api/auth/app-token", json={
    "app_id": app_id,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  return app_id, r.json()["token"]


def test_app_token_cannot_write_shared_storage(client, owner_token):
  _, app_token = _make_app_and_token(client, owner_token)
  r = client.put(
    "/api/storage/shared/test.txt",
    json={"content": "injected"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403


def test_app_token_cannot_delete_shared_storage(client, owner_token):
  # Owner writes a file, then app token tries to delete it.
  client.put(
    "/api/storage/shared/victim.txt",
    json={"content": "important"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  _, app_token = _make_app_and_token(client, owner_token)
  r = client.delete(
    "/api/storage/shared/victim.txt",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403


def test_app_token_can_read_shared_storage(client, owner_token):
  client.put(
    "/api/storage/shared/readable.txt",
    json={"content": "hello"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  _, app_token = _make_app_and_token(client, owner_token)
  r = client.get(
    "/api/storage/shared/readable.txt",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200
  assert "hello" in r.text


def test_app_token_can_access_cross_app_storage(client, owner_token):
  # Create two apps — token for app 1 should access app 2's storage.
  app_id_1, app_token_1 = _make_app_and_token(client, owner_token)
  r = client.post("/api/apps/", json={
    "name": "other-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>2</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id_2 = r.json()["id"]

  # Owner writes to app 2.
  client.put(
    f"/api/storage/apps/{app_id_2}/data.json",
    json={"content": '{"shared": true}'},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  # App 1's token can read app 2's storage (sharing by design).
  r = client.get(
    f"/api/storage/apps/{app_id_2}/data.json",
    headers={"Authorization": f"Bearer {app_token_1}"},
  )
  assert r.status_code == 200


def test_app_token_cannot_upload_to_chat(client, owner_token, db):
  """App tokens must not be able to upload files to chats."""
  from app import models
  chat = models.Chat(id="upload-test", title="test", messages=[])
  db.add(chat)
  db.commit()

  _, app_token = _make_app_and_token(client, owner_token)
  r = client.post(
    "/api/chats/upload-test/uploads",
    files=[("files", ("evil.txt", io.BytesIO(b"payload"), "text/plain"))],
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403


def test_app_token_cannot_delete_upload(client, owner_token, db):
  """App tokens must not be able to delete chat uploads."""
  from app import models
  chat = models.Chat(id="del-test", title="test", messages=[])
  db.add(chat)
  db.commit()

  # Owner uploads a file.
  client.post(
    "/api/chats/del-test/uploads",
    files=[("files", ("keep.txt", io.BytesIO(b"keep"), "text/plain"))],
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  _, app_token = _make_app_and_token(client, owner_token)
  r = client.delete(
    "/api/chats/del-test/uploads/keep.txt",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403


def test_app_token_can_list_shared_storage(client, owner_token):
  """App tokens can list shared directory contents (read-only)."""
  # Create a subdirectory with a file so we have something to list.
  client.put(
    "/api/storage/shared/listtest/listed.txt",
    json={"content": "visible"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  _, app_token = _make_app_and_token(client, owner_token)
  r = client.get(
    "/api/storage/shared-list/listtest",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200
  names = [e["name"] for e in r.json()]
  assert "listed.txt" in names
