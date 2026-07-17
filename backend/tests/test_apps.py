"""App registry lifecycle tests."""

from pathlib import Path
from unittest.mock import patch

from app import models
from app.config import get_settings


def _service_auth():
  token = (Path(get_settings().data_dir) / "service-token.txt").read_text()
  return {"Authorization": f"Bearer {token}"}


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


def test_create_app_publishes_pane_neutral_ready_relationship(client, auth):
  with patch("app.routes.apps.get_system_broadcast") as mock_get_broadcast:
    response = client.post(
      "/api/apps/",
      json={
        "name": "Trip planner",
        "description": "test",
        "jsx_source": "export default function App() { return <div/> }",
        "chat_id": "building-chat",
      },
      headers=auth,
    )

  assert response.status_code == 201, response.text
  mock_get_broadcast.return_value.publish.assert_called_once_with({
    "type": "app_created",
    "appId": str(response.json()["id"]),
    "chatId": "building-chat",
  })


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
  source_dir.mkdir(parents=True, exist_ok=True)
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


def test_delete_then_purge_preserves_platform_core_source(client, auth, db):
  """A legacy row may point at /data/platform/core-apps; TTL purge must not
  remove source outside /data/apps."""
  from datetime import datetime, timedelta, UTC
  from app import models
  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "platform" / "core-apps" / "memory"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  app = models.App(
    name="Memory",
    description="legacy platform row",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(source_dir),
    slug="memory",
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  app_id = app.id

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  client.get("/api/apps/", headers=auth)

  assert source_dir.exists()
  assert (source_dir / "index.jsx").exists()


def test_delete_scheduled_app_disables_own_cron_replay(
  client, auth,
):
  """Deleting a scheduled app tombstones the replay script in its source tree."""
  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "apps" / "reflection"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  replay = source_dir / "init-cron.sh"
  replay.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

  r = client.post("/api/apps/", json={
    "name": "Reflection",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=auth)
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  assert not replay.exists()
  assert (source_dir / "init-cron.sh.tombstoned").exists()
  assert source_dir.exists()


def test_delete_legacy_platform_app_disables_runtime_cron_replay(
  client, auth, db, monkeypatch,
):
  """Old platform-core rows also had a replay sidecar under /data/apps/<slug>."""
  monkeypatch.setattr("app.install._unregister_cron", lambda _source: None)
  data_dir = Path(get_settings().data_dir)
  platform_source = data_dir / "platform" / "core-apps" / "reflection"
  platform_source.mkdir(parents=True, exist_ok=True)
  (platform_source / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  runtime_dir = data_dir / "apps" / "reflection"
  runtime_dir.mkdir(parents=True, exist_ok=True)
  replay = runtime_dir / "init-cron.sh"
  replay.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

  app = models.App(
    name="Reflection",
    description="legacy platform app",
    jsx_source="export default function App() { return <div/> }",
    slug="reflection",
    source_dir=str(platform_source),
  )
  db.add(app)
  db.commit()

  assert client.delete(f"/api/apps/{app.id}", headers=auth).status_code == 204

  assert not replay.exists()
  assert (runtime_dir / "init-cron.sh.tombstoned").exists()
  assert platform_source.exists()


def test_app_token_can_update_own_schedule_only(client, auth, monkeypatch):
  calls = []

  def fake_register(slug, schedule_expr, job_path, app_id=None):
    calls.append((slug, schedule_expr, job_path.name, app_id))

  monkeypatch.setattr("app.install._register_cron", fake_register)
  source_dir = Path(get_settings().data_dir) / "apps" / "news"
  source_dir.mkdir(parents=True, exist_ok=True)
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


def test_platform_source_patch_rejected_and_store_identity_preserved(client, auth, db):
  from app import models
  data_dir = Path(get_settings().data_dir)
  old_source = data_dir / "apps" / "memory"
  old_source.mkdir(parents=True, exist_ok=True)
  source_dir = data_dir / "platform" / "core-apps" / "memory"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  app = models.App(
    name="Memory",
    description="store-managed core",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(old_source),
    slug="memory",
    manifest_url="https://raw.githubusercontent.com/mobius-os/app-memory/main/mobius.json",
    version="1.2.3",
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  app_id = app.id

  r = client.patch(
    f"/api/apps/{app_id}",
    json={"source_dir": str(source_dir)},
    headers=_service_auth(),
  )
  assert r.status_code == 400, r.text
  db.refresh(app)
  assert app.source_dir == str(old_source)
  assert app.manifest_url == "https://raw.githubusercontent.com/mobius-os/app-memory/main/mobius.json"
  assert app.version == "1.2.3"


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
  }, headers=_service_auth())
  assert r.status_code == 201, r.text

  r = client.get("/api/apps/schedules", headers=auth)
  assert r.status_code == 200, r.text
  assert [(job["cron"], job["job"]) for job in r.json()] == [
    ("0 6 * * *", "fetch.sh")
  ]


def test_app_schedules_resolve_supervised_runner_job(client, auth):
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")

  r = client.post("/api/apps/", json={
    "name": "Memory",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
    "source_dir": str(source_dir),
  }, headers=_service_auth())
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]

  from app.routes import apps as apps_module
  supervised = (
    "15 4 * * * python3 /app/scripts/app-job-runner.py "
    f"{app_id} {source_dir}/fetch.sh"
  )
  with patch.object(apps_module, "_read_live_crontab", return_value=supervised):
    r = client.get("/api/apps/schedules", headers=auth)

  assert r.status_code == 200, r.text
  assert [(job["cron"], job["job"]) for job in r.json()] == [
    ("15 4 * * *", "fetch.sh")
  ]


def test_boot_reconciles_legacy_direct_cron_through_runner(client, db):
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  job = source_dir / "fetch.sh"
  job.write_text("#!/bin/sh\n", encoding="utf-8")
  app = models.App(
    name="Memory",
    slug="memory",
    description="test",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(source_dir),
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  from app.routes import apps as apps_module
  direct = f"15 4 * * * {source_dir}/fetch.sh {app.id}"
  with patch.object(apps_module, "_read_live_crontab", return_value=direct), \
       patch("app.install._register_cron") as register:
    count, warnings = apps_module.reconcile_app_cron_supervision(db)

  assert count == 1
  assert warnings == []
  register.assert_called_once_with(
    "memory", "15 4 * * *", job.resolve(), app.id,
  )


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

  # A URL carrying the app's exact updated_at is content-addressed from the
  # browser's perspective: any icon-changing update advances updated_at and
  # therefore produces a new URL. Keep this response indefinitely so reopening
  # the App Store never re-downloads unchanged icons.
  row = db.query(models.App).filter(models.App.id == app_id).first()
  versioned = client.get(
    f"/api/apps/{app_id}/icon",
    params={"size": 128, "v": row.updated_at.isoformat()},
  )
  assert versioned.status_code == 200
  assert versioned.headers["Cache-Control"] == "public, max-age=31536000, immutable"

  # A guessed/stale version must not earn immutable caching.
  stale_version = client.get(
    f"/api/apps/{app_id}/icon",
    params={"size": 128, "v": "stale"},
  )
  assert stale_version.status_code == 200
  assert stale_version.headers["Cache-Control"] == _CC


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
