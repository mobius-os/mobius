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


def _create_app(client, owner_token, name, cross=None, share=None):
  body = {
    "name": name,
    "description": "test",
    "jsx_source": "export default function App() { return <div>x</div> }",
  }
  if cross is not None:
    body["cross_app_access"] = cross
  if share is not None:
    body["share_with_apps"] = share
  r = client.post(
    "/api/apps/", json=body,
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  return r.json()["id"]


def _make_app_token(client, owner_token, app_id):
  r = client.post(
    "/api/auth/app-token", json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  return r.json()["token"]


def test_cross_app_storage_blocked_when_caller_cross_app_access_is_none(
  client, owner_token,
):
  """Subject-side: caller defaults to 'none' → blocked even if target shares."""
  caller_id = _create_app(client, owner_token, "caller")  # cross='none'
  caller_token = _make_app_token(client, owner_token, caller_id)
  target_id = _create_app(client, owner_token, "target", share="write")
  client.put(
    f"/api/storage/apps/{target_id}/data.json",
    json={"content": '{"k": 1}'},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps/{target_id}/data.json",
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 403


def test_cross_app_storage_blocked_when_target_share_is_none(
  client, owner_token,
):
  """Object-side: target defaults to 'none' → blocked even if caller is open."""
  caller_id = _create_app(client, owner_token, "caller", cross="write")
  caller_token = _make_app_token(client, owner_token, caller_id)
  target_id = _create_app(client, owner_token, "target")  # share='none'
  client.put(
    f"/api/storage/apps/{target_id}/data.json",
    json={"content": '{"k": 1}'},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps/{target_id}/data.json",
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 403


def test_cross_app_storage_read_when_both_sides_permit(client, owner_token):
  """Both sides 'read' (or higher) → GET succeeds, PUT blocked."""
  caller_id = _create_app(client, owner_token, "caller", cross="read")
  caller_token = _make_app_token(client, owner_token, caller_id)
  target_id = _create_app(client, owner_token, "target", share="read")
  client.put(
    f"/api/storage/apps/{target_id}/data.json",
    json={"content": '{"k": 1}'},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps/{target_id}/data.json",
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 200
  r = client.put(
    f"/api/storage/apps/{target_id}/data.json",
    json={"content": '{"tampered": true}'},
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 403


def test_cross_app_storage_write_when_both_sides_permit(client, owner_token):
  """Both sides 'write' → GET / PUT / DELETE all succeed."""
  caller_id = _create_app(client, owner_token, "caller", cross="write")
  caller_token = _make_app_token(client, owner_token, caller_id)
  target_id = _create_app(client, owner_token, "target", share="write")
  r = client.put(
    f"/api/storage/apps/{target_id}/data.json",
    json={"content": '{"k": 1}'},
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 204
  r = client.get(
    f"/api/storage/apps/{target_id}/data.json",
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 200


def test_app_token_can_access_own_storage_regardless_of_share(
  client, owner_token,
):
  """Own-app storage is always full-access for that app's token."""
  app_id, app_token = _make_app_and_token(client, owner_token)
  # Both fields stay 'none' by default — still works for own data.
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
  # shared-list now returns the same {entries, next_cursor} shape as
  # apps-list (Codex review #10), each entry a full _list_entry.
  body = r.json()
  names = [e["name"] for e in body["entries"]]
  assert "listed.txt" in names
  assert "next_cursor" in body


def test_app_token_can_list_own_app_storage(client, owner_token):
  """An app's own token can list that app's storage directory."""
  app_id, app_token = _make_app_and_token(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/items/a.json",
    json={"k": 1},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  r = client.get(
    f"/api/storage/apps-list/{app_id}/items",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200
  names = [e["name"] for e in r.json()["entries"]]
  assert "a.json" in names


def test_cross_app_list_allowed_when_both_sides_permit(client, owner_token):
  """Permitted cross-app read → listing the other app's storage works."""
  caller_id = _create_app(client, owner_token, "caller", cross="read")
  caller_token = _make_app_token(client, owner_token, caller_id)
  target_id = _create_app(client, owner_token, "target", share="read")
  client.put(
    f"/api/storage/apps/{target_id}/items/shared.json",
    json={"k": 1},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps-list/{target_id}/items",
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 200
  names = [e["name"] for e in r.json()["entries"]]
  assert "shared.json" in names


def test_cross_app_list_blocked_when_not_permitted(client, owner_token):
  """Denied cross-app read → listing the other app's storage is 403."""
  caller_id = _create_app(client, owner_token, "caller")  # cross='none'
  caller_token = _make_app_token(client, owner_token, caller_id)
  target_id = _create_app(client, owner_token, "target", share="read")
  client.put(
    f"/api/storage/apps/{target_id}/items/secret.json",
    json={"k": 1},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    f"/api/storage/apps-list/{target_id}/items",
    headers={"Authorization": f"Bearer {caller_token}"},
  )
  assert r.status_code == 403


def test_deleted_app_token_rejected(client, owner_token):
  """An app token stops working the instant its app is uninstalled.

  get_principal requires the app row to still exist, so a not-yet-expired
  token for a deleted app can't read/recreate/list its storage tree (Codex
  review #1)."""
  app_id, app_token = _make_app_and_token(client, owner_token)
  app_auth = {"Authorization": f"Bearer {app_token}"}
  # Token works while the app exists.
  client.put(f"/api/storage/apps/{app_id}/x.json", json={"k": 1},
             headers=app_auth)
  assert client.get(f"/api/storage/apps/{app_id}/x.json",
                    headers=app_auth).status_code == 200
  # Owner uninstalls the app.
  assert client.delete(
    f"/api/apps/{app_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  ).status_code == 204
  # The same token now fails auth — not a 404, a 401 (the principal can't
  # be resolved at all).
  assert client.get(f"/api/storage/apps/{app_id}/x.json",
                    headers=app_auth).status_code == 401
  assert client.put(f"/api/storage/apps/{app_id}/y.json", json={"k": 2},
                    headers=app_auth).status_code == 401


def test_uninstall_deletes_storage_tree(client, owner_token):
  """Uninstall removes the numeric /data/apps/<id> storage tree, not just
  the slug source dir (Codex review #1)."""
  import os
  app_id, app_token = _make_app_and_token(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/data.json", json={"k": 1},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  storage_dir = os.path.join(os.environ["DATA_DIR"], "apps", str(app_id))
  assert os.path.isdir(storage_dir)
  assert client.delete(
    f"/api/apps/{app_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  ).status_code == 204
  assert not os.path.isdir(storage_dir)


def test_deleted_app_token_rejected_on_shared_storage(client, owner_token):
  """The shared-storage routes reject a deleted app's token too.

  The shared routes use resolve_owner_or_app, not get_principal — the
  fix centralizes the app-row check so BOTH paths reject a stale token
  (Codex review #2)."""
  app_id, app_token = _make_app_and_token(client, owner_token)
  app_auth = {"Authorization": f"Bearer {app_token}"}
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  client.put("/api/storage/shared/s.txt", json={"content": "x"},
             headers=owner_auth)
  # App token can read shared while the app exists.
  assert client.get("/api/storage/shared/s.txt",
                    headers=app_auth).status_code == 200
  assert client.delete(f"/api/apps/{app_id}",
                       headers=owner_auth).status_code == 204
  # After uninstall the token is rejected on shared read AND shared-list,
  # not just the numeric per-app routes.
  assert client.get("/api/storage/shared/s.txt",
                    headers=app_auth).status_code == 401
  assert client.get("/api/storage/shared-list/",
                    headers=app_auth).status_code == 401


def test_app_token_rejected_after_id_reuse(client, owner_token, db):
  """A token can't authenticate against a DIFFERENT app that reused its
  SQLite integer id — the row's rotated token_nonce no longer matches the
  token's stamped app_nonce (Codex review #1)."""
  import app.models as models
  app_id, app_token = _make_app_and_token(client, owner_token)
  app_auth = {"Authorization": f"Bearer {app_token}"}
  assert client.put(f"/api/storage/apps/{app_id}/x.json", json={"k": 1},
                    headers=app_auth).status_code == 204
  # Simulate id reuse: the row at this id is now a different app identity
  # (a fresh install would get a fresh random nonce).
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.token_nonce = "rotated-deadbeef-nonce"
  db.commit()
  assert client.get(f"/api/storage/apps/{app_id}/x.json",
                    headers=app_auth).status_code == 401
