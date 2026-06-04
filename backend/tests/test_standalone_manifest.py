"""The per-app PWA manifest must reflect a rename immediately and never
be served stale.

The install flow saves the home-screen name (PATCH /api/apps/{id}) and
then navigates straight to the install surface, so the manifest fetched
there has to carry the just-saved name. Two guarantees back that:
`Cache-Control: no-cache` (the browser can't pin an old manifest) and a
microsecond-resolution icon `?v=` (a name PATCH + icon PUT in the same
second still bust the icon cache).
"""

import re

import pytest

from app import models
from app.database import SessionLocal


def _create_app(client, owner_token, name):
  r = client.post(
    "/api/apps/",
    json={
      "name": name,
      "description": "x",
      "jsx_source": "export default function App() { return <div>hi</div> }",
    },
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  return r.json()


def _spa_active(client):
  """The SPA catch-all only registers in built/container environments."""
  r = client.get("/__definitely_shell_route__")
  return r.status_code == 200 and "text/html" in r.headers.get(
    "content-type", ""
  )


def test_manifest_has_no_cache_header(client, owner_token):
  app = _create_app(client, owner_token, "News App")
  r = client.get(f"/apps/{app['slug']}/manifest.json")
  assert r.status_code == 200
  assert "no-cache" in r.headers.get("cache-control", "").lower()


def test_manifest_name_reflects_rename_immediately(client, owner_token, auth):
  app = _create_app(client, owner_token, "News App")
  slug, app_id = app["slug"], app["id"]

  before = client.get(f"/apps/{slug}/manifest.json").json()
  assert before["name"] == "News App"
  assert before["short_name"] == "News App"[:12]

  r = client.patch(f"/api/apps/{app_id}", json={"name": "News"}, headers=auth)
  assert r.status_code == 200, r.text

  after = client.get(f"/apps/{slug}/manifest.json").json()
  assert after["name"] == "News"
  assert after["short_name"] == "News"


def test_icon_version_is_microsecond_resolution(client, owner_token):
  app = _create_app(client, owner_token, "News App")
  body = client.get(f"/apps/{app['slug']}/manifest.json").json()
  src = body["icons"][0]["src"]
  m = re.search(r"[?&]v=(\d+)", src)
  assert m, src
  # Microseconds since the epoch are ~1.7e15 in 2026; int-seconds would
  # be ~1.7e9. This locks in the resolution bump that prevents
  # same-second collisions between a name PATCH and an icon PUT.
  assert int(m.group(1)) > 10**15, src


def test_manifest_and_loading_shell_use_app_declared_colors(client, owner_token):
  app = _create_app(client, owner_token, "Atlas")
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app["id"]).one()
    row.theme_color = "#223344"
    row.background_color = "#101820"
    db.commit()
  finally:
    db.close()

  manifest = client.get(f"/apps/{app['slug']}/manifest.json").json()
  assert manifest["theme_color"] == "#223344"
  assert manifest["background_color"] == "#101820"

  shell = client.get(f"/apps/{app['slug']}/")
  assert shell.status_code == 200
  assert "--bg: #101820;" in shell.text


def test_top_level_app_slug_redirects_to_standalone_scope(client, owner_token):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  app = _create_app(client, owner_token, "CubeRun")
  assert app["slug"] == "cuberun"

  for path in ("/cuberun", "/cuberun/", "/cuberun/index.html"):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 307, path
    assert r.headers["location"] == "/apps/cuberun/"
    assert r.headers["cache-control"] == "no-store"


def test_reserved_top_level_routes_do_not_alias_to_apps(client, owner_token):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")

  db = SessionLocal()
  try:
    for slug in ("api", "apps", "assets", "recover", "shell", "vendor"):
      db.add(models.App(
        name=slug,
        slug=slug,
        description="reserved route collision",
        jsx_source="export default function App() { return null }",
      ))
    db.commit()
  finally:
    db.close()

  for slug in ("api", "apps", "assets", "recover", "shell", "vendor"):
    r = client.get(f"/{slug}", follow_redirects=False)
    assert r.status_code != 307, slug
    assert r.headers.get("location") != f"/apps/{slug}/"


def test_unknown_top_level_route_still_serves_shell(client):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  r = client.get("/not-an-installed-app")
  assert r.status_code == 200
  assert "text/html" in r.headers.get("content-type", "")
