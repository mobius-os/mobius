"""Tests for the app-attributed chat contract (capability A).

Cover the actor gate: an app can create a chat stamped with its app_id
and send/stream to a chat it owns; it cannot touch a chat owned by the
owner or by another app. Owner tokens are unaffected.

The send path spawns the agent runner, which these tests don't want to
drive end-to-end — they assert on the AUTHORIZATION boundary (which
status code each actor gets), which is decided before any runner work.
"""

from app import models
from test_app_fixtures import create_local_app


def _make_app(client, owner_token, name):
  app_id = create_local_app(
    client, {"Authorization": f"Bearer {owner_token}"}, name=name,
  )["id"]
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
  owner_view = client.get(
    f"/api/chats/{chat_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert owner_view.status_code == 200
  assert owner_view.json()["created_by_app_id"] == app_id

  # The generic app token may create/own the chat but cannot become the nested
  # renderer principal. The renderer must exchange a one-use capability for its
  # exact chat/session; that positive path is pinned in
  # test_chat_embed_capability.py.
  app_view = client.get(
    f"/api/chats/{chat_id}",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert app_view.status_code == 403, app_view.text

  # The app can send to its own chat (202 — accepted + runner spawned).
  r = client.post(
    f"/api/chats/{chat_id}/messages", json={"content": "hello agent"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 202, r.text


def test_owner_chat_list_includes_only_owner_visible_app_chats(
  client, owner_token, db
):
  app_id, app_token = _make_app(client, owner_token, "drawer-app")
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_auth = {"Authorization": f"Bearer {app_token}"}

  owner = client.post(
    "/api/chats",
    json={"title": "Owner chat"},
    headers=auth,
  )
  assert owner.status_code == 200, owner.text

  hidden = client.post(
    "/api/app-chats",
    json={"title": "Embedded panel"},
    headers=app_auth,
  )
  assert hidden.status_code == 201, hidden.text

  visible = client.post(
    "/api/app-chats",
    json={"title": "Repair chat", "owner_visible": True},
    headers=app_auth,
  )
  assert visible.status_code == 201, visible.text
  visible_id = visible.json()["id"]

  row = db.query(models.Chat).filter(models.Chat.id == visible_id).first()
  assert row.created_by_app_id == app_id
  assert row.agent_settings_json["owner_visible"] is True

  drawer = client.get("/api/chats", headers=auth)
  assert drawer.status_code == 200, drawer.text
  drawer_ids = {c["id"] for c in drawer.json()}
  assert owner.json()["id"] in drawer_ids
  assert visible.json()["id"] in drawer_ids
  assert hidden.json()["id"] not in drawer_ids

  all_chats = client.get("/api/chats?include_app_chats=1", headers=auth)
  assert all_chats.status_code == 200, all_chats.text
  all_ids = {c["id"] for c in all_chats.json()}
  assert {owner.json()["id"], visible.json()["id"], hidden.json()["id"]} <= all_ids


def test_app_chat_create_and_patch_store_custom_system_prompt(
  client, owner_token, db
):
  app_id, app_token = _make_app(client, owner_token, "prompted")

  r = client.post(
    "/api/app-chats",
    json={
      "title": "App conversation",
      "system_prompt": "You live inside the Notes app.",
      "model": "claude-sonnet-4-6",
    },
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 201, r.text
  chat_id = r.json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  assert row.created_by_app_id == app_id
  assert row.agent_settings_json["system_prompt"] == (
    "You live inside the Notes app."
  )
  assert row.agent_settings_json["model"] == "claude-sonnet-4-6"

  r = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"system_prompt": "You live inside LaTeX.", "model": ""},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200, r.text
  db.refresh(row)
  assert row.agent_settings_json["system_prompt"] == "You live inside LaTeX."
  assert "model" not in row.agent_settings_json


def test_app_chat_cannot_change_system_prompt_after_it_started(
  client, owner_token, db,
):
  _, app_token = _make_app(client, owner_token, "fixed-prompt")
  created = client.post(
    "/api/app-chats",
    json={"system_prompt": "FIRST"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert created.status_code == 201, created.text
  chat_id = created.json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.messages = [{"role": "user", "content": "started"}]
  db.commit()

  changed = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"system_prompt": "SECOND"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert changed.status_code == 409
  assert "new app chat" in changed.json()["detail"]


def test_app_chat_list_can_filter_by_scope(client, owner_token):
  _, app_token = _make_app(client, owner_token, "scoped-chatter")
  _, other_app_token = _make_app(client, owner_token, "other-scoped-chatter")
  app_auth = {"Authorization": f"Bearer {app_token}"}
  other_auth = {"Authorization": f"Bearer {other_app_token}"}

  def create(title, scope, headers=app_auth):
    r = client.post(
      "/api/app-chats",
      json={
        "title": title,
        "scope": scope,
        "scope_label": "Session A",
      },
      headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]

  session_a_1 = create("Session A notes", "workout-session:session-a")
  session_b = create("Session B notes", "workout-session:session-b")
  session_a_2 = create("Session A follow-up", "workout-session:session-a")
  foreign = create(
    "Other app same scope", "workout-session:session-a", headers=other_auth,
  )

  owner_list = client.get(
    "/api/app-chats",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert owner_list.status_code == 403, owner_list.text

  scoped = client.get(
    "/api/app-chats?scope=workout-session:session-a",
    headers=app_auth,
  )
  assert scoped.status_code == 200, scoped.text
  rows = scoped.json()
  ids = {row["id"] for row in rows}
  assert ids == {session_a_1, session_a_2}
  assert session_b not in ids
  assert foreign not in ids
  assert all(row["scope"] == "workout-session:session-a" for row in rows)
  assert all(row["scope_label"] == "Session A" for row in rows)


def test_app_chat_create_stores_report_date_and_kind(client, owner_token, db):
  """An app opening a chat about one of its reports stores the link.

  The Reflection app POSTs report_date + report_kind when the partner taps
  "Discuss this brief"; chat.py reads report_date back from
  agent_settings_json on the first turn to inject the brief as context.
  """
  app_id, app_token = _make_app(client, owner_token, "report-chatter")

  r = client.post(
    "/api/app-chats",
    json={
      "title": "Brief — 2026-06-22",
      "report_date": "2026-06-22",
      "report_kind": "reflection",
    },
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 201, r.text
  chat_id = r.json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  assert row.created_by_app_id == app_id
  assert row.agent_settings_json["report_date"] == "2026-06-22"
  assert row.agent_settings_json["report_kind"] == "reflection"


def test_app_chat_create_rejects_malformed_report_date(client, owner_token):
  """report_date is a path component downstream, so it's strictly ISO.

  A non-ISO value (separator swap, traversal attempt, garbage) is rejected
  at the schema boundary with a 422 rather than stored.
  """
  _, app_token = _make_app(client, owner_token, "bad-date")
  for bad in ("2026/06/22", "2026-6-2", "../../etc/passwd", "today"):
    r = client.post(
      "/api/app-chats",
      json={"title": "x", "report_date": bad},
      headers={"Authorization": f"Bearer {app_token}"},
    )
    assert r.status_code == 422, f"{bad!r} should be rejected: {r.text}"


def test_app_chat_create_rejects_provider_model_mismatch(
  client, owner_token,
):
  _, app_token = _make_app(client, owner_token, "mismatched-model")
  response = client.post(
    "/api/app-chats",
    json={"provider": "claude", "model": "gpt-5.4"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 422


def test_app_chat_patch_can_set_provider_before_assistant_turns(
  client, owner_token, db
):
  _, app_token = _make_app(client, owner_token, "provider-picker")

  r = client.post(
    "/api/app-chats",
    json={"title": "App conversation", "provider": "claude"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 201, r.text
  chat_id = r.json()["id"]

  r = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"provider": "codex"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200, r.text
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  assert row.provider == "codex"
  assert row.session_id is None


def test_app_chat_patch_rejects_provider_switch_after_assistant_turn(
  client, owner_token, db
):
  _, app_token = _make_app(client, owner_token, "provider-locked")

  r = client.post(
    "/api/app-chats",
    json={"title": "App conversation", "provider": "claude"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 201, r.text
  chat_id = r.json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  row.session_id = "claude-session"
  row.messages = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
  ]
  db.commit()

  r = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"provider": "codex"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 409, r.text
  db.refresh(row)
  assert row.provider == "claude"
  assert row.session_id == "claude-session"


def test_app_chat_patch_rejects_provider_switch_after_first_user_turn(
  client, owner_token, db,
):
  _, app_token = _make_app(client, owner_token, "provider-first-turn")
  response = client.post(
    "/api/app-chats",
    json={"title": "App conversation", "provider": "claude"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  chat_id = response.json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.messages = [{"role": "user", "content": "first request"}]
  row.run_status = "running"
  db.add(models.ChatRun(
    id="app-first-live-turn",
    chat_id=chat_id,
    status="running",
    provider="claude",
  ))
  db.commit()

  response = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"provider": "codex"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 409
  db.refresh(row)
  assert row.provider == "claude"


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

  # Loading either foreign transcript is forbidden by the same principal gate.
  for chat_id in ("owner-chat", "other-app-chat"):
    r = client.get(
      f"/api/chats/{chat_id}",
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
