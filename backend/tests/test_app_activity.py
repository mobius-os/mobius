"""App-attributed notifications drive a durable drawer activity marker."""

from datetime import timedelta

from app import app_activity, models
from app.broadcast import get_system_broadcast


def _app(db):
  app = models.App(
    slug="test-app-activity-8",
    source_dir="/tmp/mobius-tests/test-app-activity-8",
    name="News", description="", jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def test_app_notification_marks_list_unseen_and_open_acknowledges(
  client, auth, db,
):
  app = _app(db)
  system_bus = get_system_broadcast()
  events = system_bus.subscribe()

  try:
    sent = client.post("/api/notifications/send", headers=auth, json={
      "title": "News digest ready",
      "source_type": "app",
      "source_id": str(app.id),
      "target": f"/shell/?app={app.id}",
    })
    assert sent.status_code == 200, sent.text
    event = events.get_nowait()
  finally:
    system_bus.unsubscribe(events)

  listed = client.get("/api/apps/", headers=auth)
  assert listed.status_code == 200, listed.text
  row = next(item for item in listed.json() if item["id"] == app.id)
  assert row["has_unseen_activity"] is True
  observed_version = row["unseen_activity_version"]
  # The live event carries exactly what the list row reports, so a shell can
  # mark this one app without re-downloading the whole app list.
  assert event == {
    "type": "app_activity", "appId": str(app.id),
    "unseenActivityVersion": observed_version,
    "appCreatedAt": row["created_at"],
  }

  seen = client.post(
    f"/api/apps/{app.id}/activity/seen",
    headers=auth,
    json={"activity_version": observed_version, "app_created_at": row["created_at"]},
  )
  assert seen.status_code == 204, seen.text
  row = next(
    item for item in client.get("/api/apps/", headers=auth).json()
    if item["id"] == app.id
  )
  assert row["has_unseen_activity"] is False


def test_stale_or_missing_app_lifetime_cannot_clear_activity(client, auth, db):
  app = _app(db)
  assert client.post("/api/notifications/send", headers=auth, json={
    "title": "Background work finished", "source_type": "app", "source_id": str(app.id),
  }).status_code == 200
  state = db.get(models.AppActivityState, app.id)
  db.refresh(state)
  response = client.post(f"/api/apps/{app.id}/activity/seen", headers=auth, json={
    "activity_version": state.activity_version,
    "app_created_at": "earlier-app-lifetime",
  })
  assert response.status_code == 409
  missing = client.post(f"/api/apps/{app.id}/activity/seen", headers=auth, json={
    "activity_version": state.activity_version,
  })
  assert missing.status_code == 422
  db.refresh(state)
  assert state.unseen is True


def test_seen_write_rechecks_lifetime_after_route_read(client, auth, db):
  app = _app(db)
  assert client.post("/api/notifications/send", headers=auth, json={
    "title": "Background work finished", "source_type": "app", "source_id": str(app.id),
  }).status_code == 200
  state = db.get(models.AppActivityState, app.id)
  db.refresh(state)
  original_created_at = app.created_at
  # Simulate the app identity changing after the route read but before its
  # acknowledgement UPDATE. The SQL write, not just the route, must reject it.
  app.created_at = original_created_at + timedelta(seconds=1)
  db.commit()
  assert app_activity.mark_seen(db, app.id, state.activity_version, original_created_at) is None
  db.refresh(state)
  assert state.unseen is True


def test_seen_receipt_reports_only_the_version_actually_cleared(client, auth, db):
  app = _app(db)
  assert client.post("/api/notifications/send", headers=auth, json={
    "title": "Background work finished", "source_type": "app", "source_id": str(app.id),
  }).status_code == 200
  state = db.get(models.AppActivityState, app.id)
  db.refresh(state)
  version = state.activity_version
  bus = get_system_broadcast()
  events = bus.subscribe()
  try:
    seen = client.post(f"/api/apps/{app.id}/activity/seen", headers=auth, json={
      "activity_version": version + 100,
      "app_created_at": app.created_at.isoformat(),
    })
    assert seen.status_code == 204
    assert events.get_nowait() == {
      "type": "app_activity_seen", "appId": str(app.id),
      "appCreatedAt": app.created_at.isoformat(), "seenThroughVersion": version,
    }
    assert client.post(f"/api/apps/{app.id}/activity/seen", headers=auth, json={
      "activity_version": version + 100,
      "app_created_at": app.created_at.isoformat(),
    }).status_code == 204
    assert events.empty(), "a duplicate acknowledgement is not new evidence"
  finally:
    bus.unsubscribe(events)


def test_late_seen_request_does_not_erase_newer_app_activity(client, auth, db):
  app = _app(db)
  payload = {
    "title": "Background work finished",
    "source_type": "app",
    "source_id": str(app.id),
  }
  assert client.post("/api/notifications/send", headers=auth, json=payload).status_code == 200
  first = db.get(models.AppActivityState, app.id)
  db.refresh(first)
  observed_version = first.activity_version

  # A second completion lands after the shell fetched the first marker but
  # before its acknowledgement reaches the server.
  assert client.post("/api/notifications/send", headers=auth, json=payload).status_code == 200
  db.refresh(first)
  newer_version = first.activity_version
  assert newer_version == observed_version + 1

  stale = client.post(
    f"/api/apps/{app.id}/activity/seen",
    headers=auth,
    json={"activity_version": observed_version, "app_created_at": app.created_at.isoformat()},
  )
  assert stale.status_code == 204
  db.refresh(first)
  assert first.unseen is True

  current = client.post(
    f"/api/apps/{app.id}/activity/seen",
    headers=auth,
    json={"activity_version": newer_version, "app_created_at": app.created_at.isoformat()},
  )
  assert current.status_code == 204
  db.refresh(first)
  assert first.unseen is False


def test_seen_rejects_versions_outside_sqlite_integer_range(client, auth, db):
  app = _app(db)
  sent = client.post("/api/notifications/send", headers=auth, json={
    "title": "Background work finished",
    "source_type": "app",
    "source_id": str(app.id),
  })
  assert sent.status_code == 200, sent.text

  for invalid_version in (0, -1, 1 << 63, 10**80):
    response = client.post(
      f"/api/apps/{app.id}/activity/seen",
      headers=auth,
      json={"activity_version": invalid_version, "app_created_at": app.created_at.isoformat()},
    )
    assert response.status_code == 422, response.text

  state = db.get(models.AppActivityState, app.id)
  db.refresh(state)
  assert state.unseen is True


def test_non_app_and_unknown_app_notifications_do_not_create_markers(
  client, auth, db,
):
  app = _app(db)
  for source_type, source_id in (
    ("system", None),
    ("app", "999"),
    ("app", "0"),
    ("app", "999999999999999999999999999999999999"),
  ):
    payload = {"title": "Background work finished", "source_type": source_type}
    if source_id is not None:
      payload["source_id"] = source_id
    sent = client.post("/api/notifications/send", headers=auth, json=payload)
    assert sent.status_code == 200, sent.text

  row = next(
    item for item in client.get("/api/apps/", headers=auth).json()
    if item["id"] == app.id
  )
  assert row["has_unseen_activity"] is False
  assert db.query(models.AppActivityState).count() == 0
  assert db.query(models.Notification).count() == 4
