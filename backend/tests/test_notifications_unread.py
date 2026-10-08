"""Explicit unread tracking for the notification panel and bell badge."""

from datetime import UTC, datetime, timedelta

import pytest

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


def _new_count(client, auth) -> int:
  res = client.get("/api/notifications/new-count", headers=auth)
  assert res.status_code == 200, res.text
  return res.json()["count"]


def test_opening_acknowledges_new_arrivals_without_reading_or_deleting(client, auth):
  first = _send(client, auth, title="First")
  second = _send(client, auth, title="Second")
  assert _new_count(client, auth) == 2
  assert _count(client, auth) == 2

  seen = client.post("/api/notifications/seen-all", headers=auth)
  assert seen.status_code == 200, seen.text
  assert seen.json() == {"updated": 2}
  assert _new_count(client, auth) == 0
  assert _count(client, auth) == 2
  rows = {row["id"]: row for row in client.get("/api/notifications", headers=auth).json()}
  assert {first, second}.issubset(rows)
  assert rows[first]["read_at"] is None
  assert rows[second]["read_at"] is None
  assert client.post("/api/notifications/seen-all", headers=auth).json() == {"updated": 0}

  third = _send(client, auth, title="Later")
  assert _new_count(client, auth) == 1
  assert _count(client, auth) == 3
  assert client.post(f"/api/notifications/{third}/read", headers=auth).json() == {"updated": 1}
  assert _new_count(client, auth) == 0
  assert _count(client, auth) == 2


def test_explicit_read_all_lifecycle(client, auth):
  """Listing leaves unread intact; read-all is explicit and idempotent."""
  assert _count(client, auth) == 0
  assert _new_count(client, auth) == 0

  sent_id = _send(client, auth)
  assert _count(client, auth) == 1
  assert _new_count(client, auth) == 1
  listed = client.get("/api/notifications", headers=auth).json()
  row = next(n for n in listed if n["id"] == sent_id)
  assert row["read_at"] is None
  assert _count(client, auth) == 1
  assert datetime.fromisoformat(row["sent_at"]).tzinfo == UTC

  first = client.post("/api/notifications/read-all", headers=auth)
  assert first.status_code == 200, first.text
  assert first.json() == {"updated": 1}
  assert _count(client, auth) == 0
  assert _new_count(client, auth) == 0
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


def test_mark_one_read_preserves_other_rows_and_recovery_actions(client, auth, db):
  first_id = _send(client, auth, title="First")
  second_id = _send(client, auth, title="Second")
  owner = db.query(models.Owner).first()
  receipt_id = "readable-recovery-receipt"
  actions = [{"action": "recover_chat", "resource_id": "chat-123"}]
  db.add(models.Notification(
    id=receipt_id, owner_id=owner.id, source_type="shell",
    title="Chat deleted", actions=actions,
  ))
  db.commit()
  assert _count(client, auth) == 3

  read = client.post(f"/api/notifications/{first_id}/read", headers=auth)
  assert read.status_code == 200, read.text
  assert read.json() == {"updated": 1}
  assert _count(client, auth) == 2
  assert client.post(
    f"/api/notifications/{first_id}/read", headers=auth,
  ).json() == {"updated": 0}

  receipt_read = client.post(
    f"/api/notifications/{receipt_id}/read", headers=auth,
  )
  assert receipt_read.status_code == 200, receipt_read.text
  assert receipt_read.json() == {"updated": 1}
  assert _count(client, auth) == 1
  rows = {row["id"]: row for row in client.get(
    "/api/notifications", headers=auth,
  ).json()}
  assert rows[first_id]["read_at"] is not None
  assert rows[second_id]["read_at"] is None
  assert rows[receipt_id]["actions"] == actions
  assert client.post(
    "/api/notifications/missing/read", headers=auth,
  ).status_code == 404


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


def test_history_continues_after_cursor_row_is_deleted(client, auth, db):
  owner = db.query(models.Owner).first()
  sent_at = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
  for notification_id in ("n-a", "n-b", "n-c"):
    db.add(models.Notification(
      id=notification_id, owner_id=owner.id, source_type="system",
      title=notification_id, sent_at=sent_at,
    ))
  db.commit()

  first = client.get("/api/notifications", headers=auth, params={"limit": 2})
  assert [row["id"] for row in first.json()] == ["n-c", "n-b"]
  cursor = first.json()[-1]
  db.delete(db.query(models.Notification).filter_by(id=cursor["id"]).one())
  db.commit()

  second = client.get("/api/notifications", headers=auth, params={
    "limit": 2, "before": cursor["id"], "before_at": cursor["sent_at"],
  })
  assert second.status_code == 200, second.text
  assert [row["id"] for row in second.json()] == ["n-a"]
  offset = client.get("/api/notifications", headers=auth, params={
    "limit": 2, "before": cursor["id"], "before_at": "2026-07-27T04:00:00+02:00",
  })
  assert [row["id"] for row in offset.json()] == ["n-a"]


def test_history_rejects_an_unknown_cursor(client, auth):
  response = client.get(
    "/api/notifications",
    headers=auth,
    params={"before": "not-a-notification"},
  )
  assert response.status_code == 400
  assert response.json()["detail"] == "Invalid notification cursor."

  for params in (
    {"before_at": "2026-07-27T02:00:00Z"},
    {"before": "n-a", "before_at": "2026-07-27T02:00:00"},
  ):
    invalid = client.get("/api/notifications", headers=auth, params=params)
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Invalid notification cursor."


def test_owner_can_dismiss_one_notification(client, auth):
  """Single-item dismissal removes just the selected row and unread badge entry."""
  keep_id = _send(client, auth, title="Keep me")
  dismiss_id = _send(client, auth, title="Dismiss me")
  assert _count(client, auth) == 2

  response = client.delete(
    f"/api/notifications/{dismiss_id}", headers=auth,
  )
  assert response.status_code == 200, response.text
  assert response.json() == {"deleted": 1}
  assert _count(client, auth) == 1
  listed = client.get("/api/notifications", headers=auth).json()
  assert [row["id"] for row in listed] == [keep_id]

  missing = client.delete(
    f"/api/notifications/{dismiss_id}", headers=auth,
  )
  assert missing.status_code == 404


@pytest.mark.parametrize(
  "actions",
  [
    [{"action": "recover_chat", "resource_id": "chat-123"}],
    [{"action": "recover_app", "resource_id": "app-123"}],
    [{"action": "recover_project", "resource_id": "project-123"}],
    [{"action": "recover_chat"}, {"action": "recover_app"}],
  ],
)
def test_single_item_dismissal_applies_to_every_recovery_receipt(client, auth, db, actions):
  """Every deletion receipt follows the same rule: an explicit × removes it."""
  owner = db.query(models.Owner).first()
  receipt_id = "recovery-receipt-dismissable"
  db.add(models.Notification(
    id=receipt_id,
    owner_id=owner.id,
    source_type="shell",
    title="Deleted",
    actions=actions,
  ))
  db.commit()

  response = client.delete(
    f"/api/notifications/{receipt_id}", headers=auth,
  )
  assert response.status_code == 200, response.text
  db.expire_all()
  assert db.query(models.Notification).filter_by(id=receipt_id).one_or_none() is None


def test_clear_preserves_active_undo_and_records_only_count(client, auth, db, monkeypatch):
  now = datetime.now(UTC)
  owner = db.query(models.Owner).first()
  events = []
  monkeypatch.setattr(
    "app.routes.notifications.activity.log_event",
    lambda event, **fields: events.append((event, fields)),
  )
  for suffix, expires, completed in (
    ("active", now + timedelta(days=1), None),
    ("expired", now - timedelta(days=1), None),
    ("restored", now + timedelta(days=1), now.isoformat()),
  ):
    db.add(models.Notification(
      id=f"undo-{suffix}", owner_id=owner.id, source_type="shell",
      title=f"Private {suffix}", actions=[{
        "action": "recover_chat", "resource_type": "chat", "resource_id": suffix,
        "expires_at": expires.isoformat(), "completed_at": completed,
      }],
    ))
  db.commit()
  ordinary_id = _send(client, auth, title="Private ordinary")

  cleared = client.delete("/api/notifications", headers=auth)
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"deleted": 3}
  assert {row["id"] for row in client.get("/api/notifications", headers=auth).json()} == {"undo-active"}
  assert _count(client, auth) == 1
  assert events == [("notification_history_cleared", {"deleted": 3})]
  assert ordinary_id not in {row.id for row in db.query(models.Notification).all()}

  again = client.delete("/api/notifications", headers=auth)
  assert again.json() == {"deleted": 0}
  assert {row["id"] for row in client.get("/api/notifications", headers=auth).json()} == {"undo-active"}
  assert events[-1] == ("notification_history_cleared", {"deleted": 0})


def test_clear_uses_dismiss_recovery_family_and_expires_unknown_receipts(client, auth, db):
  owner = db.query(models.Owner).first()
  now = datetime.now(UTC)
  for suffix, expires_at in (
    ("future", now + timedelta(days=1)),
    ("expired", now - timedelta(days=1)),
  ):
    db.add(models.Notification(
      id=f"future-recovery-{suffix}", owner_id=owner.id,
      source_type="shell", title="Recoverable",
      actions=[{"action": "recover_future_resource", "expires_at": expires_at.isoformat()}],
    ))
  db.add(models.Notification(
    id="future-recovery-malformed", owner_id=owner.id,
    source_type="shell", title="Recoverable",
    actions=[{"action": "recover_future_resource", "expires_at": "not-a-date"}],
  ))
  db.commit()

  cleared = client.delete("/api/notifications", headers=auth)
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"deleted": 1}
  assert {row.id for row in db.query(models.Notification).all()} == {
    "future-recovery-future", "future-recovery-malformed",
  }


def test_clear_rechecks_actions_at_delete_boundary(client, auth, db, monkeypatch):
  from app.database import SessionLocal
  from app.routes import notifications as route

  owner_id = db.query(models.Owner.id).first()[0]
  db.add(models.Notification(
    id="became-recoverable", owner_id=owner_id,
    source_type="shell", title="Ordinary",
    sent_at=datetime.now(UTC) - timedelta(minutes=1),
  ))
  db.commit()
  original = route._has_active_undo
  changed = False

  def add_recovery_after_selection(actions, now):
    nonlocal changed
    if not changed:
      changed = True
      with SessionLocal() as other:
        row = other.query(models.Notification).filter_by(id="became-recoverable").one()
        row.actions = [{
          "action": "recover_future_resource",
          "expires_at": (now + timedelta(days=1)).isoformat(),
        }]
        other.commit()
    return original(actions, now)

  monkeypatch.setattr(route, "_has_active_undo", add_recovery_after_selection)
  cleared = client.delete("/api/notifications", headers=auth)
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"deleted": 0}
  db.expire_all()
  saved = db.query(models.Notification).filter_by(id="became-recoverable").one()
  assert saved.actions[0]["action"] == "recover_future_resource"


def test_clear_scans_large_history_without_skipping_live_undo(client, auth, db):
  owner = db.query(models.Owner).first()
  db.add_all([
    models.Notification(
      id=f"bulk-{number:04d}", owner_id=owner.id,
      source_type="agent", title="Ordinary",
    )
    for number in range(505)
  ])
  db.add(models.Notification(
    id="bulk-0250-undo", owner_id=owner.id, source_type="shell",
    title="Undo", actions=[{
      "action": "recover_chat",
      "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }],
  ))
  db.commit()

  cleared = client.delete("/api/notifications", headers=auth)
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"deleted": 505}
  assert {row.id for row in db.query(models.Notification).all()} == {"bulk-0250-undo"}


def test_clear_does_not_delete_arrival_between_batches(client, auth, db, monkeypatch):
  from app.database import SessionLocal
  from app.routes import notifications as route

  owner_id = db.query(models.Owner.id).first()[0]
  db.add(models.Notification(
    id="a-old", owner_id=owner_id, source_type="agent", title="Old",
    sent_at=datetime.now(UTC) - timedelta(minutes=1),
  ))
  db.commit()

  original = route._has_active_undo
  inserted = False

  def insert_after_sweep_started(actions, started_at):
    nonlocal inserted
    if not inserted:
      inserted = True
      with SessionLocal() as other:
        other.add(models.Notification(
          id="z-new", owner_id=owner_id, source_type="agent", title="New",
          sent_at=started_at + timedelta(microseconds=1),
        ))
        other.commit()
    return original(actions, started_at)

  monkeypatch.setattr(route, "_has_active_undo", insert_after_sweep_started)
  cleared = client.delete("/api/notifications", headers=auth)
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"deleted": 1}
  assert {row.id for row in db.query(models.Notification).all()} == {"z-new"}


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
    activity = events.get_nowait()
    assert activity["type"] == "app_activity"
    assert activity["appId"] == str(app.id)
    assert isinstance(activity["unseenActivityVersion"], int)
    assert activity["appCreatedAt"] == app.created_at.isoformat()
    assert events.get_nowait() == {
      "type": "notification_created", "id": sent_id,
    }
  finally:
    bus.unsubscribe(events)


def test_unread_endpoints_are_owner_only(client, auth, db):
  """The bell is the owner's surface: no token → 401, app token → 403."""
  assert client.get("/api/notifications/unread-count").status_code == 401
  assert client.get("/api/notifications/new-count").status_code == 401
  assert client.post("/api/notifications/read-all").status_code == 401
  assert client.post("/api/notifications/seen-all").status_code == 401
  assert client.post("/api/notifications/n-1/read").status_code == 401

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
  assert client.get(
    "/api/notifications/new-count", headers=app_headers,
  ).status_code == 403
  assert client.post(
    "/api/notifications/read-all", headers=app_headers,
  ).status_code == 403
  assert client.post(
    "/api/notifications/seen-all", headers=app_headers,
  ).status_code == 403
  assert client.post(
    "/api/notifications/n-1/read", headers=app_headers,
  ).status_code == 403
