"""Integration coverage for the retired core-app suppression marker."""

from pathlib import Path

from app import core_app_suppress
from app.config import get_settings


def _marker(slug):
  return (
    Path(get_settings().data_dir) / "shared" / "suppressed-core-apps" / slug
  )


def _create(client, auth, name):
  r = client.post(
    "/api/apps/",
    headers=auth,
    json={
      "name": name,
      "description": "t",
      "jsx_source": "export default function App() { return <div/> }",
    },
  )
  assert r.status_code == 201, r.text
  return r.json()


def test_delete_memory_app_writes_no_marker_recover_keeps_none(client, auth):
  core_app_suppress.clear_suppressed(get_settings().data_dir, "memory")
  app = _create(client, auth, "Memory")
  assert app["slug"] == "memory", app
  assert not _marker("memory").exists()

  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  assert not _marker("memory").exists()

  r = client.post(f"/api/apps/{app['id']}/recover", headers=auth)
  assert r.status_code == 200, r.text
  assert not _marker("memory").exists()


def test_delete_ordinary_app_writes_no_marker(client, auth):
  app = _create(client, auth, "Notes")
  slug = app["slug"]
  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  # Ordinary apps aren't re-seeded, so no marker — deleting them must not
  # scatter suppression files.
  assert not _marker(slug).exists()
  assert core_app_suppress.list_suppressed(get_settings().data_dir) == set()
