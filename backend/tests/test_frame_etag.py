"""The /frame ETag must reflect the shared app-frame.html, not just
`app.updated_at`.

The frame serves the runtime shell (importmap + bootstrap), which changes
independently of any app row. If the validator ignored the frame file, an
edit to app-frame.html (e.g. bumping a vendored import path) would never
reach an already-installed PWA — it would revalidate against an unchanged
validator, get a 304, and run the stale frame forever. That is the exact
failure that pinned a client to a dropped `/vendor/three/` path.
"""

import datetime
from types import SimpleNamespace

from app.routes.apps import _frame_etag


def test_frame_etag_busts_when_frame_content_changes(tmp_path):
  f = tmp_path / "app-frame.html"
  f.write_text("<html>v1</html>")
  app = SimpleNamespace(updated_at=datetime.datetime(2026, 5, 30, 12, 0, 0))

  e1 = _frame_etag(app, f)
  assert e1 and e1.startswith('W/"')

  # Editing the frame content (e.g. a new importmap) must change the
  # validator even though app.updated_at is unchanged — content hash,
  # so it doesn't depend on mtime.
  f.write_text("<html>v2 — new importmap</html>")
  e2 = _frame_etag(app, f)
  assert e2 != e1


def test_frame_etag_busts_when_app_updates(tmp_path):
  f = tmp_path / "app-frame.html"
  f.write_text("<html>frame</html>")
  a1 = SimpleNamespace(updated_at=datetime.datetime(2026, 5, 30, 12, 0, 0))
  a2 = SimpleNamespace(updated_at=datetime.datetime(2026, 5, 30, 12, 0, 1))
  assert _frame_etag(a1, f) != _frame_etag(a2, f)


def test_frame_etag_none_without_any_signal(tmp_path):
  app = SimpleNamespace(updated_at=None)
  missing = tmp_path / "does-not-exist.html"
  assert _frame_etag(app, missing) is None


def _create_app(client, owner_token, name="frame-etag-demo"):
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


def test_frame_route_serves_compound_etag(client, owner_token):
  """The live /frame response carries the compound validator (app +
  frame), i.e. two parts joined by '-', not the app-only ETag."""
  app = _create_app(client, owner_token)
  r = client.get(f"/api/apps/{app['id']}/frame")
  if r.status_code == 404:
    # No app-frame.html resolvable in this environment (bare local
    # checkout); the unit tests above still cover the logic.
    return
  assert r.status_code == 200, r.text
  etag = r.headers.get("etag", "")
  assert etag.startswith('W/"') and "-" in etag, etag
