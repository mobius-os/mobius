import json
from pathlib import Path

import httpx
from fastapi import Response

from app import models
from app.app_capabilities import contract_from_manifest
from app.auth import create_app_token, hash_password
from app.config import get_settings
import app.routes.secrets as secrets_routes


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
  if owner is None:
    owner = models.Owner(
      username="test",
      hashed_password=hash_password("testpassword123"),
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
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


def _declare_credentialed_fetch(db, app: models.App, source: Path) -> None:
  source.mkdir(parents=True, exist_ok=True)
  manifest = {
    "permissions": {
      "credentialed_fetch": {
        "amap": {
          "secret": "amap-key",
          "origin": "https://restapi.amap.com",
          "paths": ["/v3/staticmap", "/v3/geocode/geo", "/v5/direction/"],
          "query_parameter": "key",
        },
      },
    },
  }
  (source / "mobius.json").write_text(json.dumps(manifest))
  app.source_dir = str(source)
  app.capability_contract = contract_from_manifest(manifest)
  db.commit()
  db.refresh(app)


def test_credentialed_fetch_injects_secret_without_returning_it(
  client, db, tmp_path, monkeypatch,
):
  app = _create_app(db, "Map Tool")
  _declare_credentialed_fetch(db, app, tmp_path / "map-tool")
  app_auth = _app_auth(db, app)
  assert client.put(
    f"/api/apps/{app.id}/secrets/amap-key",
    headers={**app_auth, "Origin": "null", "Sec-Fetch-Site": "cross-site"},
    json={"value": "private-map-key"},
  ).status_code == 204

  captured = {}
  monkeypatch.setattr(
    secrets_routes,
    "validate_url_safe",
    lambda url: (url, "restapi.amap.com", "restapi.amap.com"),
  )

  async def fake_response(_client, request):
    captured["url"] = str(request.url)
    return Response(content=b"map-bytes", media_type="image/png")

  monkeypatch.setattr(secrets_routes, "_credentialed_response", fake_response)
  response = client.get(
    f"/api/apps/{app.id}/credentialed-fetch/amap",
    headers=app_auth,
    params={
      "url": "https://restapi.amap.com/v3/staticmap?zoom=6&size=600*600",
    },
  )
  assert response.status_code == 200
  assert response.content == b"map-bytes"
  assert "private-map-key" in captured["url"]
  assert "private-map-key" not in response.text


def test_credentialed_fetch_rejects_undeclared_origin_path_and_key(
  client, db, tmp_path, monkeypatch,
):
  app = _create_app(db, "Strict Map")
  _declare_credentialed_fetch(db, app, tmp_path / "strict-map")
  headers = _app_auth(db, app)
  assert client.put(
    f"/api/apps/{app.id}/secrets/amap-key",
    headers={**headers, "Origin": "null", "Sec-Fetch-Site": "cross-site"},
    json={"value": "secret"},
  ).status_code == 204
  endpoint = f"/api/apps/{app.id}/credentialed-fetch/amap"

  assert client.get(endpoint, headers=headers, params={
    "url": "https://attacker.example/v3/staticmap",
  }).status_code == 403
  assert client.get(endpoint, headers=headers, params={
    "url": "https://restapi.amap.com/v3/config/district",
  }).status_code == 403
  assert client.get(endpoint, headers=headers, params={
    "url": "https://restapi.amap.com/v3/staticmap?key=override",
  }).status_code == 400

  (tmp_path / "strict-map" / "mobius.json").write_text(json.dumps({
    "permissions": {"credentialed_fetch": {"amap": {
      "secret": "amap-key",
      "origin": "https://attacker.example",
      "paths": ["/v3/staticmap"],
      "query_parameter": "key",
    }}},
  }))
  assert client.get(endpoint, headers=headers, params={
    "url": "https://attacker.example/v3/staticmap",
  }).status_code == 403


def test_credentialed_fetch_rejects_oversized_response(
  client, db, tmp_path, monkeypatch,
):
  app = _create_app(db, "Bounded Map")
  _declare_credentialed_fetch(db, app, tmp_path / "bounded-map")
  headers = _app_auth(db, app)
  assert client.put(
    f"/api/apps/{app.id}/secrets/amap-key",
    headers={**headers, "Origin": "null", "Sec-Fetch-Site": "cross-site"},
    json={"value": "secret"},
  ).status_code == 204
  monkeypatch.setattr(
    secrets_routes,
    "validate_url_safe",
    lambda url: (url, "restapi.amap.com", "restapi.amap.com"),
  )

  async def oversized(_client, request, **_kwargs):
    return httpx.Response(
      200,
      content=b"x" * (secrets_routes._CREDENTIAL_RESPONSE_LIMIT + 1),
      request=request,
    )

  monkeypatch.setattr(httpx.AsyncClient, "send", oversized)
  response = client.get(
    f"/api/apps/{app.id}/credentialed-fetch/amap",
    headers=headers,
    params={"url": "https://restapi.amap.com/v3/staticmap"},
  )
  assert response.status_code == 413


def test_credentialed_fetch_injects_secret_after_declared_path_prefix(
  client, db, tmp_path, monkeypatch,
):
  app = _create_app(db, "Bot Bridge")
  source = tmp_path / "bot-bridge"
  source.mkdir()
  (source / "mobius.json").write_text(json.dumps({
    "permissions": {
      "credentialed_fetch": {
        "telegram": {
          "secret": "bot-token",
          "origin": "https://api.telegram.org",
          "paths": ["/bot/"],
          "path_prefix": "/bot",
        },
      },
    },
  }))
  app.capability_contract = contract_from_manifest(json.loads(
    (source / "mobius.json").read_text()
  ))
  app.source_dir = str(source)
  db.commit()
  db.refresh(app)
  app_auth = _app_auth(db, app)
  assert client.put(
    f"/api/apps/{app.id}/secrets/bot-token",
    headers={**app_auth, "Origin": "null", "Sec-Fetch-Site": "cross-site"},
    json={"value": "123456:private/token"},
  ).status_code == 204

  captured = {}
  monkeypatch.setattr(
    secrets_routes,
    "validate_url_safe",
    lambda url: (url, "api.telegram.org", "api.telegram.org"),
  )

  async def fake_response(_client, request):
    captured["url"] = str(request.url)
    return Response(content=b'{"ok":true}', media_type="application/json")

  monkeypatch.setattr(secrets_routes, "_credentialed_response", fake_response)
  response = client.get(
    f"/api/apps/{app.id}/credentialed-fetch/telegram",
    headers=app_auth,
    params={"url": "https://api.telegram.org/bot/getMe"},
  )
  assert response.status_code == 200
  assert "/bot123456:private%2Ftoken/getMe" in captured["url"]
  assert "private/token" not in response.text


def test_credentialed_fetch_path_prefix_rejects_unreviewed_path(
  client, db, tmp_path,
):
  app = _create_app(db, "Strict Bot")
  source = tmp_path / "strict-bot"
  source.mkdir()
  (source / "mobius.json").write_text(json.dumps({
    "permissions": {
      "credentialed_fetch": {
        "telegram": {
          "secret": "bot-token",
          "origin": "https://api.telegram.org",
          "paths": ["/bot/"],
          "path_prefix": "/bot",
        },
      },
    },
  }))
  app.capability_contract = contract_from_manifest(json.loads(
    (source / "mobius.json").read_text()
  ))
  app.source_dir = str(source)
  db.commit()
  db.refresh(app)
  headers = _app_auth(db, app)

  endpoint = f"/api/apps/{app.id}/credentialed-fetch/telegram"
  assert client.get(endpoint, headers=headers, params={
    "url": "https://api.telegram.org/file/getMe",
  }).status_code == 403


def test_entrypoint_secret_root_is_usable_when_volume_chown_fails():
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")

  assert "if chown mobius:mobius /data/app-secrets" in entrypoint
  assert "chmod 700 /data/app-secrets" in entrypoint
  assert "chmod 733 /data/app-secrets" in entrypoint
