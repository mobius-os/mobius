"""Apps report their own unread count for the sidebar pill."""

import asyncio
import secrets

import pytest
from fastapi import HTTPException

from app import fs_locks, models
from app.auth import create_app_token
from app.broadcast import get_system_broadcast
from app.database import SessionLocal
from app.deps import Principal


@pytest.fixture(autouse=True)
def _fresh_app_locks(monkeypatch):
  # Each async test runs on its own event loop and SQLite reuses freed app ids,
  # so a lock kept alive by an earlier test's traceback must not be shared.
  from weakref import WeakValueDictionary
  monkeypatch.setattr(fs_locks, "_app_locks", WeakValueDictionary())


def _app(db, slug):
  app = models.App(
    slug=slug, source_dir=f"/tmp/mobius-tests/{slug}",
    name=slug, description="", jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _service_auth(db, app, service="private"):
  owner = db.query(models.Owner).first()
  token = create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
    service=service,
  )
  return {"Authorization": f"Bearer {token}"}


def _badge(client, auth, app_id):
  row = next(a for a in client.get("/api/apps/", headers=auth).json()
             if a["id"] == app_id)
  return row["badge_count"]


def _drain(events):
  seen = []
  while not events.empty():
    seen.append(events.get_nowait())
  return seen


def test_app_sets_and_clears_its_badge_and_nudges_only_on_change(
  client, auth, db,
):
  app = _app(db, "badge-social")
  service = _service_auth(db, app)
  assert _badge(client, auth, app.id) == 0
  nudge = {"type": "app_activity", "appId": str(app.id)}

  bus = get_system_broadcast()
  events = bus.subscribe()
  try:
    reply = client.put(f"/api/apps/{app.id}/badge", headers=service,
                       json={"count": 5})
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"count": 5, "revision": None, "applied": True}
    assert _drain(events) == [nudge]
    # Re-reporting the same count is a no-op for live shells.
    assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                      json={"count": 5}).status_code == 200
    assert _drain(events) == []
  finally:
    bus.unsubscribe(events)

  assert _badge(client, auth, app.id) == 5
  assert client.get(f"/api/apps/{app.id}", headers=auth).json()["badge_count"] == 5
  assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                    json={"count": 0}).status_code == 200
  assert _badge(client, auth, app.id) == 0


def test_app_cannot_badge_a_sibling_and_values_stay_in_integer_range(
  client, auth, db,
):
  mine = _app(db, "badge-mine")
  other = _app(db, "badge-other")
  service = _service_auth(db, mine)
  assert client.put(f"/api/apps/{other.id}/badge", headers=service,
                    json={"count": 1}).status_code == 403
  for body in ({"count": -1}, {"count": 2**63}, {"count": 1, "revision": -1}):
    assert client.put(f"/api/apps/{mine.id}/badge", headers=auth,
                      json=body).status_code == 422
  assert _badge(client, auth, other.id) == 0


def test_public_service_invocation_can_badge_only_its_own_app(client, auth, db):
  # Unread items typically arrive as a public request (a peer delivering a
  # message), so the service answering it must be able to report the count.
  mine = _app(db, "badge-public")
  other = _app(db, "badge-public-other")
  public = _service_auth(db, mine, service="public")
  assert client.put(f"/api/apps/{mine.id}/badge", headers=public,
                    json={"count": 1}).status_code == 200
  assert _badge(client, auth, mine.id) == 1
  assert client.put(f"/api/apps/{other.id}/badge", headers=public,
                    json={"count": 1}).status_code == 403


def test_app_token_without_an_installation_binding_fails_closed(
  client, auth, db,
):
  # Without its installation nonce the fence would compare the row to itself.
  app = _app(db, "badge-unbound")
  owner = db.query(models.Owner).first()
  token = create_app_token(app.id, owner.username, owner.token_epoch)
  reply = client.put(f"/api/apps/{app.id}/badge",
                     headers={"Authorization": f"Bearer {token}"},
                     json={"count": 3})
  assert reply.status_code == 401, reply.text
  assert _badge(client, auth, app.id) == 0


def test_stale_revisions_are_ignored_and_unrevisioned_reports_reset(
  client, auth, db,
):
  app = _app(db, "badge-order")
  service = _service_auth(db, app)

  def put(body, headers=service):
    reply = client.put(f"/api/apps/{app.id}/badge", headers=headers, json=body)
    assert reply.status_code == 200, reply.text
    return reply.json()

  # A delivery counted 2 at revision 10; a read receipt counted 0 at 11 and
  # landed first. The late, older report must not resurrect the count.
  assert put({"count": 0, "revision": 11})["applied"] is True
  assert put({"count": 2, "revision": 10}) == {
    "count": 0, "revision": 11, "applied": False,
  }
  # An equal revision is the same state, not a newer one.
  assert put({"count": 5, "revision": 11})["applied"] is False
  assert put({"count": 3, "revision": 12})["applied"] is True
  # Clearing keeps the ordering, so a stale report cannot reappear after it.
  assert put({"count": 0, "revision": 13})["applied"] is True
  assert put({"count": 3, "revision": 12})["applied"] is False
  assert _badge(client, auth, app.id) == 0
  # An unrevisioned report always applies and resets the ordering: the
  # recovery for an app whose revisions restarted lower (a data restore).
  assert put({"count": 4}) == {"count": 4, "revision": None, "applied": True}
  assert put({"count": 1, "revision": 1})["applied"] is True
  assert _badge(client, auth, app.id) == 1


def test_wiping_app_data_forgets_its_badge(client, auth, db):
  app = _app(db, "badge-wipe")
  service = _service_auth(db, app)
  assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                    json={"count": 4, "revision": 500}).status_code == 200
  assert client.delete(f"/api/apps/{app.id}/data", headers=auth).status_code in (200, 204)
  assert _badge(client, auth, app.id) == 0
  # The fresh installation's first report applies at its restarted revision.
  db.refresh(app)
  service = _service_auth(db, app)
  assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                    json={"count": 2, "revision": 3}).json()["applied"] is True
  assert _badge(client, auth, app.id) == 2


async def _report_while_lock_held(app_id, principal, count, revision, between):
  """Start a badge report, let it wait at the app's lifecycle lock, run
  ``between`` (a concurrent writer committing in its own session), then let
  the report proceed. Returns the route's answer or raises its error."""
  from app.routes.apps import AppBadgeRequest, set_app_badge

  session = SessionLocal()
  try:
    lock = fs_locks.app_storage_lock(app_id)
    await lock.acquire()
    try:
      task = asyncio.create_task(set_app_badge(
        app_id, AppBadgeRequest(count=count, revision=revision),
        db=session, principal=principal,
      ))
      await asyncio.sleep(0)  # the report is now authorized and waiting
      other = SessionLocal()
      try:
        between(other)
        other.commit()
      finally:
        other.close()
    finally:
      lock.release()
    return await task
  finally:
    session.close()


def _app_principal(db, app):
  owner = db.query(models.Owner).first()
  return Principal(owner=owner, app_id=app.id, app_instance_id=app.token_nonce,
                   scope="app", app_is_service=True)


@pytest.mark.asyncio
async def test_change_detection_sees_a_write_that_landed_while_waiting(client, db):
  from app import app_badge
  app = _app(db, "badge-race")
  bus = get_system_broadcast()
  events = bus.subscribe()
  try:
    # The shell has observed 5 from a concurrent writer that committed while
    # this report (count 0, newer revision) waited. It must still nudge, or an
    # open sidebar keeps showing 5 while the database holds 0.
    answer = await _report_while_lock_held(
      app.id, _app_principal(db, app), 0, 3,
      lambda other: app_badge.apply_report(other, app.id, 5, 2),
    )
    assert answer == {"count": 0, "revision": 3, "applied": True}
    assert {"type": "app_activity", "appId": str(app.id)} in _drain(events)
  finally:
    bus.unsubscribe(events)


@pytest.mark.asyncio
async def test_report_authorized_before_a_data_wipe_cannot_write_after_it(
  client, db,
):
  app = _app(db, "badge-fence-wipe")
  principal = _app_principal(db, app)

  def wipe(other):
    row = other.get(models.App, app.id)
    row.token_nonce = secrets.token_hex(16)  # what delete_app_data rotates

  with pytest.raises(HTTPException) as refused:
    await _report_while_lock_held(app.id, principal, 4, None, wipe)
  assert refused.value.status_code == 404
  assert db.get(models.AppBadgeState, app.id) is None


@pytest.mark.asyncio
async def test_report_cannot_badge_a_replacement_that_reused_the_app_id(
  client, db,
):
  app = _app(db, "badge-fence-reuse")
  app_id = app.id
  principal = _app_principal(db, app)

  def replace(other):
    other.delete(other.get(models.App, app_id))
    other.flush()
    other.add(models.App(
      id=app_id, slug="badge-fence-replacement",
      source_dir="/tmp/mobius-tests/badge-fence-replacement",
      name="replacement", description="",
      jsx_source="export default function App(){}", compiled_path="/tmp/app.js",
    ))

  with pytest.raises(HTTPException) as refused:
    await _report_while_lock_held(app_id, principal, 4, None, replace)
  assert refused.value.status_code == 404
  db.expire_all()
  assert db.get(models.AppBadgeState, app_id) is None
