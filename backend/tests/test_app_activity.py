"""App-attributed notifications drive a durable drawer activity marker."""

from app import models
from app.broadcast import get_system_broadcast


def _app(db):
  app = models.App(
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
    assert events.get_nowait() == {
      "type": "app_activity", "appId": str(app.id),
    }
  finally:
    system_bus.unsubscribe(events)

  listed = client.get("/api/apps/", headers=auth)
  assert listed.status_code == 200, listed.text
  row = next(item for item in listed.json() if item["id"] == app.id)
  assert row["has_unseen_activity"] is True
  observed_version = row["unseen_activity_version"]

  seen = client.post(
    f"/api/apps/{app.id}/activity/seen",
    headers=auth,
    json={"activity_version": observed_version},
  )
  assert seen.status_code == 204, seen.text
  row = next(
    item for item in client.get("/api/apps/", headers=auth).json()
    if item["id"] == app.id
  )
  assert row["has_unseen_activity"] is False


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
    json={"activity_version": observed_version},
  )
  assert stale.status_code == 204
  db.refresh(first)
  assert first.unseen is True

  current = client.post(
    f"/api/apps/{app.id}/activity/seen",
    headers=auth,
    json={"activity_version": newer_version},
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
      json={"activity_version": invalid_version},
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
