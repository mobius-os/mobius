"""Hosted app publication stays snapshotted, exact-app, and revocable."""

import hashlib
import json
from pathlib import Path
import re

from fastapi.responses import Response

from app import auth as token_auth, models
from app.config import get_settings
from app.database import SessionLocal
from test_app_fixtures import create_local_app


PUBLIC_ACCESS = {
  "network": [{
    "origin": "https://example.com",
    "path_prefix": "/events",
    "query": {"allow": ["city"]},
  }],
}


def _create(
  client, auth, name="Public test", public_access=PUBLIC_ACCESS, **kwargs,
):
  return create_local_app(
    client,
    auth,
    name=name,
    manifest_extra={"public_access": public_access},
    **kwargs,
  )


def _publish(client, headers, app_id):
  return client.put(f"/api/apps/{app_id}/hosted-publication", headers=headers)


def _public_token_from_html(html: str) -> str:
  match = re.search(r"const TOKEN = (\"[^\"]+\");", html)
  assert match, html[:1000]
  return json.loads(match.group(1))


def _public_module(client, app_id: int, token: str):
  return client.get(
    f"/api/public-apps/{app_id}/module",
    headers={"Authorization": f"Bearer {token}"},
  )


def test_app_is_private_by_default_and_top_level_alias_stays_owner_only(
  client, auth,
):
  app = _create(client, auth)
  assert app["hosted_publication"] is None
  assert app["capability_contract"]["public"] == PUBLIC_ACCESS

  response = client.get(f"/{app['slug']}", follow_redirects=False)
  assert response.status_code == 307
  assert response.headers["location"] == f"/apps/{app['slug']}/"


def test_owner_publishes_exact_snapshot_without_exposing_an_owner_token(
  client, auth,
):
  app = _create(client, auth)
  published = _publish(client, auth, app["id"])
  assert published.status_code == 200, published.text
  state = published.json()["hosted_publication"]
  assert state["path"] == f"/{app['slug']}"
  assert state["has_unpublished_changes"] is False

  response = client.get(f"/{app['slug']}", follow_redirects=False)
  assert response.status_code == 200
  assert response.headers["cache-control"] == "no-store"
  assert "moebius:frame-init" in response.text
  assert "/module?token=" not in response.text
  token = _public_token_from_html(response.text)
  claims = token_auth.decode_access_token(token)
  assert claims["scope"] == "public_app"
  assert claims["app_id"] == app["id"]
  assert "sub" not in claims
  assert "epoch" not in claims

  module = _public_module(client, app["id"], token)
  assert module.status_code == 200
  assert client.get(
    f"/api/public-apps/{app['id']}/module", params={"token": token},
  ).status_code == 401
  # Public tokens never enter the owner/app module route.
  assert client.get(
    f"/api/apps/{app['id']}/module", params={"token": token},
  ).status_code in (401, 403)


def test_private_edit_does_not_change_live_snapshot_until_publish_update(
  client, auth,
):
  app = _create(client, auth, jsx_source=(
    "export default function App() { return <div>first</div> }\n"
  ))
  assert _publish(client, auth, app["id"]).status_code == 200
  first_page = client.get(f"/{app['slug']}")
  first_token = _public_token_from_html(first_page.text)
  first_module = _public_module(client, app["id"], first_token).content

  source = Path(app["source_dir"])
  (source / "index.jsx").write_text(
    "export default function App() { return <div>second</div> }\n"
  )
  applied = client.post(
    "/api/apps/apply",
    json={"source_dir": str(source)},
    headers=auth,
  )
  assert applied.status_code == 200, applied.text
  assert applied.json()["app"]["hosted_publication"][
    "has_unpublished_changes"
  ] is True
  assert _public_module(client, app["id"], first_token).content == first_module

  republished = _publish(client, auth, app["id"])
  assert republished.status_code == 200, republished.text
  assert republished.json()["hosted_publication"][
    "has_unpublished_changes"
  ] is False
  assert _public_module(client, app["id"], first_token).status_code == 401
  second_token = _public_token_from_html(client.get(f"/{app['slug']}").text)
  second_module = _public_module(client, app["id"], second_token).content
  assert second_module != first_module


def test_private_name_edit_does_not_change_public_metadata_until_republish(
  client, auth,
):
  app = _create(client, auth, name="Original public name")
  assert _publish(client, auth, app["id"]).status_code == 200

  renamed = client.patch(
    f"/api/apps/{app['id']}",
    json={"name": "Private draft name"},
    headers=auth,
  )
  assert renamed.status_code == 200, renamed.text
  assert renamed.json()["hosted_publication"]["has_unpublished_changes"] is True
  public_page = client.get(f"/{app['slug']}")
  assert "<title>Original public name</title>" in public_page.text
  assert "Private draft name" not in public_page.text

  assert _publish(client, auth, app["id"]).status_code == 200
  updated_page = client.get(f"/{app['slug']}")
  assert "<title>Private draft name</title>" in updated_page.text


def test_public_token_is_exact_app_and_rejected_by_private_surfaces(
  client, auth,
):
  first = _create(client, auth, "First public")
  second = _create(client, auth, "Second private")
  _publish(client, auth, first["id"])
  token = _public_token_from_html(client.get(f"/{first['slug']}").text)
  bearer = {"Authorization": f"Bearer {token}"}

  assert _public_module(client, second["id"], token).status_code == 401
  assert client.get(
    f"/api/storage/apps/{first['id']}/private.json", headers=bearer,
  ).status_code in (401, 403)
  assert client.get(
    "/api/proxy?url=https%3A%2F%2Fexample.com%2Fevents", headers=bearer,
  ).status_code in (401, 403)
  assert client.get("/api/settings", headers=bearer).status_code in (401, 403)


def test_public_fetch_enforces_path_query_contract_and_exact_app_token(
  client, auth, monkeypatch,
):
  from app import public_app_transport as public_transport

  app = _create(client, auth)
  other = _create(client, auth, "Other app")
  _publish(client, auth, app["id"])
  token = _public_token_from_html(client.get(f"/{app['slug']}").text)
  bearer = {"Authorization": f"Bearer {token}"}

  monkeypatch.setattr(
    public_transport,
    "validate_url_safe",
    lambda url: (url, "example.com", "example.com"),
  )

  clients = []

  async def fake_response(_client, _request, **_kwargs):
    clients.append(_client)
    return Response(
      b"ok", media_type="text/plain", headers={"Cache-Control": "max-age=60"},
    )

  monkeypatch.setattr(public_transport, "_capped_response", fake_response)
  allowed = client.get(
    f"/api/public-apps/{app['id']}/fetch",
    params={"url": "https://example.com/events/today?city=london"},
    headers=bearer,
  )
  assert allowed.status_code == 200
  assert allowed.text == "ok"
  assert allowed.headers["cache-control"] == "max-age=60"
  repeated = client.get(
    f"/api/public-apps/{app['id']}/fetch",
    params={"url": "https://example.com/events/today?city=berlin"},
    headers=bearer,
  )
  assert repeated.status_code == 200
  assert clients[0] is clients[1]

  for denied_url in (
    "https://example.com/account",
    "https://example.com/events?admin=true",
    "https://example.com/events?city=london&city=berlin",
  ):
    denied = client.get(
      f"/api/public-apps/{app['id']}/fetch",
      params={"url": denied_url},
      headers=bearer,
    )
    assert denied.status_code == 403
  denied_app = client.get(
    f"/api/public-apps/{other['id']}/fetch",
    params={"url": "https://example.com/events"},
    headers=bearer,
  )
  assert denied_app.status_code == 401


def test_public_query_contract_can_bind_large_operation_text_by_digest():
  from app.public_app_transport import _target_allowed

  query = "query LIST($city: String!) { events(city: $city) { id } }"
  rules = [{
    "origin": "https://example.com",
    "path_prefix": "/graphql",
    "query": {
      "allow": ["variables"],
      "sha256": {"query": [hashlib.sha256(query.encode()).hexdigest()]},
    },
  }]
  from urllib.parse import urlencode
  allowed = "https://example.com/graphql?" + urlencode({
    "query": query, "variables": '{"city":"London"}',
  })
  denied = "https://example.com/graphql?" + urlencode({
    "query": "query ADMIN { users { password } }",
    "variables": "{}",
  })

  assert _target_allowed(allowed, rules) is True
  assert _target_allowed(denied, rules) is False


def test_stop_revokes_existing_session_and_restores_private_alias(client, auth):
  app = _create(client, auth)
  _publish(client, auth, app["id"])
  token = _public_token_from_html(client.get(f"/{app['slug']}").text)

  stopped = client.delete(
    f"/api/apps/{app['id']}/hosted-publication", headers=auth,
  )
  assert stopped.status_code == 200
  assert stopped.json()["hosted_publication"] is None
  assert _public_module(client, app["id"], token).status_code == 401
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
  db = SessionLocal()
  try:
    row = db.get(models.App, app["id"])
    row.slug = "shell"
    db.commit()
  finally:
    db.close()
  response = _publish(client, auth, app["id"])
  assert response.status_code == 400
