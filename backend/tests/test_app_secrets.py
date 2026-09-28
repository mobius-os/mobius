from pathlib import Path

from app import models
from app.auth import create_app_token
from app.config import get_settings


def _create_app(db, name: str) -> models.App:
  slug = name.lower().replace(" ", "-")
  app = models.App(
    source_dir=f"/tmp/mobius-tests/{slug}",
    name=name,
    slug=slug,
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


def test_app_service_token_can_read_its_own_secret(client, auth, db):
  app = _create_app(db, "Service Backed")
  owner = db.query(models.Owner).first()
  assert client.put(
    f"/api/apps/{app.id}/secrets/key",
    headers=auth,
    json={"value": "service-value"},
  ).status_code == 204

  service_token = create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
    service="private",
  )
  service_auth = {"Authorization": f"Bearer {service_token}"}
  read = client.get(f"/api/apps/{app.id}/secrets/key", headers=service_auth)
  assert read.status_code == 200
  assert read.text == "service-value"

  other = _create_app(db, "Not Mine")
  cross = client.get(f"/api/apps/{other.id}/secrets/key", headers=service_auth)
  assert cross.status_code == 403


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


def _job_auth(client, auth, app):
  response = client.post('/api/auth/app-job-token', headers=auth, json={'app_id': app.id})
  assert response.status_code == 200
  return {'Authorization': 'Bearer ' + response.json()['token']}


def test_job_reads_only_named_own_secrets_and_cannot_mint(client, auth, db):
  app = _create_app(db, 'Scoped Job')
  other = _create_app(db, 'Other Job')
  app.capability_contract = {'data': {'job_secret_read': ['bot-token']}}
  db.commit()
  for target, name in [(app, 'bot-token'), (app, 'unapproved'), (other, 'bot-token')]:
    assert client.put(f'/api/apps/{target.id}/secrets/{name}', headers=auth,
                      json={'value': 'fake-test-key'}).status_code == 204
  job = _job_auth(client, auth, app)
  path = f'/api/apps/{app.id}/secrets/bot-token'
  read = client.get(path, headers=job)
  assert read.status_code == 200 and read.text == 'fake-test-key'
  assert read.headers['cache-control'] == 'no-store'
  assert client.get(path, headers=_app_auth(db, app)).status_code == 403
  assert client.get(f'/api/apps/{app.id}/secrets/unapproved', headers=job).status_code == 403
  assert client.get(f'/api/apps/{other.id}/secrets/bot-token', headers=job).status_code == 403
  for headers in (job, _app_auth(db, app)):
    assert client.post('/api/auth/app-job-token', headers=headers,
                       json={'app_id': app.id}).status_code == 403


def test_job_grant_removal_revokes_and_addition_requires_new_job(client, auth, db):
  app = _create_app(db, 'Revocable Job')
  path = f'/api/apps/{app.id}/secrets/bot-token'
  assert client.put(path, headers=auth, json={'value': 'fake'}).status_code == 204
  unapproved = _job_auth(client, auth, app)
  app.capability_contract = {'data': {'job_secret_read': ['bot-token']}}
  db.commit()
  assert client.get(path, headers=unapproved).status_code == 403
  approved = _job_auth(client, auth, app)
  assert client.get(path, headers=approved).status_code == 200
  app.capability_contract = {'data': {}}
  db.commit()
  assert client.get(path, headers=approved).status_code == 403


def test_job_secret_access_keeps_expiry_epoch_and_nonce_checks(client, auth, db):
  from datetime import timedelta
  app = _create_app(db, 'Lifetime Job')
  app.capability_contract = {'data': {'job_secret_read': ['bot-token']}}
  db.commit()
  owner = db.query(models.Owner).first()
  path = f'/api/apps/{app.id}/secrets/bot-token'
  assert client.put(path, headers=auth, json={'value': 'fake'}).status_code == 204
  for options in [
    {'expires_delta': timedelta(seconds=-1)},
    {'token_epoch': owner.token_epoch + 1},
    {'app_nonce': 'revoked-instance'},
  ]:
    args = dict(app_id=app.id, owner_username=owner.username,
                token_epoch=owner.token_epoch, app_nonce=app.token_nonce,
                job_secrets=['bot-token'])
    args.update(options)
    token = create_app_token(**args)
    assert client.get(path, headers={'Authorization': f'Bearer {token}'}).status_code in (401, 403)


def test_job_grant_is_rechecked_after_waiting_for_storage_lock(client, auth, db, monkeypatch):
  from contextlib import asynccontextmanager
  from app import fs_locks
  app = _create_app(db, 'Queued Job')
  app.capability_contract = {'data': {'job_secret_read': ['bot-token']}}
  db.commit()
  path = f'/api/apps/{app.id}/secrets/bot-token'
  assert client.put(path, headers=auth, json={'value': 'fake'}).status_code == 204
  job = _job_auth(client, auth, app)

  @asynccontextmanager
  async def revoked_while_queued(app_id):
    assert app_id == app.id
    app.capability_contract = {'data': {}}
    db.commit()
    yield

  monkeypatch.setattr(fs_locks, 'app_storage_lock', revoked_while_queued)
  assert client.get(path, headers=job).status_code == 403
