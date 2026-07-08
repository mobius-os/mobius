"""App registry lifecycle tests."""

from pathlib import Path

from app.config import get_settings


def test_create_app_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/apps/",
    json={
      "name": "blocked-app",
      "description": "test",
      "jsx_source": "export default function App() { return <div/> }",
    },
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_update_app_rejects_cross_site_request(client, auth):
  cross = client.patch(
    "/api/apps/1",
    json={"name": "blocked-app"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_delete_then_purge_removes_non_slug_source_dir(client, auth, db):
  """Delete is soft (the source tree survives for recovery); the TTL purge
  removes it, using the stored source_dir rather than the display-name slug.
  Feature 110."""
  from datetime import datetime, timedelta, UTC
  from app import models
  source_dir = Path(get_settings().data_dir) / "apps" / "My App (draft)"
  source_dir.mkdir(parents=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )

  r = client.post("/api/apps/", json={
    "name": "My App (draft)",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201
  app_id = r.json()["id"]

  # Soft delete tombstones the app but preserves its source tree.
  r = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert r.status_code == 204
  assert source_dir.exists()

  # Age the tombstone past the TTL; the next list call purges it, resolving the
  # tree via the stored source_dir (not the "My App (draft)" display name).
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  client.get("/api/apps/", headers=auth)
  assert not source_dir.exists()


def test_app_token_can_update_own_schedule_only(client, auth, monkeypatch):
  calls = []

  def fake_register(slug, schedule_expr, job_path, app_id=None):
    calls.append((slug, schedule_expr, job_path.name, app_id))

  monkeypatch.setattr("app.install._register_cron", fake_register)
  source_dir = Path(get_settings().data_dir) / "apps" / "news"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")

  r = client.post("/api/apps/", json={
    "name": "News",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]

  token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {token}"}

  r = client.post(
    f"/api/apps/{app_id}/schedule",
    json={"cron": "15 7 * * *", "job": "fetch.sh"},
    headers=app_auth,
  )
  assert r.status_code == 200, r.text
  assert calls == [("news", "15 7 * * *", "fetch.sh", app_id)]

  r = client.post(
    f"/api/apps/{app_id + 1}/schedule",
    json={"cron": "15 8 * * *", "job": "fetch.sh"},
    headers=app_auth,
  )
  assert r.status_code == 403


def test_app_schedules_are_readable_by_app_tokens(client, auth):
  source_dir = Path(get_settings().data_dir) / "apps" / "news"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "mobius.json").write_text(
    '{"schedule":{"default":"0 10 * * *","job":"fetch.sh"}}',
    encoding="utf-8",
  )

  r = client.post("/api/apps/", json={
    "name": "News",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201, r.text

  tasks = client.post("/api/apps/", json={
    "name": "Tasks",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
  }, headers=auth).json()
  token = client.post(
    "/api/auth/app-token", json={"app_id": tasks["id"]}, headers=auth,
  ).json()["token"]

  r = client.get(
    "/api/apps/schedules",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 200, r.text
  assert r.json() == [{
    "id": 1,
    "name": "News",
    "slug": "news",
    "cron": "0 10 * * *",
    "job": "fetch.sh",
    "next_run": None,
  }]


def test_app_schedules_prefer_init_cron_over_manifest(client, auth):
  source_dir = Path(get_settings().data_dir) / "apps" / "reflection"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "mobius.json").write_text(
    '{"schedule":{"default":"0 10 * * *","job":"fetch.sh"}}',
    encoding="utf-8",
  )
  (source_dir / "init-cron.sh").write_text(
    f'ENTRY="0 6 * * * {source_dir}/fetch.sh 56"\n',
    encoding="utf-8",
  )

  r = client.post("/api/apps/", json={
    "name": "Reflection",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201, r.text

  r = client.get("/api/apps/schedules", headers=auth)
  assert r.status_code == 200, r.text
  assert [(job["cron"], job["job"]) for job in r.json()] == [
    ("0 6 * * *", "fetch.sh")
  ]


def _make_icon_app(client, auth, db):
  """An app row whose `icon_png` is a large (512px) PNG, so a ?size= variant
  is provably smaller. Returns the app id."""
  import io
  from PIL import Image
  from app import models
  r = client.post("/api/apps/", json={
    "name": "Iconic",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
  }, headers=auth)
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  buf = io.BytesIO()
  # RGBA with varied pixels so optimize=True can't collapse it to a few bytes.
  img = Image.new("RGBA", (512, 512))
  img.putdata([
    ((x * 7) % 256, (y * 5) % 256, (x + y) % 256, 255)
    for y in range(512) for x in range(512)
  ])
  img.save(buf, format="PNG")
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.icon_png = buf.getvalue()
  db.commit()
  return app_id


def test_get_icon_size_returns_smaller_cached_variant(client, auth, db):
  """?size= serves a Pillow-downscaled PNG (fewer bytes) with the size folded
  into the ETag and a long cache header; default (no size) is unchanged."""
  app_id = _make_icon_app(client, auth, db)
  _CC = "public, max-age=3600, stale-while-revalidate=86400"

  full = client.get(f"/api/apps/{app_id}/icon")
  assert full.status_code == 200
  assert full.headers["Cache-Control"] == _CC
  full_etag = full.headers["ETag"]

  small = client.get(f"/api/apps/{app_id}/icon", params={"size": 64})
  assert small.status_code == 200
  assert small.headers["Content-Type"] == "image/png"
  assert small.headers["Cache-Control"] == _CC
  # The downscaled variant is strictly smaller than the full-res icon.
  assert len(small.content) < len(full.content)
  # Its ETag folds the size in, so it caches independently of the full-res one.
  assert small.headers["ETag"] != full_etag
  assert small.headers["ETag"].endswith('-64"')

  # The variant ETag round-trips to a 304 with the same cache header.
  again = client.get(
    f"/api/apps/{app_id}/icon",
    params={"size": 64},
    headers={"If-None-Match": small.headers["ETag"]},
  )
  assert again.status_code == 304
  assert again.headers["Cache-Control"] == _CC


def test_get_icon_rejects_unsupported_size(client, auth, db):
  """An unsupported ?size= is a 400 so the variant cache can't be flooded."""
  app_id = _make_icon_app(client, auth, db)
  r = client.get(f"/api/apps/{app_id}/icon", params={"size": 999})
  assert r.status_code == 400


def test_get_icon_variant_is_byte_identical_and_cached_on_disk(client, auth, db):
  """A ?size= variant is deterministic: two fetches return identical bytes, and
  the second is served from the icon_cache (RAM/disk) rather than recomputed.
  Asserting on the on-disk cache file proves the downscale is memoized, not
  re-run per request — the fix for the staggered icon trickle."""
  from app import icon_cache
  from app.config import get_settings
  import pathlib

  app_id = _make_icon_app(client, auth, db)

  first = client.get(f"/api/apps/{app_id}/icon", params={"size": 128})
  assert first.status_code == 200
  second = client.get(f"/api/apps/{app_id}/icon", params={"size": 128})
  assert second.status_code == 200
  # Deterministic render → byte-identical across requests.
  assert first.content == second.content

  # The downscaled bytes were written to the on-disk cache and match the
  # response body exactly (so a warm hit serves these bytes with no Pillow).
  cache_dir = pathlib.Path(get_settings().data_dir) / "compiled" / "icons"
  files = list(cache_dir.glob(f"{app_id}-embed-128-*"))
  assert files, f"expected a cached icon variant under {cache_dir}"
  assert files[0].read_bytes() == first.content


def test_get_icon_variant_cache_busts_on_app_update(client, auth, db):
  """Changing the stored icon advances app.updated_at, which changes the cache
  key — so the new icon is served, never the stale cached variant."""
  import io
  from PIL import Image
  from app import models

  app_id = _make_icon_app(client, auth, db)
  before = client.get(f"/api/apps/{app_id}/icon", params={"size": 64})
  assert before.status_code == 200

  # Replace the stored icon with a visibly different image and bump updated_at
  # the way a real icon upload does.
  buf = io.BytesIO()
  img = Image.new("RGBA", (512, 512))
  img.putdata([
    ((x * 3) % 256, (y * 11) % 256, (x * y) % 256, 255)
    for y in range(512) for x in range(512)
  ])
  img.save(buf, format="PNG")
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.icon_png = buf.getvalue()
  row.updated_at = row.updated_at.replace(microsecond=(row.updated_at.microsecond + 1) % 1000000)
  db.commit()

  after = client.get(f"/api/apps/{app_id}/icon", params={"size": 64})
  assert after.status_code == 200
  assert after.content != before.content, "stale cached variant served after update"
