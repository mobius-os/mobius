"""Tests for the mini-app frame endpoint."""


def test_frame_injects_app_id(client, owner_token):
  """GET /api/apps/{id}/frame replaces the placeholder app ID."""
  r = client.post("/api/apps/", json={
    "name": "frame-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200
  html = r.text
  assert f"var _FRAME_APP_ID = \"{app_id}\"" in html
  assert "var _FRAME_APP_ID = 'unknown'" not in html


def test_frame_has_no_unset_origin(client, owner_token):
  """The frame must not contain _FRAME_PARENT_ORIGIN = 'UNSET'.

  Regression test for 2d17109: the frame-origin refactor switched the
  module script to window.location.origin but left the error panel's
  reportError() using the old _FRAME_PARENT_ORIGIN variable, which the
  server no longer substitutes. postMessage(..., 'UNSET') silently fails,
  so "Tell agent to fix" was broken for crashed apps.
  """
  r = client.post("/api/apps/", json={
    "name": "origin-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  html = r.text
  assert "UNSET" not in html
  assert "_FRAME_PARENT_ORIGIN" not in html


def test_frame_report_error_uses_location_origin(client, owner_token):
  """reportError() must postMessage with window.location.origin."""
  r = client.post("/api/apps/", json={
    "name": "report-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  html = r.text
  assert "window.location.origin)" in html


def test_frame_is_public(client, owner_token):
  """Frame endpoint does not require authentication."""
  r = client.post("/api/apps/", json={
    "name": "public-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  # No auth header — should still work.
  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200


def test_frame_returns_etag_and_cache_control(client, owner_token):
  """Frame response carries an ETag derived from app.updated_at and
  `Cache-Control: no-cache` so the browser revalidates on every load."""
  r = client.post("/api/apps/", json={
    "name": "etag-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200
  assert r.headers.get("etag", "").startswith('W/"')
  assert "no-cache" in r.headers.get("cache-control", "")


def test_frame_304_on_matching_if_none_match(client, owner_token):
  """Repeated GET with the previous ETag returns 304 + empty body —
  closes the round-trip without re-sending the frame HTML."""
  r = client.post("/api/apps/", json={
    "name": "etag-304-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  r1 = client.get(f"/api/apps/{app_id}/frame")
  etag = r1.headers["etag"]

  r2 = client.get(
    f"/api/apps/{app_id}/frame",
    headers={"If-None-Match": etag},
  )
  assert r2.status_code == 304
  assert r2.text == ""
  # ETag is preserved on 304 so the browser keeps its validator.
  assert r2.headers["etag"] == etag


def test_frame_etag_changes_after_app_update(client, auth, db):
  """When app.updated_at changes (any PATCH), the ETag changes and
  a stale If-None-Match no longer 304s. This is the load-bearing
  invariant — without it the agent's fix would be invisible.
  """
  from datetime import UTC, datetime, timedelta

  from app import models

  r = client.post("/api/apps/", json={
    "name": "etag-bump-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>old</div> }",
  }, headers=auth)
  app_id = r.json()["id"]

  r1 = client.get(f"/api/apps/{app_id}/frame")
  old_etag = r1.headers["etag"]

  # Bump updated_at to an EXPLICIT future timestamp rather than
  # sleeping + relying on the SQLAlchemy onupdate hook. The hook
  # uses datetime.now(UTC) which has microsecond resolution in
  # Python, but two calls inside the same OS scheduler quantum can
  # return identical timestamps under load — flake risk on CI. An
  # explicit future timestamp is deterministic.
  app = db.query(models.App).filter(models.App.id == app_id).first()
  app.updated_at = datetime.now(UTC) + timedelta(seconds=1)
  db.commit()

  r2 = client.get(
    f"/api/apps/{app_id}/frame",
    headers={"If-None-Match": old_etag},
  )
  # The old ETag no longer matches — server should NOT 304.
  assert r2.status_code == 200
  assert r2.headers["etag"] != old_etag


def test_frame_paints_light_theme_from_first_byte(client, auth):
  """In light mode the SERVED frame already carries the light theme — its
  injected <style> sets --bg to the light value and <html> gets
  data-theme="light".

  Regression test for the "opening an app changes the background" bug: the
  frame's hardcoded :root fallback is dark, and the theme used to reach the
  frame ONLY via a post-load postMessage. In light mode that meant the
  served HTML painted dark until (or unless) frame-init landed — a dark panel
  against the light shell. Server-injecting the theme closes that window.
  """
  import pathlib

  from app.config import get_settings

  data_dir = pathlib.Path(get_settings().data_dir)
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; --text: #1c1b1a; }")
  (shared / "theme-mode").write_text('"light"')
  try:
    r = client.post("/api/apps/", json={
      "name": "light-frame-test",
      "description": "test",
      "jsx_source": "export default function App() { return <div>hi</div> }",
    }, headers=auth)
    app_id = r.json()["id"]

    html = client.get(f"/api/apps/{app_id}/frame").text
    # The injected light theme is present from the first byte (no postMessage).
    assert 'data-theme="light"' in html
    assert "#f0eeeb" in html
    # The native color-scheme is pinned to the shell mode too, so the
    # iframe's UA-native surfaces (scrollbars, form controls, canvas)
    # follow the shell instead of the OS prefers-color-scheme. Mirrors the
    # dark-branch assertion in test_frame_dark_theme_pins_color_scheme.
    assert "color-scheme:light" in html
  finally:
    (shared / "theme.css").unlink(missing_ok=True)
    (shared / "theme-mode").unlink(missing_ok=True)


def test_frame_dark_theme_pins_color_scheme(client, auth):
  """A dark-mode served frame carries BOTH data-theme="dark" and an explicit
  dark color-scheme, so the iframe's native widgets / scrollbars / canvas
  follow the shell mode regardless of the OS prefers-color-scheme.

  Regression test for "Notes content-area flashes a light canvas in dark
  mode": data-theme drives shell CSS, but the UA-native color-scheme came
  from the OS preference until we injected an explicit :root{color-scheme}.
  """
  import pathlib

  from app.config import get_settings

  data_dir = pathlib.Path(get_settings().data_dir)
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  (shared / "theme.css").write_text(":root { --bg: #0d0d0d; --text: #ececec; }")
  (shared / "theme-mode").write_text('"dark"')
  try:
    r = client.post("/api/apps/", json={
      "name": "dark-scheme-test",
      "description": "test",
      "jsx_source": "export default function App() { return <div>hi</div> }",
    }, headers=auth)
    app_id = r.json()["id"]

    html = client.get(f"/api/apps/{app_id}/frame").text
    assert 'data-theme="dark"' in html
    # The explicit native color-scheme is injected from the first byte.
    assert "color-scheme:dark" in html
  finally:
    (shared / "theme.css").unlink(missing_ok=True)
    (shared / "theme-mode").unlink(missing_ok=True)


def test_frame_etag_varies_by_theme(client, auth):
  """The frame ETag must change when the active theme changes, so a cached
  frame revalidates after a light/dark toggle instead of 304-ing against a
  stale validator and running the wrong-theme frame forever (the same trap
  the frame content hash closes for code edits)."""
  import pathlib

  from app.config import get_settings

  data_dir = pathlib.Path(get_settings().data_dir)
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)

  r = client.post("/api/apps/", json={
    "name": "theme-etag-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers=auth)
  app_id = r.json()["id"]

  # Default (dark) theme.
  (shared / "theme.css").unlink(missing_ok=True)
  (shared / "theme-mode").unlink(missing_ok=True)
  dark_etag = client.get(f"/api/apps/{app_id}/frame").headers["etag"]

  # Switch to light — same app, same frame file, only the theme changed.
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; --text: #1c1b1a; }")
  (shared / "theme-mode").write_text('"light"')
  try:
    light_etag = client.get(f"/api/apps/{app_id}/frame").headers["etag"]
    assert light_etag != dark_etag
    # A request carrying the stale dark validator must NOT 304.
    r2 = client.get(
      f"/api/apps/{app_id}/frame",
      headers={"If-None-Match": dark_etag},
    )
    assert r2.status_code == 200
  finally:
    (shared / "theme.css").unlink(missing_ok=True)
    (shared / "theme-mode").unlink(missing_ok=True)


def test_module_returns_etag(client, auth):
  """The module endpoint uses the same ETag scheme as the frame, so
  the iframe's dynamic `import()` revalidates with `If-None-Match`."""
  r = client.post("/api/apps/", json={
    "name": "etag-module-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers=auth)
  app_id = r.json()["id"]
  token = auth["Authorization"].split()[1]

  r = client.get(f"/api/apps/{app_id}/module?token={token}")
  assert r.status_code == 200
  assert r.headers.get("etag", "").startswith('W/"')

  r2 = client.get(
    f"/api/apps/{app_id}/module?token={token}",
    headers={"If-None-Match": r.headers["etag"]},
  )
  assert r2.status_code == 304
