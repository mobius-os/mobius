"""Packaged-app static assets (/app-assets/) must support HTTP caching.

These files (CubeRun's models/textures, ~19MB) change only on app
re-install, but the route used to serve every GET as a full 200 with
no validators — every open re-downloaded everything. The contract now:

- every 200 carries an ETag + Last-Modified;
- a conditional GET (If-None-Match / If-Modified-Since) answers a
  bodiless 304 when the client's copy is current;
- content-hashed filenames (main.8f3a2b1c.js) are immutable — a
  re-install that changes the bytes changes the name, so the URL is the
  validator and the browser may cache for a year;
- everything else keeps no-cache revalidate semantics.
"""

from pathlib import Path

from app import models
from app.database import SessionLocal


def _create_app(client, owner_token, name="CubeRun"):
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


def _write_static(app_id, relpath, content):
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app_id).one()
    target = Path(row.source_dir) / "static" / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
  finally:
    db.close()


def test_asset_get_carries_validators_and_revalidate_semantics(
  client, owner_token,
):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "models/ship.glb", "glb-bytes")

  r = client.get("/app-assets/cuberun/models/ship.glb")
  assert r.status_code == 200
  assert r.text == "glb-bytes"
  assert r.headers.get("etag")
  assert r.headers.get("last-modified")
  # Un-hashed names keep revalidate semantics (a re-install may replace
  # the bytes under the same name).
  assert "no-cache" in r.headers["cache-control"]
  assert "immutable" not in r.headers["cache-control"]
  assert r.headers["x-content-type-options"] == "nosniff"


def test_if_none_match_returns_bodiless_304(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "models/ship.glb", "glb-bytes")

  first = client.get("/app-assets/cuberun/models/ship.glb")
  assert first.status_code == 200
  etag = first.headers["etag"]

  again = client.get(
    "/app-assets/cuberun/models/ship.glb",
    headers={"If-None-Match": etag},
  )
  assert again.status_code == 304
  assert again.content == b""
  # The 304 re-states the validators + cache policy so the client can
  # extend its cache entry's lifetime.
  assert again.headers["etag"] == etag
  assert "no-cache" in again.headers["cache-control"]


def test_if_modified_since_returns_304(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "index.html", "<title>CubeRun</title>")

  first = client.get("/app-assets/cuberun/index.html")
  assert first.status_code == 200

  again = client.get(
    "/app-assets/cuberun/index.html",
    headers={"If-Modified-Since": first.headers["last-modified"]},
  )
  assert again.status_code == 304
  assert again.content == b""


def test_stale_etag_gets_full_response(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "main.js", "console.log(1)")

  r = client.get(
    "/app-assets/cuberun/main.js",
    headers={"If-None-Match": '"some-other-etag"'},
  )
  assert r.status_code == 200
  assert r.text == "console.log(1)"


def test_content_change_rotates_etag_and_defeats_stale_validator(
  client, owner_token,
):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "main.js", "console.log(1)")
  old = client.get("/app-assets/cuberun/main.js")
  old_etag = old.headers["etag"]

  # Re-install replaces the file in place (different size → different
  # validator even if mtime resolution were coarse).
  _write_static(app["id"], "main.js", "console.log('replaced')")

  r = client.get(
    "/app-assets/cuberun/main.js",
    headers={"If-None-Match": old_etag},
  )
  assert r.status_code == 200
  assert r.text == "console.log('replaced')"
  assert r.headers["etag"] != old_etag


def test_hashed_filename_is_immutable(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "js/main.8f3a2b1c.js", "console.log('hashed')")

  r = client.get("/app-assets/cuberun/js/main.8f3a2b1c.js")
  assert r.status_code == 200
  assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

  # Immutable assets still honor conditional requests — a client that
  # revalidates anyway (e.g. force-refresh) gets the cheap 304.
  again = client.get(
    "/app-assets/cuberun/js/main.8f3a2b1c.js",
    headers={"If-None-Match": r.headers["etag"]},
  )
  assert again.status_code == 304
  assert again.headers["cache-control"] == (
    "public, max-age=31536000, immutable"
  )


def test_dash_separated_hash_is_immutable_and_short_hex_is_not(
  client, owner_token,
):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "chunk-a1b2c3d4e5f6.js", "x")
  _write_static(app["id"], "cafe.js", "y")

  hashed = client.get("/app-assets/cuberun/chunk-a1b2c3d4e5f6.js")
  assert "immutable" in hashed.headers["cache-control"]

  # A short hex-looking word (cafe) must NOT be treated as a content
  # hash — those files can be replaced in place on re-install.
  plain = client.get("/app-assets/cuberun/cafe.js")
  assert "immutable" not in plain.headers["cache-control"]
  assert "no-cache" in plain.headers["cache-control"]


def test_by_id_route_supports_conditional_requests(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "index.html", "<title>CubeRun</title>")

  first = client.get(f"/app-assets/by-id/{app['id']}/index.html")
  assert first.status_code == 200

  again = client.get(
    f"/app-assets/by-id/{app['id']}/index.html",
    headers={"If-None-Match": first.headers["etag"]},
  )
  assert again.status_code == 304


def test_security_guards_run_before_caching(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "index.html", "<title>CubeRun</title>")

  # Traversal is still rejected even when the request looks like a
  # cheap revalidation.
  traversal = client.get(
    "/app-assets/cuberun/../index.html",
    headers={"If-None-Match": '"anything"'},
  )
  assert traversal.status_code in (404, 405)

  unknown = client.get(
    "/app-assets/no-such-app/index.html",
    headers={"If-None-Match": '"anything"'},
  )
  assert unknown.status_code == 404


def test_head_is_supported_with_validators_and_no_body(client, owner_token):
  app = _create_app(client, owner_token)
  _write_static(app["id"], "index.html", "<title>CubeRun</title>")

  r = client.head("/app-assets/cuberun/index.html")
  assert r.status_code == 200
  assert r.content == b""
  assert r.headers.get("etag")
  # A 405 here pushes client-side probes into a `Range: bytes=0-0` GET
  # fallback — the trigger for the partial-body cache poisoning below.

  by_id = client.head(f"/app-assets/by-id/{app['id']}/index.html")
  assert by_id.status_code == 200
  assert by_id.content == b""


def test_range_is_ignored_on_revalidating_assets(client, owner_token):
  """A ranged GET of a no-cache asset must get the FULL 200 body.

  Serving a 206 slice of a `no-cache` + ETag asset poisoned Chromium's
  HTTP cache: the stored slice revalidated 304 and was then served as a
  status-200 full response, one byte long — CubeRun's index.html became
  the single character '<' for every later open (2026-06-12 outage).
  RFC 9110 explicitly allows ignoring Range, so these assets do.
  """
  app = _create_app(client, owner_token)
  _write_static(app["id"], "index.html", "<title>CubeRun</title>")

  r = client.get(
    "/app-assets/cuberun/index.html",
    headers={"Range": "bytes=0-0"},
  )
  assert r.status_code == 200
  assert r.text == "<title>CubeRun</title>"
  assert "content-range" not in r.headers


def test_range_still_works_on_immutable_hashed_assets(client, owner_token):
  """Hashed-name assets keep 206 support (media seeking) — safe because
  immutable entries are never revalidated, so the 304-slice trap above
  cannot fire for them."""
  app = _create_app(client, owner_token)
  _write_static(app["id"], "media/track.a1b2c3d4.mp3", "0123456789")

  r = client.get(
    "/app-assets/cuberun/media/track.a1b2c3d4.mp3",
    headers={"Range": "bytes=0-3"},
  )
  assert r.status_code == 206
  assert r.content == b"0123"
  assert r.headers["content-range"] == "bytes 0-3/10"


def test_all_digit_name_is_not_immutable(client, owner_token):
  """A date-stamped, all-digit segment must NOT be treated as a content
  hash. _HASHED_ASSET_NAME used to match any 8+ hex run, so IMG-20260612.png
  (replaced in place on re-upload) was served immutable for a year. The
  lookahead requiring an alphabetic hex digit fixes the false positive while
  keeping genuine digests (which always mix in a-f) immutable."""
  app = _create_app(client, owner_token)
  _write_static(app["id"], "IMG-20260612.png", "img-bytes")
  _write_static(app["id"], "report.20260101.html", "<p>r</p>")
  _write_static(app["id"], "bundle.cafe1234.js", "console.log(1)")

  all_digit_png = client.get("/app-assets/cuberun/IMG-20260612.png")
  assert "immutable" not in all_digit_png.headers["cache-control"]
  assert "no-cache" in all_digit_png.headers["cache-control"]

  all_digit_html = client.get("/app-assets/cuberun/report.20260101.html")
  assert "immutable" not in all_digit_html.headers["cache-control"]

  # A delimited digest with at least one a-f char is still immutable.
  real_hash = client.get("/app-assets/cuberun/bundle.cafe1234.js")
  assert "immutable" in real_hash.headers["cache-control"]


# ---------------------------------------------------------------------------
# The module + sw.js routes share the /app-assets Range-poisoning class: both
# serve a revalidating FileResponse (no-cache + stable ETag) that HONORS a
# `Range` header. A `Range: bytes=0-0` probe yields a 1-byte 206 that Chromium
# caches and later serves as a status-200 full body — a one-byte module (black
# mini-app until the next app update) or a one-byte service worker. These tests
# lock in the structural fix: Range is stripped, so the full-body 200 is the
# only answer; HEAD is registered so probes don't fall back to ranged GETs.
# ---------------------------------------------------------------------------

_MODULE_JSX = "export default function App() { return null }"


def _create_module_app(client, owner_token, offline=False):
  r = client.post(
    "/api/apps/",
    json={
      "name": "ModApp",
      "description": "x",
      "jsx_source": _MODULE_JSX,
      "offline_capable": offline,
    },
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  return r.json()


def test_module_range_probe_gets_full_body_200(client, owner_token):
  app = _create_module_app(client, owner_token)
  url = f"/api/apps/{app['id']}/module?token={owner_token}"

  full = client.get(url)
  assert full.status_code == 200
  body = full.content
  assert len(body) > 1

  # A bytes=0-0 probe must NOT yield a 1-byte 206 — the poisoning trigger.
  ranged = client.get(url, headers={"Range": "bytes=0-0"})
  assert ranged.status_code == 200
  assert ranged.content == body
  assert "content-range" not in ranged.headers
  # Still a revalidating response (so a 304 stays cheap), just never partial.
  assert "no-cache" in ranged.headers["cache-control"]


def test_module_head_is_supported_not_405(client, owner_token):
  app = _create_module_app(client, owner_token)
  url = f"/api/apps/{app['id']}/module?token={owner_token}"

  r = client.head(url)
  # A 405 here pushes client probes into a `Range: bytes=0-0` GET fallback —
  # exactly the poisoning trigger. HEAD must be registered alongside GET.
  assert r.status_code == 200
  assert r.content == b""
  assert r.headers.get("etag")


def test_frame_head_is_supported_not_405(client, owner_token):
  app = _create_module_app(client, owner_token)
  r = client.head(f"/api/apps/{app['id']}/frame")
  assert r.status_code == 200
  assert r.content == b""


def test_sw_js_range_probe_gets_full_body_200(client):
  """sw.js carries no-cache + the mtime ETag FileResponse sets, so it is
  the same revalidating Range-poisoning class. A bytes=0-0 probe must get the
  full body, never a 1-byte 206 that would later serve as a 1-byte SW."""
  import pytest

  full = client.get("/sw.js")
  # The catch-all SPA route only exists when a static build is present; a
  # build without sw.js falls through to the index.html SPA fallback (an
  # HTMLResponse — text/html, no Last-Modified). Gate on the FileResponse
  # signature (JS content-type + Last-Modified) so we only assert when the
  # actual sw.js branch ran. The branch is identical in shape to the
  # module/asset ones above, which DO run here.
  ctype = full.headers.get("content-type", "")
  served_from_file = (
    full.status_code == 200
    and "javascript" in ctype
    and full.headers.get("last-modified") is not None
  )
  if not served_from_file:
    pytest.skip("sw.js not served from this build's static dir")
  body = full.content
  assert len(body) > 1

  ranged = client.get("/sw.js", headers={"Range": "bytes=0-0"})
  assert ranged.status_code == 200
  assert ranged.content == body
  assert "content-range" not in ranged.headers


def _activity_events():
  import json
  from app.config import get_settings
  path = Path(get_settings().data_dir) / "logs" / "activity.jsonl"
  if not path.exists():
    return []
  return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_frame_head_probe_does_not_log_app_open(client, owner_token):
  """A HEAD on /frame is an existence probe (the reason HEAD is registered:
  so probes don't fall back to ranged GETs), not a real open — it must not
  inflate app_open analytics. A GET still counts."""
  app = _create_module_app(client, owner_token)
  app_id = app["id"]

  before = sum(
    1 for e in _activity_events()
    if e.get("ev") == "app_open" and e.get("app_id") == app_id
  )

  client.head(f"/api/apps/{app_id}/frame")
  after_head = sum(
    1 for e in _activity_events()
    if e.get("ev") == "app_open" and e.get("app_id") == app_id
  )
  assert after_head == before, "HEAD must not log an app_open"

  client.get(f"/api/apps/{app_id}/frame")
  after_get = sum(
    1 for e in _activity_events()
    if e.get("ev") == "app_open" and e.get("app_id") == app_id
  )
  assert after_get == before + 1, "GET must still log app_open"
