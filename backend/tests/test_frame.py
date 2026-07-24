"""Tests for the mini-app frame endpoint."""

from test_app_fixtures import create_local_app


def _make_app(client, headers, name, jsx_source=None):
  kwargs = {"name": name, "description": "test"}
  if jsx_source is not None:
    kwargs["jsx_source"] = jsx_source
  return create_local_app(client, headers, **kwargs)["id"]


def test_frame_injects_app_id(client, owner_token):
  """GET /api/apps/{id}/frame replaces the placeholder app ID."""
  app_id = _make_app(
    client, {"Authorization": f"Bearer {owner_token}"}, "frame-test",
  )

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
  app_id = _make_app(
    client, {"Authorization": f"Bearer {owner_token}"}, "origin-test",
  )

  r = client.get(f"/api/apps/{app_id}/frame")
  html = r.text
  assert "UNSET" not in html
  assert "_FRAME_PARENT_ORIGIN" not in html


def test_frame_report_error_uses_location_origin(client, owner_token):
  """reportError() must postMessage with window.location.origin."""
  app_id = _make_app(
    client, {"Authorization": f"Bearer {owner_token}"}, "report-test",
  )

  r = client.get(f"/api/apps/{app_id}/frame")
  html = r.text
  assert "window.location.origin)" in html


def test_frame_is_public(client, owner_token):
  """Frame endpoint does not require authentication."""
  app_id = _make_app(
    client, {"Authorization": f"Bearer {owner_token}"}, "public-test",
  )

  # No auth header — should still work.
  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200


def test_frame_returns_etag_and_cache_control(client, owner_token):
  """Frame response carries an ETag derived from app.updated_at and
  `Cache-Control: no-cache` so the browser revalidates on every load."""
  app_id = _make_app(
    client, {"Authorization": f"Bearer {owner_token}"}, "etag-test",
  )

  r = client.get(f"/api/apps/{app_id}/frame")
  assert r.status_code == 200
  assert r.headers.get("etag", "").startswith('W/"')
  assert "no-cache" in r.headers.get("cache-control", "")
  sandbox = r.headers.get("content-security-policy", "")
  assert "sandbox allow-scripts" in sandbox
  # A target=_blank link must open as a normal destination page rather than
  # inheriting this frame's opaque origin (which breaks same-origin fetches and
  # signed-in storage on sites such as GitHub).
  assert "allow-popups-to-escape-sandbox" in sandbox
  assert "allow-same-origin" not in sandbox


def test_frame_304_on_matching_if_none_match(client, owner_token):
  """Repeated GET with the previous ETag returns 304 + empty body —
  closes the round-trip without re-sending the frame HTML."""
  app_id = _make_app(
    client, {"Authorization": f"Bearer {owner_token}"}, "etag-304-test",
  )

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
  # Response policy changes independently of the frame body. A conditional
  # direct-origin load must freshen cached CSP metadata rather than keeping the
  # old popup sandbox after deployment.
  assert r2.headers["content-security-policy"] == (
    r1.headers["content-security-policy"]
  )
  assert "allow-popups-to-escape-sandbox" in (
    r2.headers["content-security-policy"]
  )
  assert "no-cache" in r2.headers["cache-control"]


def test_frame_etag_changes_after_app_update(client, auth, db):
  """When app.updated_at changes (any PATCH), the ETag changes and
  a stale If-None-Match no longer 304s. This is the load-bearing
  invariant — without it the agent's fix would be invisible.
  """
  from datetime import UTC, datetime, timedelta

  from app import models

  app_id = _make_app(
    client, auth, "etag-bump-test",
    "export default function App() { return <div>old</div> }",
  )

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


def test_frame_self_themes_no_server_style_injection(client, auth):
  """Theme-as-data: the served frame is NOT theme-injected. It ships the
  shared pre-paint IIFE (which reads the __mobius-theme__ slot + the shell's
  same-origin localStorage and paints --bg / data-theme / color-scheme
  client-side) plus the data-theme="light" fallback CSS — but the SERVER no
  longer writes a <style> theme block, a data-theme attr on <html>, or a
  color-scheme rule into the served bytes.

  Replaces the old "frame paints light from the first byte" server-injection
  test: the no-flash guarantee now comes from the client pre-paint IIFE, not
  a server <style>. The frame bytes are theme-independent (see the ETag
  test), so this holds regardless of the active theme.css/theme-mode.
  """
  import pathlib

  from app.config import get_settings

  data_dir = pathlib.Path(get_settings().data_dir)
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; --text: #1c1b1a; }")
  (shared / "theme-mode").write_text('"light"')
  try:
    app_id = _make_app(client, auth, "self-theme-test")

    html = client.get(f"/api/apps/{app_id}/frame").text
    # The pre-paint IIFE is present (shared with index.html / PREPAINT_SRC).
    assert "__mobius-theme__" in html
    assert "data-theme" in html  # the IIFE sets it + the fallback CSS gates on it
    # The frame self-themes by luminance from localStorage/slot — it does NOT
    # carry a SERVER-injected <html data-theme="light"> attribute (that was the
    # old inject_theme_into_html path). <html> is the bare tag.
    assert '<html lang="en" data-theme=' not in html
    # No server-injected color-scheme rule either; color-scheme is set by the
    # client IIFE (root.style.colorScheme = mode), not a served <style>.
    assert "color-scheme:light" not in html
    assert "color-scheme:dark" not in html
  finally:
    (shared / "theme.css").unlink(missing_ok=True)
    (shared / "theme-mode").unlink(missing_ok=True)


def test_frame_prepaint_sets_color_scheme_client_side(client, auth):
  """The frame's pre-paint IIFE pins color-scheme to the resolved mode on the
  client (root.style.colorScheme = mode), so the iframe's UA-native surfaces
  (scrollbars, form controls, canvas) follow the owner's persisted mode
  instead of the OS prefers-color-scheme — without any server injection.

  Replaces test_frame_dark_theme_pins_color_scheme: the color-scheme pin
  moved from a server <style> to the shared client IIFE.
  """
  app_id = _make_app(client, auth, "scheme-iife-test")

  html = client.get(f"/api/apps/{app_id}/frame").text
  # The IIFE sets color-scheme from the resolved mode (mirrors applyTheme.js).
  assert "colorScheme = mode" in html
  assert "setAttribute('data-theme', mode)" in html


def test_frame_etag_theme_independent(client, auth):
  """Theme-as-data made the served frame theme-INDEPENDENT, so its ETag must
  NOT change when the active theme changes — and a request carrying the
  pre-toggle validator MUST 304 (the frame bytes are identical, so a refetch
  would be wasted).

  This INVERTS the old test_frame_etag_varies_by_theme: the frame used to be
  server-injected with the theme (so the ETag had to fold the theme in);
  now the client paints the theme and the frame bytes don't vary, so folding
  the theme into the validator would force needless refetches on every
  toggle. The ETag still keys on app.updated_at + the app-frame.html content
  hash, so a frame-file edit or app edit still busts it.
  """
  import pathlib

  from app.config import get_settings

  data_dir = pathlib.Path(get_settings().data_dir)
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)

  app_id = _make_app(client, auth, "theme-indep-etag-test")

  # Default (dark) theme.
  (shared / "theme.css").unlink(missing_ok=True)
  (shared / "theme-mode").unlink(missing_ok=True)
  from app import theme as theme_mod
  theme_mod._EFFECTIVE_THEME_MEMO.clear()
  dark_etag = client.get(f"/api/apps/{app_id}/frame").headers["etag"]

  # Switch to light — same app, same frame file, only the theme changed.
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; --text: #1c1b1a; }")
  (shared / "theme-mode").write_text('"light"')
  theme_mod._EFFECTIVE_THEME_MEMO.clear()
  try:
    light_etag = client.get(f"/api/apps/{app_id}/frame").headers["etag"]
    # The ETag is theme-independent now.
    assert light_etag == dark_etag, (
      "frame ETag must NOT vary by theme after theme-as-data"
    )
    # A request carrying the pre-toggle validator 304s (bytes unchanged).
    r2 = client.get(
      f"/api/apps/{app_id}/frame",
      headers={"If-None-Match": dark_etag},
    )
    assert r2.status_code == 304
  finally:
    (shared / "theme.css").unlink(missing_ok=True)
    (shared / "theme-mode").unlink(missing_ok=True)
    theme_mod._EFFECTIVE_THEME_MEMO.clear()
