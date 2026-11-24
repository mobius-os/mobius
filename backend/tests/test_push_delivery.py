from app import models
from app.database import checked_out_connections
import asyncio
import threading

from app.push import notify_owner, notify_owner_async


def _owner_with_subscription(db):
  owner = db.query(models.Owner).first()
  assert owner is not None
  db.add(models.PushSubscription(
    id="sub-1",
    owner_id=owner.id,
    endpoint="https://push.example/sub-1",
    p256dh="p256",
    auth="auth",
  ))
  db.commit()
  return owner


def test_notify_owner_sends_normal_agent_push(db, auth, monkeypatch):
  owner = _owner_with_subscription(db)
  sent = []
  baseline = checked_out_connections()

  def fake_send_push(subscription_info, payload):
    assert checked_out_connections() == baseline
    sent.append((subscription_info, payload))
    return True

  monkeypatch.setattr("app.push.send_push", fake_send_push)

  notif_id = notify_owner(
    db,
    owner.id,
    title="Task complete",
    body="Your app is ready.",
    source_type="agent",
    target="/shell/?chat=chat-1",
  )

  assert db.get(models.Notification, notif_id) is not None
  assert len(sent) == 1
  assert sent[0][1]["title"] == "Task complete"


def test_notify_owner_async_keeps_remote_delivery_off_the_event_loop(
  db, auth, monkeypatch,
):
  owner = _owner_with_subscription(db)
  loop_thread = threading.get_ident()
  delivery_threads = []

  def fake_send_push(subscription_info, payload):
    del subscription_info, payload
    delivery_threads.append(threading.get_ident())
    return True

  monkeypatch.setattr("app.push.send_push", fake_send_push)
  notification_id = asyncio.run(notify_owner_async(
    db,
    owner.id,
    title="Turn ready",
    body="Continuing now.",
    source_type="system",
    source_id="chat-1",
  ))

  assert db.get(models.Notification, notification_id) is not None
  assert delivery_threads
  assert all(thread_id != loop_thread for thread_id in delivery_threads)


def test_notify_owner_saves_platform_maintenance_without_push(
  db, auth, monkeypatch,
):
  owner = _owner_with_subscription(db)
  sent = []
  monkeypatch.setattr(
    "app.push.send_push",
    lambda subscription_info, payload: sent.append(payload) or True,
  )

  notif_id = notify_owner(
    db,
    owner.id,
    title="Platform update needs conflict resolution",
    body="The platform update conflicts with local edits.",
    source_type="platform_conflict",
    source_id="chat-1",
    target="/shell/?chat=chat-1",
  )

  row = db.get(models.Notification, notif_id)
  assert row is not None
  assert row.title == "Platform update needs conflict resolution"
  assert sent == []


def test_notify_owner_quiets_shell_maintenance_source_type(db, auth, monkeypatch):
  # Shell-rebuild notices stay quiet by declaring a maintenance source_type,
  # not by their wording. Suppression is gated purely on source_type membership
  # so a push's copy can never change whether it pops (see
  # _is_quiet_maintenance_push's invariant).
  owner = _owner_with_subscription(db)
  sent = []
  monkeypatch.setattr(
    "app.push.send_push",
    lambda subscription_info, payload: sent.append(payload) or True,
  )

  notif_id = notify_owner(
    db,
    owner.id,
    title="Shell rebuild failed",
    body=None,
    source_type="shell_rebuild_failed",
  )

  assert db.get(models.Notification, notif_id) is not None
  assert sent == []


def test_notify_owner_resume_push_never_swallowed_by_wording(db, auth, monkeypatch):
  # A resume push carries maintenance-adjacent wording ("paused … update") but a
  # system source_type, so it must ALWAYS deliver — the copy must never trip the
  # maintenance suppressor. This pins the invariant behind item 5.
  owner = _owner_with_subscription(db)
  sent = []
  monkeypatch.setattr(
    "app.push.send_push",
    lambda subscription_info, payload: sent.append(payload) or True,
  )

  notif_id = notify_owner(
    db,
    owner.id,
    title="Turn paused for a platform update",
    body="Your turn was paused for a platform update — tap to resume.",
    source_type="system",
  )

  assert db.get(models.Notification, notif_id) is not None
  assert len(sent) == 1


def test_notify_owner_does_not_quiet_app_update_copy(db, auth, monkeypatch):
  owner = _owner_with_subscription(db)
  sent = []
  monkeypatch.setattr(
    "app.push.send_push",
    lambda subscription_info, payload: sent.append(payload) or True,
  )

  notif_id = notify_owner(
    db,
    owner.id,
    title="Platform update ready",
    body="Your app has new data.",
    source_type="app",
    source_id="1",
  )

  assert db.get(models.Notification, notif_id) is not None
  assert len(sent) == 1


def test_chat_presence_suppresses_only_chat_sources_not_app_sources(
  db, auth, monkeypatch,
):
  """A chat stream's watchers speak for that chat only. An app's source_id
  names an app, so a chat stream sharing the id must not silence the app's
  push; apps are watched through the shell's reported visible apps."""
  from app.broadcast import create_broadcast, remove_broadcast

  owner = _owner_with_subscription(db)
  sent = []
  monkeypatch.setattr(
    "app.push.send_push",
    lambda subscription_info, payload: sent.append(payload) or True,
  )

  async def go():
    watched = create_broadcast("7")
    _catch_up, queue = watched.subscribe()
    try:
      notify_owner(
        db, owner.id, title="Agent reply", body=None,
        source_type="agent", source_id="7",
      )
      notify_owner(
        db, owner.id, title="App message", body=None,
        source_type="app", source_id="7",
      )
    finally:
      watched.unsubscribe(queue)
      remove_broadcast("7")

  asyncio.run(go())
  assert [payload["title"] for payload in sent] == ["App message"]


def test_web_push_request_fits_the_push_service_limit(db, auth, monkeypatch):
  """Push services refuse an encrypted request over 4096 bytes (413), so the
  body, then the title, is shortened to fit; every other field is untouched."""
  import base64
  import json
  import os

  import http_ece
  import pywebpush
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric import ec
  from py_vapid import Vapid

  from app import push

  vapid = Vapid()
  vapid.generate_keys()
  monkeypatch.setattr(push, "_vapid", vapid)
  receiver = ec.generate_private_key(ec.SECP256R1())
  auth_secret = os.urandom(16)

  def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

  owner = db.query(models.Owner).first()
  owner_id = owner.id
  db.add(models.PushSubscription(
    id="sub-size", owner_id=owner_id, endpoint="https://push.example/sub-size",
    p256dh=b64(receiver.public_key().public_bytes(
      serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )),
    auth=b64(auth_secret),
  ))
  db.commit()
  requests_sent = []

  class Response:
    status_code, reason, text, headers = 201, "Created", "", {}

  def push_service(endpoint, timeout=None, data=None, headers=None, **_kwargs):
    requests_sent.append(data)
    return Response()

  monkeypatch.setattr(pywebpush.requests, "post", push_service)
  target = "/shell/?chat=chat-size"
  for title, body in [
    ("Build finished", "x" * 4000),
    ("Party", "\U0001F389" * 400),
    ("新消息", "你" * 700),
  ]:
    requests_sent.clear()
    notification_id = notify_owner(
      db, owner_id, title=title, body=body, source_type="agent",
      source_id="chat-size", target=target, tag="thread",
    )
    assert len(requests_sent) == 1
    assert len(requests_sent[0]) <= 4096, len(requests_sent[0])
    delivered = json.loads(http_ece.decrypt(
      requests_sent[0], private_key=receiver, auth_secret=auth_secret,
    ))
    assert delivered["title"] == title
    if len(body.encode()) < 4000:
      # Sent as UTF-8, not \u escapes, the whole text fits.
      assert delivered["body"] == body
    else:
      assert delivered["body"].endswith("…")
      assert body.startswith(delivered["body"][:-1])
    assert delivered["id"] == notification_id
    assert delivered["target"] == target
    assert delivered["tag"] == "agent:chat-size:thread"
    # The full text stays in the owner's history.
    assert db.get(models.Notification, notification_id).body == body
