"""Native push devices: registration with the relay, delivery and cleanup."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from app import models, push_relay
from app import push as app_push
from app.push import notify_owner

# The real hand-off to the native-push workers; the relay fixture swaps it for
# an inline call so most tests can assert right after notify_owner returns.
REAL_SEND_NATIVE_LATER = app_push._send_native_later

FCM = "fcm:" + "a" * 140


class FakeRelay:
  def __init__(self, monkeypatch):
    self.binds, self.unbinds, self.sends = [], [], []
    self.error = None
    self.statuses = None

    def bind_device(token):
      self.binds.append(token)
      if self.error is not None:
        raise self.error

    def unbind_device(token):
      self.unbinds.append(token)
      if self.error is not None:
        raise self.error

    def send(tokens, **message):
      self.sends.append((list(tokens), message))
      if self.error is not None:
        raise self.error
      return self.statuses or ["ok"] * len(tokens)

    monkeypatch.setattr(push_relay, "bind_device", bind_device)
    monkeypatch.setattr(push_relay, "unbind_device", unbind_device)
    monkeypatch.setattr(push_relay, "send", send)


@pytest.fixture
def relay(monkeypatch):
  monkeypatch.setattr(app_push, "_send_native_later", app_push._deliver_native_and_clean)
  return FakeRelay(monkeypatch)


def _put(client, auth, **body):
  return client.put("/api/push/devices", headers=auth, json=body)


def _tokens(db):
  db.expire_all()
  return sorted(row.token for row in db.query(models.PushDevice))


def _owner_id(db):
  return db.query(models.Owner).filter_by(username="test").one().id


def test_registering_binds_at_the_relay_then_stores_one_device(client, auth, db, relay):
  assert _put(client, auth, token=FCM).json() == {"status": "registered"}
  assert _put(client, auth, token=FCM).status_code == 200

  assert relay.binds == [FCM, FCM]
  assert _tokens(db) == [FCM]


def test_platform_and_apns_fields_from_the_app_are_ignored(client, auth, db, relay):
  response = _put(
    client, auth, token=FCM, platform="ios", apnsToken="ab" * 32, apnsEnvironment="sandbox",
  )

  assert response.status_code == 200
  assert relay.binds == [FCM] and _tokens(db) == [FCM]


@pytest.mark.parametrize(("status", "code"), [(401, 409), (503, 502)])
def test_a_refused_bind_stores_nothing_and_explains_why(client, auth, db, relay, status, code):
  relay.error = push_relay.RelayError(f"http_{status}", status=status)

  response = _put(client, auth, token=FCM)

  assert response.status_code == code
  if code == 409:
    assert "mobius.you account" in response.json()["detail"]
  assert _tokens(db) == []


def test_a_token_registered_under_another_owner_moves_to_the_caller(client, auth, db, relay):
  other = models.Owner(username="previous-owner", hashed_password="x")
  db.add(other)
  db.flush()
  db.add(models.PushDevice(id="dev-other", owner_id=other.id, token=FCM))
  db.commit()

  _put(client, auth, token=FCM)

  db.expire_all()
  assert db.get(models.PushDevice, "dev-other").owner_id == _owner_id(db)


@pytest.mark.parametrize("body", [
  {"token": "has space"},
  {"token": ""},
  {"token": "x" * 4097},
  {},
])
def test_malformed_registrations_are_refused_before_the_relay(client, auth, db, relay, body):
  assert _put(client, auth, **body).status_code == 422
  assert relay.binds == [] and _tokens(db) == []


def test_sign_out_removes_the_device_unbinds_it_and_is_idempotent(client, auth, db, relay):
  _put(client, auth, token=FCM)

  for _ in range(2):
    response = client.request("DELETE", "/api/push/devices", headers=auth, json={"token": FCM})
    assert response.status_code == 204
  assert _tokens(db) == [] and relay.unbinds == [FCM]


def test_sign_out_still_removes_the_device_when_the_relay_is_down(client, auth, db, relay):
  _put(client, auth, token=FCM)
  relay.error = push_relay.RelayError("unreachable:ConnectError")

  response = client.request("DELETE", "/api/push/devices", headers=auth, json={"token": FCM})

  assert response.status_code == 204 and _tokens(db) == []


def test_an_owner_notification_reaches_every_phone_with_a_tap_url(client, auth, db, relay):
  _put(client, auth, token=FCM)
  _put(client, auth, token=FCM + "b")

  notification_id = notify_owner(
    db, _owner_id(db), title="Build finished", body="Open it when ready.",
    source_type="agent", target="/shell/?chat=chat-1", tag="done",
  )

  [(tokens, message)] = relay.sends
  assert sorted(tokens) == [FCM, FCM + "b"]
  assert message["title"] == "Build finished"
  assert message["data"]["notificationId"] == notification_id
  assert message["data"]["url"].startswith("http")
  assert message["data"]["url"].endswith("/shell/?chat=chat-1")
  assert message["collapse_key"] == "agent::done"


def test_an_invalid_token_is_deleted_and_others_are_kept(client, auth, db, relay):
  _put(client, auth, token=FCM)
  _put(client, auth, token=FCM + "b")
  relay.statuses = ["invalid_token", "ok"]

  notify_owner(db, _owner_id(db), title="Hi", body=None, source_type="agent")

  survivors = _tokens(db)
  assert len(survivors) == 1 and survivors[0] == relay.sends[0][0][1]


def test_a_phone_that_registers_again_during_a_send_is_kept(client, auth, db, relay, monkeypatch):
  _put(client, auth, token=FCM)
  relay.statuses = ["invalid_token"]
  real_send = push_relay.send

  def send_while_the_phone_re_registers(tokens, **message):
    statuses = real_send(tokens, **message)
    _put(client, auth, token=FCM)
    return statuses

  monkeypatch.setattr(push_relay, "send", send_while_the_phone_re_registers)

  notify_owner(db, _owner_id(db), title="Hi", body=None, source_type="agent")

  assert _tokens(db) == [FCM]


def test_a_collapse_key_over_64_bytes_is_hashed_to_fit():
  from app.push import _collapse_key

  short = "ü" * 32  # 32 characters, 64 bytes
  assert _collapse_key(short, "n1") == short
  hashed = _collapse_key("ü" * 33, "n1")
  assert hashed.startswith("c:") and len(hashed.encode()) == 64


def test_a_relay_outage_keeps_the_devices_and_the_history(client, auth, db, relay):
  _put(client, auth, token=FCM)
  relay.error = push_relay.RelayError("http_503", status=503)

  notification_id = notify_owner(db, _owner_id(db), title="Hi", body=None, source_type="agent")

  assert _tokens(db) == [FCM]
  assert db.get(models.Notification, notification_id) is not None


def test_quiet_maintenance_notices_never_reach_the_phone(client, auth, db, relay):
  _put(client, auth, token=FCM)

  notify_owner(db, _owner_id(db), title="Updated", body=None, source_type="platform_update")

  assert relay.sends == []


def test_device_routes_require_authentication(client):
  assert client.put("/api/push/devices", json={"token": FCM}).status_code == 401


def test_settings_lists_phones_without_their_full_token(client, auth, db, relay):
  _put(client, auth, token=FCM)

  [device] = client.get("/api/push/devices", headers=auth).json()

  assert device["tokenTail"] == FCM[-6:]
  assert FCM not in str(device)
  assert device["registeredAt"].endswith("+00:00")


def test_settings_removes_a_phone_by_id_and_unbinds_it(client, auth, db, relay):
  _put(client, auth, token=FCM)
  [device] = client.get("/api/push/devices", headers=auth).json()

  for _ in range(2):
    response = client.delete(f"/api/push/devices/{device['id']}", headers=auth)
    assert response.status_code == 204
  assert _tokens(db) == [] and relay.unbinds == [FCM]


def test_settings_cannot_see_or_remove_another_owners_phone(client, auth, db, relay):
  other = models.Owner(username="other-owner", hashed_password="x")
  db.add(other)
  db.flush()
  db.add(models.PushDevice(id="dev-other", owner_id=other.id, token=FCM))
  db.commit()

  assert client.get("/api/push/devices", headers=auth).json() == []
  assert client.delete("/api/push/devices/dev-other", headers=auth).status_code == 204
  assert _tokens(db) == [FCM] and relay.unbinds == []


def test_a_tap_link_too_large_once_encoded_falls_back_to_the_shell():
  from app.push import _tap_url

  assert _tap_url("/shell/?chat=c1").endswith("/shell/?chat=c1")
  # Under 2048 characters, but well over 2048 bytes once encoded.
  assert _tap_url("/shell/?q=" + "ü" * 1500).endswith("/shell/")


def test_removing_a_phone_keeps_it_when_it_registered_again_meanwhile(client, auth, db, relay):
  from app.routes.push import _remove_observed

  _put(client, auth, token=FCM)
  observed = db.query(
    models.PushDevice.id, models.PushDevice.token, models.PushDevice.registered_at,
  ).all()
  _put(client, auth, token=FCM)  # the phone re-registers while removal is paused

  assert _remove_observed(db, _owner_id(db), observed) == []

  assert _tokens(db) == [FCM] and relay.unbinds == []


def test_a_phone_registered_again_before_the_unbind_is_bound_again(client, auth, db, relay):
  from app.routes.push import _remove_observed, _unbind_removed

  _put(client, auth, token=FCM)
  observed = db.query(
    models.PushDevice.id, models.PushDevice.token, models.PushDevice.registered_at,
  ).all()
  removed = _remove_observed(db, _owner_id(db), observed)
  _put(client, auth, token=FCM)  # registers between the deletion and the unbind
  binds_before = len(relay.binds)

  _unbind_removed(removed)

  assert relay.unbinds == [FCM]
  assert len(relay.binds) == binds_before + 1 and _tokens(db) == [FCM]


def test_a_removed_phone_that_stayed_removed_is_only_unbound(client, auth, db, relay):
  from app.routes.push import _unbind_removed

  _unbind_removed([FCM])

  assert relay.unbinds == [FCM] and relay.binds == []


def test_a_slow_relay_does_not_hold_up_the_notification(client, auth, db, relay, monkeypatch):
  monkeypatch.setattr(app_push, "_send_native_later", REAL_SEND_NATIVE_LATER)
  _put(client, auth, token=FCM)
  release, sent = threading.Event(), threading.Event()

  def slow_send(tokens, **message):
    release.wait(5)
    sent.set()
    return ["ok"] * len(tokens)

  monkeypatch.setattr(push_relay, "send", slow_send)
  started = time.monotonic()
  notify_owner(db, _owner_id(db), title="Hi", body=None, source_type="agent")

  assert time.monotonic() - started < 2 and not sent.is_set()
  release.set()
  assert sent.wait(5)


def test_a_failing_background_send_is_logged_not_raised(client, auth, db, relay, monkeypatch, caplog):
  _put(client, auth, token=FCM)

  def broken(tokens, **message):
    raise RuntimeError("boom")

  monkeypatch.setattr(push_relay, "send", broken)

  notify_owner(db, _owner_id(db), title="Hi", body=None, source_type="agent")

  assert "native push delivery failed" in caplog.text
  assert _tokens(db) == [FCM]


def test_racing_registrations_of_a_new_phone_both_succeed(client, auth, db, relay, monkeypatch):
  from app.database import SessionLocal
  from app.routes import push as push_routes

  real_find = push_routes._find_device
  calls = []

  def find_after_a_concurrent_insert(session, token):
    calls.append(token)
    if len(calls) == 1:
      # Another request inserts the same new token between our lookup and commit.
      with SessionLocal() as other:
        other.add(models.PushDevice(id=str(uuid.uuid4()), owner_id=_owner_id(other), token=token))
        other.commit()
      return None
    return real_find(session, token)

  monkeypatch.setattr(push_routes, "_find_device", find_after_a_concurrent_insert)

  response = _put(client, auth, token=FCM)

  assert response.status_code == 200
  assert len(calls) == 2 and _tokens(db) == [FCM]
