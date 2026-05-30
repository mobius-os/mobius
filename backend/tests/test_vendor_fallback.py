"""The SPA HTML fallback must never answer a static-asset miss.

A module URL served as `200 text/html` (the catch-all index.html) is
rejected by the browser's strict module-MIME check and poisons a
cache-first service worker — the failure mode behind the missing
`three.core.js` regression. `_is_static_asset_path` decides which paths
404 on a miss instead of falling through to HTML.
"""

import pytest

from app.main import _is_static_asset_path


def test_classifies_module_and_asset_paths_as_static():
  assert _is_static_asset_path("vendor/three@0.184.0/three.core.js")
  assert _is_static_asset_path("assets/index-abc123.js")
  assert _is_static_asset_path("sw.js")
  assert _is_static_asset_path("some/where/styles.css")
  assert _is_static_asset_path("module.mjs")
  assert _is_static_asset_path("bundle.js.map")
  assert _is_static_asset_path("pkg/lib.wasm")
  assert _is_static_asset_path("data/config.json")
  # Bare namespace dirs (no trailing slash) also 404 rather than serve HTML.
  assert _is_static_asset_path("vendor")
  assert _is_static_asset_path("assets")


def test_app_routes_and_images_are_not_static():
  # App routes have no asset extension — they must keep getting the SPA.
  assert not _is_static_asset_path("")
  assert not _is_static_asset_path("recover")
  assert not _is_static_asset_path("chats/123")
  # .webmanifest is handled separately at the top of spa_fallback.
  assert not _is_static_asset_path("manifest.webmanifest")
  # A missing image should degrade gracefully, not 404 a real route.
  assert not _is_static_asset_path("icons/logo.png")
  # A route that merely starts with "vendor" must not be over-matched.
  assert not _is_static_asset_path("vendorfoo")


def _spa_active(client):
  """The catch-all only registers when a static dir exists (the Docker
  pytest image has /app/static; a bare local checkout does not)."""
  r = client.get("/")
  return r.status_code == 200 and "text/html" in r.headers.get(
    "content-type", ""
  )


def test_vendor_miss_returns_404_not_html(client):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  for miss in (
    "/vendor/three@0.184.0/does-not-exist.js",
    "/vendor/three@0.184.0/missing.wasm",
    "/some/missing-module.mjs",
    "/data/missing.json",
  ):
    r = client.get(miss)
    assert r.status_code == 404, miss
    assert "text/html" not in r.headers.get("content-type", ""), miss


def test_app_route_still_serves_html(client):
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  r = client.get("/some-nonexistent-app-route")
  assert r.status_code == 200
  assert "text/html" in r.headers.get("content-type", "")
