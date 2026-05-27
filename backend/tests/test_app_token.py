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


def _create_app(client, owner_token, name, share=None):
  body = {
    "name": name,
    "description": "test",
    "jsx_source": "export default function App() { return <div>x</div> }",
  }
  if share is not None:
    body["share_with_apps"] = share
  r = client.post(
    "/api/apps/", json=body,
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  return r.json()["id"]


def test_cross_app_storage_default_is_none(client, owner_token):
  """Default share_with_apps='none' → other apps get 403."""
  _, app_token_1 = _make_app_and_token(client, owner_token)
  app_id_2 = _create_app(client, owner_token, "other-app")
  client.put(
    f"/api/storage/apps/{app_id_2}/data.json",
    json={"content": '{"shared": true}'},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps/{app_id_2}/data.json",
    headers={"Authorization": f"Bearer {app_token_1}"},
  )
  assert r.status_code == 403


def test_cross_app_storage_read_allows_get_blocks_write(client, owner_token):
  """share_with_apps='read' → other apps GET succeeds, PUT/DELETE 403."""
  _, app_token_1 = _make_app_and_token(client, owner_token)
  app_id_2 = _create_app(client, owner_token, "readable-app", share="read")
  client.put(
    f"/api/storage/apps/{app_id_2}/data.json",
    json={"content": '{"shared": true}'},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps/{app_id_2}/data.json",
    headers={"Authorization": f"Bearer {app_token_1}"},
  )
  assert r.status_code == 200
  r = client.put(
    f"/api/storage/apps/{app_id_2}/data.json",
    json={"content": '{"tampered": true}'},
    headers={"Authorization": f"Bearer {app_token_1}"},
  )
  assert r.status_code == 403


def test_cross_app_storage_write_allows_everything(client, owner_token):
  """share_with_apps='write' → other apps can GET / PUT / DELETE."""
  _, app_token_1 = _make_app_and_token(client, owner_token)
  app_id_2 = _create_app(client, owner_token, "writable-app", share="write")
  r = client.put(
    f"/api/storage/apps/{app_id_2}/data.json",
    json={"content": '{"from_other": true}'},
    headers={"Authorization": f"Bearer {app_token_1}"},
  )
  assert r.status_code == 204
  r = client.get(
    f"/api/storage/apps/{app_id_2}/data.json",
    headers={"Authorization": f"Bearer {app_token_1}"},
  )
  assert r.status_code == 200


def test_app_token_can_access_own_storage_regardless_of_share(
  client, owner_token,
):
  """Own-app storage is always full-access for that app's token."""
  app_id, app_token = _make_app_and_token(client, owner_token)
  # share_with_apps stays 'none' by default — still works for own data.
  r = client.put(
    f"/api/storage/apps/{app_id}/mine.json",
    json={"content": '{"k": 1}'},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 204
  r = client.get(
    f"/api/storage/apps/{app_id}/mine.json",
    headers={"Authorization": f"Bearer {app_token}"},
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
