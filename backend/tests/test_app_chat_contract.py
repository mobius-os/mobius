"""Tests for the app-attributed chat contract (capability A).

Cover the actor gate: an app can create a chat stamped with its app_id
and send/stream to a chat it owns; it cannot touch a chat owned by the
owner or by another app. Owner tokens are unaffected.

The send path spawns the agent runner, which these tests don't want to
drive end-to-end — they assert on the AUTHORIZATION boundary (which
status code each actor gets), which is decided before any runner work.
"""
import json

import pytest
from sqlalchemy.orm import object_session
from app import transcript_rows
from app.chat_writer import create_chat

from fastapi.responses import JSONResponse

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


def _stub_first_turn(monkeypatch, db):
  """Accept one first turn without launching a provider subprocess."""
  from app.routes import chats_stream

  async def accept(body, chat_id, principal, request_db):
    row = request_db.query(models.Chat).filter(models.Chat.id == chat_id).one()
    transcript_rows.replace_all(object_session(row), row, [{"role": "user", "content": body.content, "cid": body.cid}])
    row.has_messages = True
    request_db.commit()
    return JSONResponse({"status": "started"}, status_code=202)

  monkeypatch.setattr(chats_stream, "send_message", accept)


def test_app_token_can_create_and_send_to_own_chat(client, owner_token, db):
  app_id, app_token = _make_app(client, owner_token, "chatter")

  # Create an app-owned chat.
  r = client.post(
    "/api/app-chats", json={
      "title": "App conversation",
      "provider": "claude",
      "model": "claude-opus-4-8",
    },
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


def test_scoped_app_chat_start_reuses_one_exact_first_turn(
  client, owner_token, db, monkeypatch,
):
  from app import activity

  app_id, app_token = _make_app(client, owner_token, "atomic-scoped-chat")
  _stub_first_turn(monkeypatch, db)
  events = []
  monkeypatch.setattr(
    activity, "log_event", lambda ev, **fields: events.append((ev, fields)) or True,
  )
  auth = {"Authorization": f"Bearer {app_token}"}
  payload = {
    "title": "Exact review",
    "scope": "contribute-review:head-1",
    "scope_label": "Review contribution",
    "owner_visible": True,
    "content": "private review prompt",
    "cid": "first-cid",
    "timezone": "Europe/London",
  }

  first = client.post("/api/app-chats/start", json=payload, headers=auth)
  second = client.post(
    "/api/app-chats/start",
    json={**payload, "cid": "second-cid"},
    headers=auth,
  )

  assert first.status_code == 200, first.text
  assert second.status_code == 200, second.text
  assert first.json()["outcome"] == "started"
  assert second.json() == {
    "chat_id": first.json()["chat_id"],
    "outcome": "reused",
    "response": None,
  }
  rows = db.query(models.Chat).filter(
    models.Chat.created_by_app_id == app_id,
  ).all()
  assert len(rows) == 1
  assert [message["cid"] for message in list(transcript_rows.history(rows[0]))] == ["first-cid"]
  handoffs = [fields for ev, fields in events if ev == "app_chat_handoff"]
  assert [row["outcome"] for row in handoffs] == ["started", "reused"]
  assert all(row["scope"] == payload["scope"] for row in handoffs)
  assert all(row["chat_id"] == first.json()["chat_id"] for row in handoffs)
  assert all("content" not in row for row in handoffs)


def test_scoped_app_chat_start_keeps_different_scopes_independent(
  client, owner_token, db, monkeypatch,
):
  app_id, app_token = _make_app(client, owner_token, "independent-scoped-chat")
  _stub_first_turn(monkeypatch, db)
  auth = {"Authorization": f"Bearer {app_token}"}

  responses = [
    client.post("/api/app-chats/start", json={
      "scope": scope,
      "content": f"review {scope}",
    }, headers=auth)
    for scope in ("review:one", "review:two")
  ]

  assert all(response.status_code == 200 for response in responses)
  assert {response.json()["outcome"] for response in responses} == {"started"}
  assert len({response.json()["chat_id"] for response in responses}) == 2
  assert db.query(models.Chat).filter(
    models.Chat.created_by_app_id == app_id,
  ).count() == 2


def test_owner_cannot_start_an_app_attributed_scoped_chat(client, owner_token):
  response = client.post(
    "/api/app-chats/start",
    json={"scope": "review:owner", "content": "not app attributed"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 403


def test_app_chat_first_send_preserves_provider_selected_at_create(
  client, owner_token, db, monkeypatch,
):
  """An unrelated owner default cannot replace an app chat's provider."""
  from app.routes import chats_stream

  _, app_token = _make_app(client, owner_token, "provider-preserving-chat")
  monkeypatch.setattr(
    chats_stream, "owner_default_provider", lambda *_: "claude",
  )

  for sender_token in (app_token, owner_token):
    created = client.post(
      "/api/app-chats",
      json={
        "title": "Codex workflow",
        "provider": "codex",
        "model": "gpt-5.6-sol",
      },
      headers={"Authorization": f"Bearer {app_token}"},
    )
    assert created.status_code == 201, created.text
    chat_id = created.json()["id"]

    sent = client.post(
      f"/api/chats/{chat_id}/messages",
      json={"content": "run the selected workflow provider"},
      headers={"Authorization": f"Bearer {sender_token}"},
    )
    assert sent.status_code == 202, sent.text
    db.expire_all()
    row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
    assert row.provider == "codex"
    run = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == chat_id,
    ).order_by(models.ChatRun.started_at.desc()).first()
    assert run is not None
    assert run.provider == "codex"


def test_owner_chat_first_send_still_uses_latest_selected_provider(
  client, owner_token, db, monkeypatch,
):
  """The app-chat exception must not freeze an ordinary empty chat."""
  from app.routes import chats_stream

  created = client.post(
    "/api/chats",
    json={"title": "Empty owner chat"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert created.status_code == 200, created.text
  chat_id = created.json()["id"]
  monkeypatch.setattr(
    chats_stream, "owner_default_provider", lambda *_: "codex",
  )
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  row.agent_settings_json = {"model": "gpt-5.6-sol"}
  db.commit()

  sent = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "use my latest selected provider"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert sent.status_code == 202, sent.text
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  assert row.provider == "codex"
  run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == chat_id,
  ).order_by(models.ChatRun.started_at.desc()).first()
  assert run is not None
  assert run.provider == "codex"


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
      "provider": "claude",
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
  assert r.status_code == 422, r.text
  db.refresh(row)
  assert row.agent_settings_json["system_prompt"] == "You live inside the Notes app."
  assert row.agent_settings_json["model"] == "claude-sonnet-4-6"

  r = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"system_prompt": "You live inside LaTeX."},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200, r.text
  db.refresh(row)
  assert row.agent_settings_json["system_prompt"] == "You live inside LaTeX."
  assert row.agent_settings_json["model"] == "claude-sonnet-4-6"


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
  transcript_rows.replace_all(object_session(row), row, [{"role": "user", "content": "started"}])
  db.commit()

  changed = client.patch(
    f"/api/app-chats/{chat_id}",
    json={"system_prompt": "SECOND"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert changed.status_code == 409
  assert "new app chat" in changed.json()["detail"]


def test_app_chat_list_can_filter_by_scope(client, owner_token, db):
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
  db.add(models.ChatRun(
    id="session-a-measured-run",
    chat_id=session_a_2,
    status="completed",
    provider="codex",
    input_tokens=900,
    output_tokens=100,
    total_tokens=1_000,
    usage_json={"provider": "codex"},
  ))
  db.commit()

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
  measured = next(row for row in rows if row["id"] == session_a_2)
  assert measured["usage"]["coverage"] == {
    "runs": 1,
    "runs_with_usage": 1,
  }
  assert measured["usage"]["totals"]["total_tokens"] == 1_000
  assert "cost_usd" not in measured["usage"]["totals"]


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
  assert row.agent_settings_json["model"] == "gpt-5.6-sol"
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
  transcript_rows.replace_all(object_session(row), row, [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
  ])
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
  transcript_rows.replace_all(object_session(row), row, [{"role": "user", "content": "first request"}])
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
  owner_chat = create_chat(id="owner-chat", title="owner's", messages=[])
  db.add(owner_chat)
  # Another app's chat.
  other = models.App(
    slug="test-app-chat-contract-362",
    source_dir="/tmp/mobius-tests/test-app-chat-contract-362",
    name="other", description="",
    jsx_source="export default () => null",
  )
  db.add(other)
  db.commit()
  db.refresh(other)
  other_chat = create_chat(
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
  app = models.App(
    slug="test-app-chat-contract-420",
    source_dir="/tmp/mobius-tests/test-app-chat-contract-420",
    name="x", description="",
    jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  chat = create_chat(
    id="app-owned",
    title="app's",
    messages=[],
    created_by_app_id=app.id,
    agent_settings_json={"model": "claude-opus-4-8"},
  )
  db.add(chat)
  db.commit()

  r = client.post(
    "/api/chats/app-owned/messages", json={"content": "owner drives it"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 202, r.text


def test_app_token_cannot_forge_a_manual_resume_marker(client, owner_token):
  app_id, app_token = _make_app(client, owner_token, "resume-marker-app")
  del app_id
  created = client.post(
    "/api/app-chats", json={"title": "App conversation"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert created.status_code == 201, created.text

  response = client.post(
    f"/api/chats/{created.json()['id']}/messages",
    json={"content": "continue", "continuation": "manual"},
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 403


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


def test_an_app_chat_keeps_the_name_its_app_gave_it(client, owner_token, db):
  _app_id, app_token = _make_app(client, owner_token, "named-chat")
  auth = {"Authorization": f"Bearer {app_token}"}
  named = client.post("/api/app-chats", json={"title": "Reflection — 2026-09-26"}, headers=auth)
  unnamed = client.post("/api/app-chats", json={}, headers=auth)

  locked = {
    row.id: row.title_locked
    for row in db.query(models.Chat).filter(
      models.Chat.id.in_([named.json()["id"], unnamed.json()["id"]]),
    )
  }
  assert locked == {named.json()["id"]: True, unnamed.json()["id"]: False}


def test_app_chat_list_shows_when_a_chat_awaits_the_owner(client, owner_token, db):
  _app_id, app_token = _make_app(client, owner_token, "asking-app")
  auth = {"Authorization": f"Bearer {app_token}"}
  waiting = client.post("/api/app-chats", json={"title": "Asks"}, headers=auth).json()["id"]
  idle = client.post("/api/app-chats", json={"title": "Idle"}, headers=auth).json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == waiting).one()
  row.pending_question_id = "question-1"
  db.commit()

  listed = {chat["id"]: chat["awaiting_owner"] for chat in client.get("/api/app-chats", headers=auth).json()}

  assert listed[waiting] is True
  assert listed[idle] is False


def test_app_chat_list_shows_whether_a_run_is_live(client, owner_token, db, monkeypatch):
  _app_id, app_token = _make_app(client, owner_token, "running-app")
  auth = {"Authorization": f"Bearer {app_token}"}
  live = client.post("/api/app-chats", json={"title": "Live"}, headers=auth).json()["id"]
  idle = client.post("/api/app-chats", json={"title": "Idle"}, headers=auth).json()["id"]
  monkeypatch.setattr("app.routes.chats.is_chat_running", lambda chat_id: chat_id == live)

  listed = {chat["id"]: chat["running"] for chat in client.get("/api/app-chats", headers=auth).json()}

  assert listed == {live: True, idle: False}


def test_app_can_rename_and_promote_only_its_own_chat(client, owner_token, db):
  app_id, token = _make_app(client, owner_token, "companion-chat")
  auth = {"Authorization": f"Bearer {token}"}
  created = client.post("/api/app-chats", headers=auth, json={
    "scope": "companion:one", "system_prompt": "Keep this prompt",
  })
  assert created.status_code == 201, created.text
  chat_id = created.json()["id"]
  row = db.get(models.Chat, chat_id)
  original_settings = dict(row.agent_settings_json)
  assert row.title_locked is False

  changed = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
    "title": "  Companion conversation  ", "owner_visible": True,
  })
  assert changed.status_code == 200, changed.text
  db.refresh(row)
  assert row.title == "Companion conversation"
  assert row.title_locked is True
  assert row.created_by_app_id == app_id
  assert row.agent_settings_json == {**original_settings, "owner_visible": True}
  summary = client.get("/api/app-chats", headers=auth).json()[0]
  assert summary["title"] == row.title
  assert summary["title_locked"] is True
  assert summary["owner_visible"] is True
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  assert chat_id in {r["id"] for r in client.get("/api/chats", headers=owner_auth).json()}

  # Omission, null and blank titles do not undo an explicit name or visibility.
  for payload in ({}, {"title": None, "owner_visible": None}, {"title": "   "}):
    response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json=payload)
    assert response.status_code == 200, response.text
    db.refresh(row)
    assert row.title == "Companion conversation"
    assert row.title_locked is True
    assert row.agent_settings_json == {**original_settings, "owner_visible": True}

  hidden = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
    "owner_visible": False,
  })
  assert hidden.status_code == 200, hidden.text
  db.refresh(row)
  assert row.agent_settings_json == original_settings
  summary = client.get("/api/app-chats", headers=auth).json()[0]
  assert summary["owner_visible"] is False
  assert summary["title_locked"] is True
  assert chat_id not in {r["id"] for r in client.get("/api/chats", headers=owner_auth).json()}


@pytest.mark.parametrize("title", ["x" * 501, "bad\x00title", "bad\ntitle"])
def test_app_chat_patch_rejects_invalid_title_without_promoting_chat(
  client, owner_token, db, title,
):
  _, token = _make_app(client, owner_token, "invalid-title")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={}).json()["id"]
  response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
    "title": title, "owner_visible": True,
  })
  assert response.status_code == 422, response.text
  row = db.get(models.Chat, chat_id)
  assert row.title == "New chat"
  assert row.title_locked is False
  assert not row.agent_settings_json.get("owner_visible")


def test_app_chat_presentation_patch_preserves_started_chat_guards(
  client, owner_token, db,
):
  _, token = _make_app(client, owner_token, "started-presentation")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={
    "provider": "claude", "system_prompt": "Original prompt",
  }).json()["id"]
  row = db.get(models.Chat, chat_id)
  row.has_messages = True
  db.commit()
  for rejected in ({"system_prompt": "Replacement"}, {"provider": "codex"}):
    response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
      **rejected, "title": "Must not stick", "owner_visible": True,
    })
    assert response.status_code == 409, response.text
    db.refresh(row)
    assert row.title == "New chat"
    assert row.title_locked is False
    assert not row.agent_settings_json.get("owner_visible")
  response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
    "title": "x" * 500, "owner_visible": True,
  })
  assert response.status_code == 200, response.text
  db.refresh(row)
  assert row.title == "x" * 500
  assert row.title_locked is True
  assert row.agent_settings_json["system_prompt"] == "Original prompt"
  assert row.provider == "claude"


def test_app_chat_presentation_patch_cannot_change_foreign_or_deleted_chats(
  client, owner_token, db, presentation_events,
):
  app_id, token = _make_app(client, owner_token, "presentation-owner")
  other_id, _ = _make_app(client, owner_token, "presentation-other")
  from datetime import UTC, datetime

  for chat_id, app, deleted, status in (
    ("owner-presentation", None, None, 403),
    ("other-presentation", other_id, None, 403),
    ("deleted-presentation", app_id, datetime.now(UTC), 404),
  ):
    row = create_chat(id=chat_id, title="Original", messages=[], created_by_app_id=app)
    row.deleted_at = deleted
    db.add(row)
    db.commit()
    presentation_events.clear()
    response = client.patch(f"/api/app-chats/{chat_id}", json={
      "title": "Changed", "owner_visible": True,
    }, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == status, response.text
    assert presentation_events == []
    db.refresh(row)
    assert row.title == "Original"
    assert not (row.agent_settings_json or {}).get("owner_visible")
  own = client.post("/api/app-chats", json={}, headers={
    "Authorization": f"Bearer {token}",
  }).json()["id"]
  presentation_events.clear()
  response = client.patch(f"/api/app-chats/{own}", json={"title": "Owner"}, headers={
    "Authorization": f"Bearer {owner_token}",
  })
  assert response.status_code == 403, response.text
  assert presentation_events == []


def test_app_chat_summary_tracks_card_identity_without_card_contents(
  client, owner_token, db,
):
  app_id, token = _make_app(client, owner_token, "card-status")
  row = create_chat(
    id="card-status-chat", title="Card status", created_by_app_id=app_id,
    messages=[{"role": "assistant", "content": "Private prose", "blocks": [{
      "type": "question", "id": "card-one", "questions": ["Private question"],
      "answers": {"answer": "Private answer"},
    }]}],
  )
  db.add(row)
  db.commit()
  for pending in ("card-one", "card-two", None):
    row.pending_question_id = pending
    db.commit()
    response = client.get("/api/app-chats", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    summary = response.json()[0]
    assert summary["pending_question_id"] == pending
    assert summary["awaiting_owner"] is bool(pending)
    assert summary["owner_visible"] is False
    assert summary["title_locked"] is False
    assert not {"messages", "blocks", "questions", "answers"} & summary.keys()
    assert "Private" not in json.dumps(summary)


@pytest.fixture
def presentation_events(monkeypatch):
  from app.routes import chats
  events = []

  class Broadcast:
    def publish(self, event):
      events.append(event)

  monkeypatch.setattr(chats, "get_system_broadcast", lambda: Broadcast())
  return events


def test_app_presentation_events_describe_committed_rows_only(
  client, owner_token, db, monkeypatch,
):
  from app.database import SessionLocal
  from app.routes import chats
  from app.chat_visibility import visible_in_owner_drawer

  _, token = _make_app(client, owner_token, "committed-presentation")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={}).json()["id"]
  row = db.get(models.Chat, chat_id)
  row.pending_question_id = "private-card-identity"
  db.commit()
  events = []
  committed = []

  class Broadcast:
    def publish(self, event):
      # A distinct session proves neither event describes uncommitted state.
      with SessionLocal() as reader:
        saved = reader.get(models.Chat, chat_id)
        committed.append((saved.title, saved.title_locked, visible_in_owner_drawer(saved)))
      events.append(event)

  monkeypatch.setattr(chats, "get_system_broadcast", lambda: Broadcast())
  for payload, types, visible in (
    ({"title": "  Companion  ", "owner_visible": True},
     ["chat_renamed", "chat_visibility_changed"], True),
    ({"owner_visible": False}, ["chat_visibility_changed"], False),
    ({"owner_visible": True}, ["chat_visibility_changed"], True),
    ({"title": "Renamed"}, ["chat_renamed"], True),
  ):
    events.clear()
    committed.clear()
    response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json=payload)
    assert response.status_code == 200, response.text
    db.refresh(row)
    assert [event["type"] for event in events] == types
    assert committed == [(row.title, True, visible)] * len(types)
    assert all(event["chatId"] == chat_id for event in events)
    for event in events:
      if event["type"] == "chat_renamed":
        assert event == chats.renamed_event(row)
      else:
        assert event == {"type": "chat_visibility_changed", "chatId": chat_id}
    assert "private-card-identity" not in json.dumps(events)
    listed = client.get(f"/api/chats?ids={chat_id}", headers={
      "Authorization": f"Bearer {owner_token}",
    }).json()
    assert (chat_id in {item["id"] for item in listed}) is visible


@pytest.mark.parametrize("payload", [
  {}, {"title": None, "owner_visible": None}, {"title": "  Same  "},
  {"title": "   "}, {"owner_visible": True}, {"scope_label": "New label"},
])
def test_app_presentation_noops_emit_nothing(
  client, owner_token, presentation_events, payload,
):
  _, token = _make_app(client, owner_token, "presentation-noop")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={
    "title": "Same", "owner_visible": True,
  }).json()["id"]
  presentation_events.clear()
  response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json=payload)
  assert response.status_code == 200, response.text
  assert presentation_events == []


@pytest.mark.parametrize("hidden", [True, False])
def test_app_presentation_events_respect_explicit_drawer_override(
  client, owner_token, db, presentation_events, hidden,
):
  _, token = _make_app(client, owner_token, "visibility-override")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={}).json()["id"]
  row = db.get(models.Chat, chat_id)
  row.agent_settings_json = {**row.agent_settings_json, "drawer_hidden": hidden}
  db.commit()
  for visible in (True, False):
    presentation_events.clear()
    response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
      "owner_visible": visible,
    })
    assert response.status_code == 200, response.text
    assert presentation_events == []


@pytest.mark.parametrize("rejected", [
  {"model": ""}, {"title": "x" * 501}, {"system_prompt": "Changed"},
])
def test_rejected_app_presentation_changes_emit_nothing(
  client, owner_token, db, presentation_events, rejected,
):
  _, token = _make_app(client, owner_token, "rejected-presentation")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={}).json()["id"]
  row = db.get(models.Chat, chat_id)
  row.has_messages = True
  db.commit()
  presentation_events.clear()
  response = client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
    "title": "Changed", "owner_visible": True, **rejected,
  })
  assert response.status_code in (409, 422), response.text
  db.refresh(row)
  assert row.title == "New chat"
  assert not row.title_locked
  assert not row.agent_settings_json.get("owner_visible")
  assert presentation_events == []


def test_app_presentation_commit_failure_emits_nothing(
  client, owner_token, db, monkeypatch, presentation_events,
):
  from sqlalchemy.orm import Session

  _, token = _make_app(client, owner_token, "rollback-presentation")
  auth = {"Authorization": f"Bearer {token}"}
  chat_id = client.post("/api/app-chats", headers=auth, json={}).json()["id"]
  presentation_events.clear()

  def fail_commit(session):
    session.flush()
    session.rollback()
    raise RuntimeError("simulated commit failure")

  with monkeypatch.context() as patch:
    patch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
      client.patch(f"/api/app-chats/{chat_id}", headers=auth, json={
        "title": "Must roll back", "owner_visible": True,
      })
  row = db.get(models.Chat, chat_id)
  assert row.title == "New chat"
  assert not row.title_locked
  assert not row.agent_settings_json.get("owner_visible")
  assert presentation_events == []
