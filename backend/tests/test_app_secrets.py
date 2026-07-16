from pathlib import Path

from app import models
from app.auth import create_app_token
from app.config import get_settings


def _create_app(db, name: str) -> models.App:
  app = models.App(
    name=name,
    slug=name.lower().replace(" ", "-"),
    description="test",
    jsx_source="export default function App() { return null }",
    compiled_path=f"/tmp/{name}.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _app_auth(db, app: models.App) -> dict[str, str]:
  owner = db.query(models.Owner).first()
  token = create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
  )
  return {"Authorization": f"Bearer {token}"}


def test_app_secret_roundtrip_is_encrypted_at_rest(client, auth, db):
  app = _create_app(db, "Image Tool")
  response = client.put(
    f"/api/apps/{app.id}/secrets/provider-key",
    headers=auth,
    json={"value": "private-key-value"},
  )
  assert response.status_code == 204

  path = (
    Path(get_settings().data_dir)
    / "app-secrets" / str(app.id) / "provider-key"
  )
  assert path.read_text() != "private-key-value"
  assert "private-key-value" not in path.read_text()
  assert path.parent.stat().st_mode & 0o777 == 0o700
  assert path.stat().st_mode & 0o777 == 0o600

  read = client.get(
    f"/api/apps/{app.id}/secrets/provider-key",
    headers=auth,
  )
  assert read.status_code == 200
  assert read.text == "private-key-value"
  assert read.headers["cache-control"] == "no-store"


def test_app_can_store_and_check_but_not_read_its_own_secret(
  client, owner_token, db,
):
  first = _create_app(db, "First")
  second = _create_app(db, "Second")
  first_auth = _app_auth(db, first)
  sandbox_headers = {
    **first_auth,
    "Origin": "null",
    "Sec-Fetch-Site": "cross-site",
  }

  own = client.put(
    f"/api/apps/{first.id}/secrets/key",
    headers=sandbox_headers,
    json={"value": "first-value"},
  )
  assert own.status_code == 204

  status = client.head(
    f"/api/apps/{first.id}/secrets/key",
    headers=first_auth,
  )
  assert status.status_code == 204
  assert status.headers["cache-control"] == "no-store"
  read = client.get(
    f"/api/apps/{first.id}/secrets/key",
    headers=first_auth,
  )
  assert read.status_code == 403

  cross = client.head(
    f"/api/apps/{second.id}/secrets/key",
    headers=first_auth,
  )
  assert cross.status_code == 403
  assert client.delete(
    f"/api/apps/{first.id}/secrets/key",
    headers=sandbox_headers,
  ).status_code == 204
  assert client.head(
    f"/api/apps/{first.id}/secrets/key",
    headers=first_auth,
  ).status_code == 404


def test_delete_app_secret(client, auth, db):
  app = _create_app(db, "Disposable")
  path = f"/api/apps/{app.id}/secrets/key"
  assert client.put(path, headers=auth, json={"value": "secret"}).status_code == 204
  assert client.delete(path, headers=auth).status_code == 204
  assert client.get(path, headers=auth).status_code == 404


def test_app_secret_name_is_strictly_validated(client, auth, db):
  app = _create_app(db, "Strict")
  response = client.put(
    f"/api/apps/{app.id}/secrets/not%20valid",
    headers=auth,
    json={"value": "secret"},
  )
  assert response.status_code == 400


def test_app_secret_count_is_bounded(client, auth, db):
  app = _create_app(db, "Bounded")
  for index in range(16):
    response = client.put(
      f"/api/apps/{app.id}/secrets/key-{index}",
      headers=auth,
      json={"value": f"secret-{index}"},
    )
    assert response.status_code == 204

  overflow = client.put(
    f"/api/apps/{app.id}/secrets/one-too-many",
    headers=auth,
    json={"value": "overflow"},
  )
  assert overflow.status_code == 413

  # Replacing an existing value does not consume another slot.
  replacement = client.put(
    f"/api/apps/{app.id}/secrets/key-0",
    headers=auth,
    json={"value": "replacement"},
  )
  assert replacement.status_code == 204


def test_media_token_cannot_access_app_secrets(client, auth, db, chat):
  app = _create_app(db, "No Media Scope")
  media_token = client.post(
    f"/api/chats/{chat.id}/media-token", headers=auth,
  ).json()["token"]
  media_auth = {"Authorization": f"Bearer {media_token}"}
  path = f"/api/apps/{app.id}/secrets/key"

  assert client.put(
    path, headers=media_auth, json={"value": "blocked"},
  ).status_code == 403
  assert client.head(path, headers=media_auth).status_code == 403
  assert client.get(path, headers=media_auth).status_code == 403
  assert client.delete(path, headers=media_auth).status_code == 403


def test_entrypoint_secret_root_is_usable_when_volume_chown_fails():
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")

  assert "if chown mobius:mobius /data/app-secrets" in entrypoint
  assert "chmod 700 /data/app-secrets" in entrypoint
  assert "chmod 733 /data/app-secrets" in entrypoint
