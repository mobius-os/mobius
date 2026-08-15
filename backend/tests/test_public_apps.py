"""Anonymous app publication stays exact-app, opt-in, and revocable."""

import json
import re

from fastapi.responses import Response

from app import auth as token_auth, models
from app.config import get_settings
from app.database import SessionLocal
from test_app_fixtures import create_local_app


PUBLIC_ACCESS = {
  "network": [
    {"origin": "https://example.com", "path_prefix": "/events"},
  ],
}


def _create(client, auth, name="Public test", public_access=PUBLIC_ACCESS):
  return create_local_app(
    client,
    auth,
    name=name,
    manifest_extra={"public_access": public_access},
  )


def _public_token_from_html(html: str) -> str:
  match = re.search(r"const TOKEN = (\"[^\"]+\");", html)
  assert match, html[:1000]
  return json.loads(match.group(1))


def test_app_is_private_by_default_and_top_level_alias_stays_owner_only(
  client, auth,
):
  app = _create(client, auth)
  assert app["public_enabled"] is False
  assert app["capability_contract"]["public"] == PUBLIC_ACCESS

  response = client.get(f"/{app['slug']}", follow_redirects=False)
  assert response.status_code == 307
  assert response.headers["location"] == f"/apps/{app['slug']}/"


def test_owner_can_publish_exact_slug_without_exposing_an_owner_token(
  client, auth,
):
  app = _create(client, auth)
  enabled = client.patch(
    f"/api/apps/{app['id']}",
    json={"public_enabled": True},
    headers=auth,
  )
  assert enabled.status_code == 200, enabled.text
  assert enabled.json()["public_enabled"] is True

  response = client.get(f"/{app['slug']}", follow_redirects=False)
  assert response.status_code == 200
  assert response.headers["cache-control"] == "no-store"
  assert "moebius:frame-init" in response.text
  token = _public_token_from_html(response.text)
  claims = token_auth.decode_access_token(token)
  assert claims["scope"] == "public_app"
  assert claims["app_id"] == app["id"]
  assert "sub" not in claims
  assert "epoch" not in claims

  module = client.get(f"/api/apps/{app['id']}/module?token={token}")
  assert module.status_code == 200


def test_public_token_is_exact_app_and_rejected_by_private_surfaces(
  client, auth,
):
  first = _create(client, auth, "First public")
  second = _create(client, auth, "Second private")
  client.patch(
    f"/api/apps/{first['id']}", json={"public_enabled": True}, headers=auth,
  )
  token = _public_token_from_html(client.get(f"/{first['slug']}").text)
  bearer = {"Authorization": f"Bearer {token}"}

  assert client.get(
    f"/api/apps/{second['id']}/module?token={token}",
  ).status_code == 401
  assert client.get(
    f"/api/storage/apps/{first['id']}/private.json", headers=bearer,
  ).status_code in (401, 403)
  assert client.get(
    "/api/proxy?url=https%3A%2F%2Fexample.com%2Fevents", headers=bearer,
  ).status_code in (401, 403)
  assert client.get("/api/settings", headers=bearer).status_code in (401, 403)


def test_public_fetch_enforces_manifest_rules_and_exact_app_token(
  client, auth, monkeypatch,
):
  from app.routes import public_apps as public_routes

  app = _create(client, auth)
  other = _create(client, auth, "Other app")
  client.patch(
    f"/api/apps/{app['id']}", json={"public_enabled": True}, headers=auth,
  )
  token = _public_token_from_html(client.get(f"/{app['slug']}").text)
  bearer = {"Authorization": f"Bearer {token}"}

  monkeypatch.setattr(
    public_routes,
    "validate_url_safe",
    lambda url: (url, "example.com", "example.com"),
  )

  async def fake_response(_client, _request, **_kwargs):
    return Response(
      b"ok", media_type="text/plain", headers={"Cache-Control": "max-age=60"},
    )

  monkeypatch.setattr(public_routes, "_capped_response", fake_response)
  allowed = client.get(
    f"/api/public-apps/{app['id']}/fetch",
    params={"url": "https://example.com/events/today?city=london"},
    headers=bearer,
  )
  assert allowed.status_code == 200
  assert allowed.text == "ok"
  assert allowed.headers["cache-control"] == "max-age=60"

  denied_path = client.get(
    f"/api/public-apps/{app['id']}/fetch",
    params={"url": "https://example.com/account"},
    headers=bearer,
  )
  assert denied_path.status_code == 403
  denied_app = client.get(
    f"/api/public-apps/{other['id']}/fetch",
    params={"url": "https://example.com/events"},
    headers=bearer,
  )
  assert denied_app.status_code == 401


def test_unpublish_revokes_existing_session_and_restores_private_alias(
  client, auth,
):
  app = _create(client, auth)
  client.patch(
    f"/api/apps/{app['id']}", json={"public_enabled": True}, headers=auth,
  )
  token = _public_token_from_html(client.get(f"/{app['slug']}").text)

  stopped = client.patch(
    f"/api/apps/{app['id']}", json={"public_enabled": False}, headers=auth,
  )
  assert stopped.status_code == 200
  assert stopped.json()["public_enabled"] is False
  assert client.get(
    f"/api/apps/{app['id']}/module?token={token}",
  ).status_code == 401
  root = client.get(f"/{app['slug']}", follow_redirects=False)
  assert root.status_code == 307
  assert root.headers["location"] == f"/apps/{app['slug']}/"


def test_reserved_root_slug_cannot_be_published(client, auth):
  app = create_local_app(
    client,
    auth,
    name="Reserved shell",
    source_dir=f"{get_settings().data_dir}/apps/reserved-shell",
  )
  # Explicitly force the historical colliding slug; the publication PATCH is
  # responsible for refusing it even if an old installation already has one.
  db = SessionLocal()
  try:
    row = db.get(models.App, app["id"])
    row.slug = "shell"
    db.commit()
  finally:
    db.close()
  response = client.patch(
    f"/api/apps/{app['id']}", json={"public_enabled": True}, headers=auth,
  )
  assert response.status_code == 400
