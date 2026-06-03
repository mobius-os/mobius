"""Tests for the app-attributed chat contract (capability A).

Cover the actor gate: an app can create a chat stamped with its app_id
and send/stream to a chat it owns; it cannot touch a chat owned by the
owner or by another app. Owner tokens are unaffected.

The send path spawns the agent runner, which these tests don't want to
drive end-to-end — they assert on the AUTHORIZATION boundary (which
status code each actor gets), which is decided before any runner work.
"""

from app import models


def _make_app(client, owner_token, name):
  r = client.post(
    "/api/apps/",
    json={"name": name, "description": "t",
          "jsx_source": "export default () => null"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  tok = client.post(
    "/api/auth/app-token", json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  ).json()["token"]
  return app_id, tok


def test_app_token_can_create_and_send_to_own_chat(client, owner_token, db):
  app_id, app_token = _make_app(client, owner_token, "chatter")

  # Create an app-owned chat.
  r = client.post(
    "/api/app-chats", json={"title": "App conversation"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 201, r.text
  chat_id = r.json()["id"]
  assert r.json()["created_by_app_id"] == app_id

  # The row is stamped with the app id.
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  assert row is not None
  assert row.created_by_app_id == app_id

  # The app can send to its own chat (202 — accepted + runner spawned).
  r = client.post(
    f"/api/chats/{chat_id}/messages", json={"content": "hello agent"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 202, r.text


def test_app_cannot_touch_foreign_chat(client, owner_token, db):
  # Owner-created chat (created_by_app_id is NULL).
  owner_chat = models.Chat(id="owner-chat", title="owner's", messages=[])
  db.add(owner_chat)
  # Another app's chat.
  other = models.App(name="other", description="",
                     jsx_source="export default () => null")
  db.add(other)
  db.commit()
  db.refresh(other)
  other_chat = models.Chat(
    id="other-app-chat", title="theirs", messages=[],
    created_by_app_id=other.id,
  )
  db.add(other_chat)
  db.commit()

  _, app_token = _make_app(client, owner_token, "intruder")

  # Send to the owner's chat → 403.
  r = client.post(
    "/api/chats/owner-chat/messages", json={"content": "sneaky"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403, r.text

  # Send to another app's chat → 403.
  r = client.post(
    "/api/chats/other-app-chat/messages", json={"content": "sneaky"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403, r.text

  # Stream another app's chat → 403 (gate runs before the broadcast).
  r = client.get(
    "/api/chats/other-app-chat/stream",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403, r.text


def test_owner_token_rejected_from_app_chats_create(client, owner_token):
  """Owners use POST /api/chats; the app-chats endpoint is app-only so a
  chat's attribution is never ambiguous."""
  r = client.post(
    "/api/app-chats", json={"title": "nope"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 403, r.text


def test_owner_can_still_send_to_app_owned_chat(client, owner_token, db):
  """The created_by_app_id tag attributes the chat to an app, but the
  owner can still drive it from the shell — it's an actor tag, not a
  fence against the owner."""
  app = models.App(name="x", description="",
                   jsx_source="export default () => null")
  db.add(app)
  db.commit()
  db.refresh(app)
  chat = models.Chat(id="app-owned", title="app's", messages=[],
                     created_by_app_id=app.id)
  db.add(chat)
  db.commit()

  r = client.post(
    "/api/chats/app-owned/messages", json={"content": "owner drives it"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 202, r.text


def test_app_chat_excluded_from_history_list(client, owner_token, db):
  """App-created chats stay out of default history but remain readable."""
  app_id, app_token = _make_app(client, owner_token, "drawer-hidden")
  app_chat_id = client.post(
    "/api/app-chats", json={"title": "app panel chat"},
    headers={"Authorization": f"Bearer {app_token}"},
  ).json()["id"]
  owner_chat_id = client.post(
    "/api/chats", json={"title": "owner chat"},
    headers={"Authorization": f"Bearer {owner_token}"},
  ).json()["id"]

  listed = client.get(
    "/api/chats", headers={"Authorization": f"Bearer {owner_token}"},
  ).json()
  ids = {c["id"] for c in listed}
  assert owner_chat_id in ids
  assert app_chat_id not in ids

  with_app = client.get(
    "/api/chats?include_app_chats=1",
    headers={"Authorization": f"Bearer {owner_token}"},
  ).json()
  with_app_by_id = {c["id"]: c for c in with_app}
  assert app_chat_id in with_app_by_id
  assert with_app_by_id[app_chat_id]["created_by_app_id"] == app_id

  r = client.get(
    f"/api/chats/{app_chat_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text


def test_app_chats_create_requires_auth(client):
  r = client.post("/api/app-chats", json={"title": "x"})
  assert r.status_code == 401
