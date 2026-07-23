"""Owning-chat app CTAs acknowledge exact preview builds durably."""

from datetime import timedelta

from app import models


def _app(db):
  app = models.App(
    name="Atlas", description="", chat_id="chat-a",
    jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _listed(client, auth, app_id):
  response = client.get("/api/apps/", headers=auth)
  assert response.status_code == 200, response.text
  return next(row for row in response.json() if row["id"] == app_id)


def test_preview_then_final_acknowledgement_is_durable(client, auth, db):
  app = _app(db)
  row = _listed(client, auth, app.id)
  assert row["preview_seen_updated_at"] is None
  assert row["preview_seen_final"] is False

  preview = client.post(
    f"/api/apps/{app.id}/preview/seen",
    headers=auth,
    json={"updated_at": row["updated_at"], "final": False},
  )
  assert preview.status_code == 204, preview.text
  row = _listed(client, auth, app.id)
  assert row["preview_seen_updated_at"] == row["updated_at"]
  assert row["preview_seen_final"] is False

  final = client.post(
    f"/api/apps/{app.id}/preview/seen",
    headers=auth,
    json={"updated_at": row["updated_at"], "final": True},
  )
  assert final.status_code == 204, final.text
  row = _listed(client, auth, app.id)
  assert row["preview_seen_updated_at"] == row["updated_at"]
  assert row["preview_seen_final"] is True


def test_stale_open_never_hides_a_newer_build(client, auth, db):
  app = _app(db)
  old_version = app.updated_at

  app.updated_at = old_version + timedelta(seconds=1)
  db.commit()
  db.refresh(app)
  new_version = app.updated_at

  current = client.post(
    f"/api/apps/{app.id}/preview/seen",
    headers=auth,
    json={"updated_at": new_version.isoformat(), "final": False},
  )
  assert current.status_code == 204, current.text

  # A delayed click from another pane/device cannot move the acknowledgement
  # back or incorrectly promote the current build to its final phase.
  stale = client.post(
    f"/api/apps/{app.id}/preview/seen",
    headers=auth,
    json={"updated_at": old_version.isoformat(), "final": True},
  )
  assert stale.status_code == 204, stale.text
  row = _listed(client, auth, app.id)
  assert row["preview_seen_updated_at"] == row["updated_at"]
  assert row["preview_seen_final"] is False


def test_future_preview_version_is_rejected(client, auth, db):
  app = _app(db)
  future = app.updated_at + timedelta(days=1)
  response = client.post(
    f"/api/apps/{app.id}/preview/seen",
    headers=auth,
    json={"updated_at": future.isoformat(), "final": True},
  )
  assert response.status_code == 409, response.text
  assert db.get(models.AppPreviewState, app.id) is None
