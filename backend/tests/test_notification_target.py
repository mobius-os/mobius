"""Notification deep-link target round-trip (feature 094).

A push notification's target must use the in-scope `/shell/?app=<id>`
form so a COLD tap reopens the installed standalone PWA instead of a
browser tab (the PWA manifest scope is `/shell/`). The backend treats
`target` as opaque — it forwards whatever the sender supplies verbatim —
so the contract this file locks in is that the new form survives the
send → persist → history read path unchanged, alongside the legacy form.
"""

import pytest


@pytest.mark.parametrize(
  "target",
  [
    "/shell/?app=42",
    "/shell/?chat=abc-123",
    "/app/42",  # legacy form still accepted for back-compat
    "/chat/abc-123",
  ],
)
def test_notification_target_round_trips(client, auth, target):
  """A send with a deep-link target persists it verbatim and returns it
  unchanged in the notification history."""
  r = client.post(
    "/api/notifications/send",
    headers=auth,
    json={
      "title": "Task complete",
      "body": "Your app is ready.",
      "target": target,
    },
  )
  assert r.status_code == 200, r.text
  notif_id = r.json()["id"]

  hist = client.get("/api/notifications", headers=auth)
  assert hist.status_code == 200, hist.text
  row = next((n for n in hist.json() if n["id"] == notif_id), None)
  assert row is not None, "sent notification missing from history"
  assert row["target"] == target


def test_notification_action_targets_round_trip(client, auth):
  """Per-action deep-link targets (open_app / open_chat) round-trip too —
  the new in-scope form is used in both the top-level target and actions."""
  r = client.post(
    "/api/notifications/send",
    headers=auth,
    json={
      "title": "Task complete",
      "body": "Your app is ready.",
      "target": "/shell/?app=7",
      "actions": [
        {"action": "open_app", "title": "Open App",
         "target": "/shell/?app=7"},
        {"action": "open_chat", "title": "View Chat",
         "target": "/shell/?chat=c-7"},
      ],
    },
  )
  assert r.status_code == 200, r.text
  notif_id = r.json()["id"]

  hist = client.get("/api/notifications", headers=auth)
  row = next((n for n in hist.json() if n["id"] == notif_id), None)
  assert row is not None
  assert row["target"] == "/shell/?app=7"
  actions = {a["action"]: a["target"] for a in (row["actions"] or [])}
  assert actions["open_app"] == "/shell/?app=7"
  assert actions["open_chat"] == "/shell/?chat=c-7"
