"""Integration: DELETE / recover / install wiring for durable core-app
suppression (the marker the boot seeder honors). Complements the pure-helper
unit tests in test_core_app_suppress.py — the wiring is where the reversible
soft-delete pattern historically hides bugs.
"""

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


def test_delete_core_app_writes_marker_recover_clears_it(client, auth):
  core_app_suppress.clear_suppressed(get_settings().data_dir, "memory")
  app = _create(client, auth, "Memory")
  # The core slug must land on its canonical value or the marker key is wrong.
  assert app["slug"] == "memory", app
  assert not _marker("memory").exists()

  # Delete → the boot seeder must never resurrect it → marker written.
  assert client.delete(f"/api/apps/{app['id']}", headers=auth).status_code == 204
  assert _marker("memory").exists()

  # Recover (within TTL) → owner brought it back → marker cleared.
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
