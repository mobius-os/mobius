"""Unread tracking for the notifications page (bell badge, seen-on-open)."""

from datetime import UTC, datetime

from app import models
from app.auth import create_app_token
from app.broadcast import get_system_broadcast


def _send(client, auth, **overrides):
  payload = {"title": "Ping", "body": "hello", **overrides}
  res = client.post("/api/notifications/send", headers=auth, json=payload)
  assert res.status_code == 200, res.text
  return res.json()["id"]


def _count(client, auth) -> int:
  res = client.get("/api/notifications/unread-count", headers=auth)
  assert res.status_code == 200, res.text
  return res.json()["count"]


def test_seen_on_open_lifecycle(client, auth):
  """Send → unread; read-all → seen (idempotent); new send → unread again."""
  assert _count(client, auth) == 0

  sent_id = _send(client, auth)
  assert _count(client, auth) == 1
  listed = client.get("/api/notifications", headers=auth).json()
  row = next(n for n in listed if n["id"] == sent_id)
  assert row["read_at"] is None
  assert datetime.fromisoformat(row["sent_at"]).tzinfo == UTC

  first = client.post("/api/notifications/read-all", headers=auth)
  assert first.status_code == 200, first.text
  assert first.json() == {"updated": 1}
  assert _count(client, auth) == 0
  listed = client.get("/api/notifications", headers=auth).json()
  row = next(n for n in listed if n["id"] == sent_id)
  assert row["read_at"] is not None
  assert datetime.fromisoformat(row["read_at"]).tzinfo == UTC

  # Idempotent: a repeat call touches nothing and stamps nothing anew.
  second = client.post("/api/notifications/read-all", headers=auth)
  assert second.status_code == 200
  assert second.json() == {"updated": 0}

  # A notification arriving after read-all counts as unread again.
  _send(client, auth, title="Later")
  assert _count(client, auth) == 1


def test_history_cursor_is_stable_when_timestamps_tie(client, auth, db):
  """Keyset pagination must neither skip nor repeat same-instant rows."""
  owner = db.query(models.Owner).first()
  sent_at = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
  for notification_id in ("n-a", "n-b", "n-c"):
    db.add(models.Notification(
      id=notification_id,
      owner_id=owner.id,
      source_type="system",
      title=notification_id,
      sent_at=sent_at,
    ))
  db.commit()

  first = client.get(
    "/api/notifications", headers=auth, params={"limit": 2},
  )
  assert first.status_code == 200, first.text
  first_ids = [row["id"] for row in first.json()]
  assert first_ids == ["n-c", "n-b"]

  second = client.get(
    "/api/notifications",
    headers=auth,
    params={"limit": 2, "before": first_ids[-1]},
  )
  assert second.status_code == 200, second.text
  assert [row["id"] for row in second.json()] == ["n-a"]


def test_history_rejects_an_unknown_cursor(client, auth):
  response = client.get(
    "/api/notifications",
    headers=auth,
    params={"before": "not-a-notification"},
  )
  assert response.status_code == 400
  assert response.json()["detail"] == "Invalid notification cursor."


def test_notification_created_published_on_system_bus(client, auth):
  """Every notify_owner call nudges the bell badge over the system stream."""
  bus = get_system_broadcast()
  events = bus.subscribe()
  try:
    sent_id = _send(client, auth)
    assert events.get_nowait() == {
      "type": "notification_created", "id": sent_id,
    }
  finally:
    bus.unsubscribe(events)


def test_app_attributed_send_publishes_activity_then_badge(client, auth, db):
  """App-sourced sends keep the drawer-dot event AND gain the badge nudge."""
  app = models.App(
    slug="test-notifications-unread-108",
    source_dir="/tmp/mobius-tests/test-notifications-unread-108",
    name="News", description="",
    jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  bus = get_system_broadcast()
  events = bus.subscribe()
  try:
    sent_id = _send(
      client, auth, source_type="app", source_id=str(app.id),
    )
    assert events.get_nowait() == {
      "type": "app_activity", "appId": str(app.id),
    }
    assert events.get_nowait() == {
      "type": "notification_created", "id": sent_id,
    }
  finally:
    bus.unsubscribe(events)


def test_unread_endpoints_are_owner_only(client, auth, db):
  """The bell is the owner's surface: no token → 401, app token → 403."""
  assert client.get("/api/notifications/unread-count").status_code == 401
  assert client.post("/api/notifications/read-all").status_code == 401

  app = models.App(
    slug="test-notifications-unread-138",
    source_dir="/tmp/mobius-tests/test-notifications-unread-138",
    name="Probe", description="",
    jsx_source="export default function App(){}",
    compiled_path="/tmp/probe.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  owner = db.query(models.Owner).first()
  app_headers = {
    "Authorization": "Bearer " + create_app_token(
      app.id, owner.username, owner.token_epoch, app.token_nonce,
    ),
  }
  assert client.get(
    "/api/notifications/unread-count", headers=app_headers,
  ).status_code == 403
  assert client.post(
    "/api/notifications/read-all", headers=app_headers,
  ).status_code == 403
