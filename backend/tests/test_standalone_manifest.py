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
from pathlib import Path

import pytest

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.install import _manifest_display
from app.theme import get_bg_color


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


def test_root_shell_manifest_is_theme_colored_and_no_cache(client):
  """The root shell manifest carries the live theme `--bg` as theme_color and
  must revalidate on every fetch. On standalone Android the OS reads the
  manifest theme_color for the system/gesture-nav bar tint, so a stale-cached
  manifest pins that bar to the old theme after a theme change (card 164)."""
  from app.main import _resolve_static_dir
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  # A real frontend build copies frontend/public/manifest.webmanifest into the
  # served static dir; the venv test env has the static dir but not that file,
  # so provision a minimal one for the duration of the test (the route only
  # overwrites theme_color/background_color, it doesn't author the manifest).
  manifest_path = _resolve_static_dir() / "manifest.webmanifest"
  created = False
  if not manifest_path.is_file():
    manifest_path.write_text(
      '{"name":"Mobius","theme_color":"#abcdef","background_color":"#abcdef"}',
      encoding="utf-8",
    )
    created = True
  try:
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers.get("content-type", "")
    manifest = r.json()
    theme_bg = get_bg_color(get_settings().data_dir)
    assert manifest["theme_color"] == theme_bg
    assert manifest["background_color"] == theme_bg
    assert "no-cache" in r.headers.get("cache-control", "").lower()
  finally:
    if created:
      manifest_path.unlink()


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


def test_manifest_colors_fall_back_to_theme_not_icon(client, owner_token):
  """An app that declares no colors gets a status bar matching the owner's
  live theme `--bg`, not a color sampled from its icon. (Old behavior fell
  back to the icon's dominant opaque color — `#0c0f14` for the iconless
  build-path app here — so this locks in the new theme-aware fallback.)"""
  app = _create_app(client, owner_token, "Plain")
  theme_bg = get_bg_color(get_settings().data_dir)

  manifest = client.get(f"/apps/{app['slug']}/manifest.json").json()
  assert manifest["theme_color"] == theme_bg
  assert manifest["background_color"] == theme_bg
  assert manifest["theme_color"] != "#0c0f14"

  shell = client.get(f"/apps/{app['slug']}/")
  assert f"--bg: {theme_bg};" in shell.text


def test_manifest_display_defaults_to_standalone(client, owner_token):
  app = _create_app(client, owner_token, "Plain Display")
  manifest = client.get(f"/apps/{app['slug']}/manifest.json").json()
  assert manifest["display"] == "standalone"


def test_manifest_display_passthrough(client, owner_token):
  app = _create_app(client, owner_token, "Fuller")
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app["id"]).one()
    row.display = "fullscreen"
    db.commit()
  finally:
    db.close()
  manifest = client.get(f"/apps/{app['slug']}/manifest.json").json()
  assert manifest["display"] == "fullscreen"


def test_display_migration_adds_column_to_existing_db(tmp_path):
  """Prod has an existing `apps` table; create_all never ALTERs it, so the
  `display` column must be added by run_migrations. Build a full schema, drop
  the column to simulate a pre-display DB, then assert run_migrations re-adds
  it (the same gate that runs on prod boot)."""
  from sqlalchemy import create_engine, inspect, text
  from app.database import Base, run_migrations

  eng = create_engine(f"sqlite:///{tmp_path}/legacy.db")
  Base.metadata.create_all(bind=eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE apps DROP COLUMN display"))
  assert "display" not in {c["name"] for c in inspect(eng).get_columns("apps")}

  run_migrations(eng)
  assert "display" in {c["name"] for c in inspect(eng).get_columns("apps")}


def test_manifest_display_validator_coerces():
  assert _manifest_display("fullscreen") == "fullscreen"
  assert _manifest_display("  FULLSCREEN ") == "fullscreen"
  assert _manifest_display("minimal-ui") == "minimal-ui"
  assert _manifest_display("standalone") == "standalone"
  # Unknown / non-string drop to None so the manifest serves "standalone".
  assert _manifest_display("immersive") is None
  assert _manifest_display("") is None
  assert _manifest_display(None) is None
  assert _manifest_display(123) is None


def test_top_level_app_slug_redirects_to_standalone_scope(client, owner_token):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  app = _create_app(client, owner_token, "CubeRun")
  assert app["slug"] == "cuberun"

  for path in ("/cuberun", "/cuberun/"):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 307, path
    assert r.headers["location"] == "/apps/cuberun/"
    assert r.headers["cache-control"] == "no-store"


def test_top_level_index_html_does_not_alias_to_standalone(
  client, owner_token,
):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  app = _create_app(client, owner_token, "CubeRun")
  assert app["slug"] == "cuberun"

  r = client.get("/cuberun/index.html", follow_redirects=False)
  assert r.status_code != 307
  assert r.headers.get("location") != "/apps/cuberun/"


def test_app_owned_static_assets_are_served_from_source_dir(
  client, owner_token,
):
  app = _create_app(client, owner_token, "CubeRun")
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app["id"]).one()
    static = Path(row.source_dir) / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text(
      "<!doctype html><title>CubeRun</title><main>game</main>",
      encoding="utf-8",
    )
    (static / "main.js").write_text(
      "console.log('cuberun')",
      encoding="utf-8",
    )
  finally:
    db.close()

  html = client.get("/app-assets/cuberun/")
  assert html.status_code == 200
  assert "CubeRun" in html.text
  assert "no-cache" in html.headers.get("cache-control", "")

  by_id = client.get(f"/app-assets/by-id/{app['id']}/index.html")
  assert by_id.status_code == 200
  assert "CubeRun" in by_id.text

  js = client.get("/app-assets/cuberun/main.js")
  assert js.status_code == 200
  assert "cuberun" in js.text
  assert js.headers["x-content-type-options"] == "nosniff"

  traversal = client.get("/app-assets/cuberun/../index.html")
  assert traversal.status_code in (404, 405)


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
