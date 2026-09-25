"""App presence: an app's push is not sent while a live shell stream shows it.

Mirrors chat presence. The shell reports its visibly shown apps against its
own system-stream subscription; the report lives exactly as long as that
subscription, so there is no timer or heartbeat that could go stale.
"""

import pytest

from app import auth as auth_mod
from app import models
from app.broadcast import get_system_broadcast
from app.push import notify_owner


@pytest.fixture
def sent_pushes(db, auth, monkeypatch):
  owner = db.query(models.Owner).one()
  db.add(models.PushSubscription(
    id="sub-presence", owner_id=owner.id,
    endpoint="https://push.example/presence", p256dh="p256", auth="auth",
  ))
  db.commit()
  sent = []
  monkeypatch.setattr(
    "app.push.send_push", lambda _sub, payload: sent.append(payload) or True,
  )
  return sent


@pytest.fixture
def shell_stream():
  broadcast = get_system_broadcast()
  subscription = broadcast.subscribe()
  yield subscription
  broadcast.unsubscribe(subscription)


def _report(client, auth, subscription_id, sequence, app_ids):
  return client.post(
    f"/api/events/system/{subscription_id}/visible-apps",
    headers=auth, json={"sequence": sequence, "app_ids": app_ids},
  )


def _notify_app(db, app_id, title):
  owner = db.query(models.Owner).one()
  return notify_owner(
    db, owner.id, title=title, body=None,
    source_type="app", source_id=app_id,
  )


def test_app_push_is_withheld_only_while_a_live_stream_shows_that_app(
  client, auth, db, sent_pushes, shell_stream,
):
  assert _report(client, auth, shell_stream.id, 1, ["7"]).status_code == 204

  withheld = _notify_app(db, "7", "while visible")
  _notify_app(db, "8", "other app")
  assert [p["title"] for p in sent_pushes] == ["other app"]
  # The history row (and bell) still exist for the withheld push.
  assert db.get(models.Notification, withheld) is not None

  assert _report(client, auth, shell_stream.id, 2, []).status_code == 204
  _notify_app(db, "7", "after hidden")
  assert sent_pushes[-1]["title"] == "after hidden"


def test_disconnecting_the_stream_drops_its_report(
  client, auth, db, sent_pushes,
):
  broadcast = get_system_broadcast()
  subscription = broadcast.subscribe()
  assert _report(client, auth, subscription.id, 1, ["7"]).status_code == 204
  broadcast.unsubscribe(subscription)

  _notify_app(db, "7", "after disconnect")
  assert [p["title"] for p in sent_pushes] == ["after disconnect"]
  # A report for the gone stream tells the shell to reconnect and re-report.
  assert _report(client, auth, subscription.id, 2, ["7"]).status_code == 404


def test_a_late_older_report_cannot_resurrect_visibility(
  client, auth, db, sent_pushes, shell_stream,
):
  """A delayed "visible" must never override a newer "hidden"."""
  assert _report(client, auth, shell_stream.id, 5, []).status_code == 204
  assert _report(client, auth, shell_stream.id, 4, ["7"]).status_code == 204
  _notify_app(db, "7", "hidden wins")
  assert [p["title"] for p in sent_pushes] == ["hidden wins"]


def test_visible_app_reports_require_the_same_site_owner(
  client, auth, db, shell_stream,
):
  app = models.App(
    slug="presence-reporter", source_dir="/tmp/mobius-tests/presence-reporter",
    name="Reporter", description="", jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  owner = db.query(models.Owner).one()
  app_auth = {"Authorization": "Bearer " + auth_mod.create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
  )}
  path = f"/api/events/system/{shell_stream.id}/visible-apps"
  body = {"sequence": 1, "app_ids": [str(app.id)]}

  assert client.post(path, json=body).status_code == 401
  assert client.post(path, headers=app_auth, json=body).status_code == 403
  assert client.post(
    path, headers={**auth, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.test"},
    json=body,
  ).status_code == 403
  assert shell_stream.visible_app_ids == frozenset()
