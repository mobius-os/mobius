"""Apps edited by a chat remain durable artifacts until explicitly acknowledged."""

from datetime import timedelta

from app import chat_app_artifacts, models


def _chat(db, chat_id):
  chat = models.Chat(id=chat_id, title=chat_id)
  db.add(chat)
  return chat


def _app(db, chat_id):
  app = models.App(
    slug="test-chat-app-artifact-9",
    source_dir="/tmp/mobius-tests/test-chat-app-artifact-9",
    name="Atlas",
    description="",
    chat_id=chat_id,
    jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.flush()
  return app


def _listed(client, auth, chat_id):
  response = client.get(f"/api/apps/chat-artifacts/{chat_id}", headers=auth)
  assert response.status_code == 200, response.text
  return response.json()


def test_one_app_remains_related_to_every_chat_that_updated_it(client, auth, db):
  _chat(db, "chat-a")
  _chat(db, "chat-b")
  app = _app(db, "chat-a")
  first_touch = app.updated_at
  chat_app_artifacts.record_touch(
    db, chat_id="chat-a", app_id=app.id, touched_at=first_touch,
  )
  second_touch = first_touch + timedelta(seconds=1)
  chat_app_artifacts.record_touch(
    db, chat_id="chat-b", app_id=app.id, touched_at=second_touch,
  )
  db.commit()

  assert _listed(client, auth, "chat-a")[0]["app"]["id"] == app.id
  assert _listed(client, auth, "chat-b")[0]["app"]["id"] == app.id


def test_opening_the_actual_app_does_not_acknowledge_its_chat_update(
  client, auth, db,
):
  _chat(db, "chat-a")
  _chat(db, "chat-b")
  app = _app(db, "chat-a")
  touched_at = app.updated_at
  for chat_id in ("chat-a", "chat-b"):
    chat_app_artifacts.record_touch(
      db, chat_id=chat_id, app_id=app.id, touched_at=touched_at,
    )
  db.commit()

  assert _listed(client, auth, "chat-a")[0]["seen_at"] is None
  assert _listed(client, auth, "chat-b")[0]["seen_at"] is None

  opened = client.post(f"/api/apps/{app.id}/opened", headers=auth)
  assert opened.status_code == 204, opened.text
  assert _listed(client, auth, "chat-a")[0]["seen_at"] is None
  assert _listed(client, auth, "chat-b")[0]["seen_at"] is None


def test_opening_brain_acknowledges_exact_visible_touches_only(
  client, auth, db,
):
  _chat(db, "chat-a")
  _chat(db, "chat-b")
  app = _app(db, "chat-a")
  first_touch = app.updated_at
  for chat_id in ("chat-a", "chat-b"):
    chat_app_artifacts.record_touch(
      db, chat_id=chat_id, app_id=app.id, touched_at=first_touch,
    )
  db.commit()

  acknowledged = client.post(
    "/api/apps/chat-artifacts/chat-a/seen",
    headers=auth,
    json={"touches": [{"app_id": app.id, "touched_at": first_touch.isoformat()}]},
  )
  assert acknowledged.status_code == 204, acknowledged.text
  assert _listed(client, auth, "chat-a")[0]["seen_at"] == (
    _listed(client, auth, "chat-a")[0]["touched_at"]
  )
  assert _listed(client, auth, "chat-b")[0]["seen_at"] is None

  next_touch = first_touch + timedelta(seconds=2)
  chat_app_artifacts.record_touch(
    db, chat_id="chat-a", app_id=app.id, touched_at=next_touch,
  )
  db.commit()
  stale_open = client.post(
    "/api/apps/chat-artifacts/chat-a/seen",
    headers=auth,
    json={"touches": [{"app_id": app.id, "touched_at": first_touch.isoformat()}]},
  )
  assert stale_open.status_code == 204, stale_open.text
  assert _listed(client, auth, "chat-a")[0]["seen_at"] != (
    _listed(client, auth, "chat-a")[0]["touched_at"]
  )


def test_missing_chat_artifact_list_is_not_a_global_app_listing(client, auth):
  response = client.get("/api/apps/chat-artifacts/missing-chat", headers=auth)
  assert response.status_code == 404
