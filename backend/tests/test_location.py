"""Location-batch ingest, history, and per-phone tracking state."""
from __future__ import annotations

import pytest


def _pos(ts, lat=10.5, lon=20.25, **extra):
  base = {"latitude": lat, "longitude": lon, "timestamp": ts}
  base.update(extra)
  return base


def test_store_batch_persists_and_reports_counts(client, auth, db):
  from app import models

  r = client.post("/api/location/batch", headers=auth, json={
    "positions": [
      _pos(1_700_000_000_000, accuracy=12.0, altitude=120.3, speed=1.5, heading=270.0),
      _pos(1_700_000_010_000),
    ],
  })
  assert r.status_code == 200, r.text
  assert r.json()["accepted"] == 2
  stored = db.query(models.LocationPosition).all()
  assert len(stored) == 2
  first = min(stored, key=lambda p: p.device_timestamp_ms)
  assert first.latitude == 10.5 and first.longitude == 20.25
  assert first.accuracy == 12.0 and first.heading == 270.0
  assert first.device_id == ""
  assert first.received_at is not None


def test_batch_is_idempotent_on_owner_device_timestamp(client, auth, db):
  from app import models

  payload = {"positions": [_pos(1_700_000_000_000)], "device_id": "phone-a"}
  assert client.post("/api/location/batch", headers=auth, json=payload).status_code == 200
  # Re-deliver the identical batch; no duplicate row appears.
  assert client.post("/api/location/batch", headers=auth, json=payload).status_code == 200
  rows = db.query(models.LocationPosition).filter_by(device_id="phone-a").all()
  assert len(rows) == 1


def test_duplicate_timestamps_within_a_batch_collapse(client, auth, db):
  from app import models

  r = client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000, lat=1.0), _pos(1_700_000_000_000, lat=2.0)],
  })
  assert r.status_code == 200, r.text
  rows = db.query(models.LocationPosition).all()
  assert len(rows) == 1
  # Last fix for a timestamp wins.
  assert rows[0].latitude == 2.0


def test_different_devices_keep_same_timestamp(client, auth, db):
  from app import models

  ts = 1_700_000_000_000
  client.post("/api/location/batch", headers=auth, json={"positions": [_pos(ts)], "device_id": "a"})
  client.post("/api/location/batch", headers=auth, json={"positions": [_pos(ts)], "device_id": "b"})
  assert db.query(models.LocationPosition).count() == 2


def test_null_optional_fields_allowed(client, auth, db):
  from app import models

  r = client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000, accuracy=None, altitude=None, speed=None, heading=None)],
  })
  assert r.status_code == 200, r.text
  row = db.query(models.LocationPosition).one()
  assert row.accuracy is None and row.altitude is None


def test_out_of_range_coordinates_rejected(client, auth):
  assert client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000, lat=91.0)],
  }).status_code == 422
  assert client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000, lon=-181.0)],
  }).status_code == 422


def test_implausible_timestamp_rejected(client, auth):
  assert client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(123)],
  }).status_code == 422


def test_batch_requires_auth(client):
  assert client.post("/api/location/batch", json={
    "positions": [_pos(1_700_000_000_000)],
  }).status_code == 401


def _report(client, auth, device_id, enabled, reported_at, **options):
  return client.put("/api/location/tracking", headers=auth, json={
    "device_id": device_id, "enabled": enabled, "options": options,
    "reported_at": reported_at,
  })


def test_tracking_state_round_trips(client, auth):
  assert client.get("/api/location/tracking", headers=auth).json() == {
    "enabled": False, "options": {}, "devices": [],
  }
  r = _report(client, auth, "phone-a", True, 1_000, interval=10000)
  assert r.status_code == 200, r.text
  got = client.get("/api/location/tracking", headers=auth).json()
  assert got["enabled"] is True
  assert got["options"] == {"interval": 10000}
  assert got["devices"] == [
    {"device_id": "phone-a", "enabled": True, "options": {"interval": 10000}},
  ]


def test_one_phone_stopping_leaves_another_phone_tracking(client, auth):
  _report(client, auth, "phone-a", True, 1_000, interval=10000)
  _report(client, auth, "phone-b", False, 2_000)

  got = client.get("/api/location/tracking", headers=auth).json()
  assert got["enabled"] is True
  assert got["options"] == {"interval": 10000}
  assert {d["device_id"]: d["enabled"] for d in got["devices"]} == {
    "phone-a": True, "phone-b": False,
  }

  _report(client, auth, "phone-a", False, 3_000)
  assert client.get("/api/location/tracking", headers=auth).json()["enabled"] is False


def test_an_older_report_cannot_overwrite_a_newer_one(client, auth):
  _report(client, auth, "phone-a", True, 2_000, interval=60000)
  _report(client, auth, "phone-a", True, 1_000, interval=30000)

  got = client.get("/api/location/tracking", headers=auth).json()
  assert got["options"] == {"interval": 60000}


@pytest.mark.parametrize("existing", [False, True])
def test_overlapping_reports_cannot_replace_a_newer_stop(client, auth, db, existing):
  from concurrent.futures import ThreadPoolExecutor
  from threading import Event
  from sqlalchemy import event
  from app import models
  from app.database import engine

  if existing:
    assert _report(client, auth, "phone-a", True, 1_000).status_code == 200
  paused = Event()
  release = Event()

  def delay_first_write(_conn, _cursor, statement, _parameters, _context, _many):
    sql = statement.lstrip().lower()
    if (sql.startswith("insert into location_tracking_devices")
        or sql.startswith("update location_tracking_devices")) and not paused.is_set():
      # Delay the older request's write until the newer request has committed.
      paused.set()
      assert release.wait(10), "The older report was not released"

  event.listen(engine, "before_cursor_execute", delay_first_write)
  try:
    with ThreadPoolExecutor(max_workers=1) as worker:
      older = worker.submit(_report, client, auth, "phone-a", True, 2_000, interval=30000)
      try:
        assert paused.wait(10), "The older report never reached its write"
        newer = _report(client, auth, "phone-a", False, 3_000)
        assert newer.status_code == 200, newer.text
      finally:
        release.set()
      result = older.result(timeout=10)
      assert result.status_code == 200, result.text
  finally:
    event.remove(engine, "before_cursor_execute", delay_first_write)

  got = client.get("/api/location/tracking", headers=auth).json()
  assert got == {
    "enabled": False, "options": {},
    "devices": [{"device_id": "phone-a", "enabled": False, "options": {}}],
  }
  stored = db.query(models.LocationTrackingDevice).one()
  assert stored.reported_at_ms == 3_000


def test_an_equal_timestamp_report_can_replace_the_previous_state(client, auth):
  assert _report(client, auth, "phone-a", True, 2_000, interval=60000).status_code == 200
  assert _report(client, auth, "phone-a", False, 2_000).status_code == 200
  got = client.get("/api/location/tracking", headers=auth).json()
  assert got["enabled"] is False
  assert got["devices"] == [{"device_id": "phone-a", "enabled": False, "options": {}}]


def test_tracking_report_requires_a_device(client, auth):
  assert client.put("/api/location/tracking", headers=auth, json={
    "enabled": True, "options": {}, "reported_at": 1,
  }).status_code == 422


def _app_headers(client, auth, *, history_read: bool):
  from app import models
  from app.database import SessionLocal
  from test_app_fixtures import create_local_app

  app_id = create_local_app(client, auth, name="location-test", description="t")["id"]
  with SessionLocal() as session:
    app = session.query(models.App).filter(models.App.id == app_id).one()
    app.capability_contract = {"schema": 1, "data": {"location_history_read": history_read}}
    session.commit()
  minted = client.post("/api/auth/app-token", json={"app_id": app_id}, headers=auth)
  assert minted.status_code == 200, minted.text
  return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_positions_are_returned_newest_first(client, auth):
  client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000), _pos(1_700_000_020_000), _pos(1_700_000_010_000)],
  })
  body = client.get("/api/location/positions?limit=2", headers=auth).json()
  assert [p["timestamp"] for p in body["positions"]] == [1_700_000_020_000, 1_700_000_010_000]
  assert body["count"] == 2


def test_only_apps_granted_history_read_can_see_location(client, auth):
  client.post("/api/location/batch", headers=auth, json={"positions": [_pos(1_700_000_000_000)]})

  denied = _app_headers(client, auth, history_read=False)
  assert client.get("/api/location/positions", headers=denied).status_code == 403
  assert client.get("/api/location/tracking", headers=denied).status_code == 403

  granted = _app_headers(client, auth, history_read=True)
  assert client.get("/api/location/positions", headers=granted).json()["count"] == 1
  assert client.get("/api/location/tracking", headers=granted).status_code == 200


def test_apps_cannot_write_location_even_with_history_read(client, auth):
  granted = _app_headers(client, auth, history_read=True)
  assert client.post("/api/location/batch", headers=granted, json={
    "positions": [_pos(1_700_000_000_000)],
  }).status_code in (401, 403)
  assert _report(client, granted, "phone-a", True, 1_000).status_code in (401, 403)


def test_location_tracking_capability_is_reviewable_in_a_manifest():
  from app.app_capabilities import normalize_runtime_capabilities

  declared = normalize_runtime_capabilities({
    "capabilities": {"location.tracking": {"version": 1, "reason": "Record walks."}},
  })["location.tracking"]
  assert declared["kind"] == "invoke" and declared["lifecycle"] == "active_frame"
  assert declared["reason"] == "Record walks."


def test_owner_can_delete_history_before_a_time_or_for_one_phone(client, auth, db):
  from app import models

  client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000), _pos(1_700_000_010_000)], "device_id": "a",
  })
  client.post("/api/location/batch", headers=auth, json={
    "positions": [_pos(1_700_000_000_000)], "device_id": "b",
  })

  r = client.delete("/api/location/positions?before=1700000010000&device_id=a", headers=auth)
  assert r.status_code == 200, r.text
  assert r.json() == {"deleted": 1}
  left = {(p.device_id, p.device_timestamp_ms) for p in db.query(models.LocationPosition)}
  assert left == {("a", 1_700_000_010_000), ("b", 1_700_000_000_000)}

  assert client.delete("/api/location/positions", headers=auth).json() == {"deleted": 2}
  assert db.query(models.LocationPosition).count() == 0


def test_apps_cannot_delete_history_even_with_history_read(client, auth, db):
  from app import models

  client.post("/api/location/batch", headers=auth, json={"positions": [_pos(1_700_000_000_000)]})
  granted = _app_headers(client, auth, history_read=True)
  assert client.delete("/api/location/positions", headers=granted).status_code in (401, 403)
  assert db.query(models.LocationPosition).count() == 1
