from app import models
from app.database import engine
from app.push import notify_owner


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
  baseline = engine.pool.checkedout()

  def fake_send_push(subscription_info, payload):
    assert engine.pool.checkedout() == baseline
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
