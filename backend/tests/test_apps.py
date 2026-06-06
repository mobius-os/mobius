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
  from datetime import datetime, timedelta
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
  row.deleted_at = datetime.utcnow() - timedelta(days=8)
  db.commit()
  client.get("/api/apps/", headers=auth)
  assert not source_dir.exists()


def test_app_token_can_update_own_schedule_only(client, auth, monkeypatch):
  calls = []

  def fake_register(slug, schedule_expr, job_path, bundled_job_bytes, app_id=None):
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
