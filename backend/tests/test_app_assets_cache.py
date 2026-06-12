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
