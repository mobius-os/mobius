"""The Recents drawer query now surfaces project chats with a project chip."""

import uuid
from datetime import timedelta

from app import models
from app.timeutil import now_naive_utc


def _make_project(client, auth, name="Alpha"):
  created = client.post(
    "/api/projects", headers=auth, json={"name": name, "template_id": "blank"},
  )
  assert created.status_code == 200, created.text
  return created.json()


def _make_project_chat(client, auth, project, title="c1", request_id="r1"):
  created = client.post(
    f"/api/projects/{project['id']}/chats", headers=auth,
    json={"title": title, "recovery_request_id": request_id},
  )
  assert created.status_code == 201, created.text
  return created.json()


def _recents_by_id(client, auth):
  response = client.get("/api/chats", headers=auth)
  assert response.status_code == 200, response.text
  return {row["id"]: row for row in response.json()}


def test_project_chat_appears_in_recents_with_its_project(client, auth):
  project = _make_project(client, auth, "Alpha")
  chat = _make_project_chat(client, auth, project)
  rows = _recents_by_id(client, auth)
  assert chat["id"] in rows
  project_ref = rows[chat["id"]]["project"]
  assert project_ref["id"] == project["id"]
  assert project_ref["name"] == "Alpha"
  assert project_ref["color"] is None
  assert project_ref["root_path"].startswith("projects/")


def test_project_color_flows_into_its_chat_recents_chip(client, auth):
  project = _make_project(client, auth, "Colored")
  chat = _make_project_chat(client, auth, project)
  updated = client.patch(
    f"/api/projects/{project['id']}", headers=auth, json={"color": "#3B82F6"},
  )
  assert updated.status_code == 200, updated.text
  assert updated.json()["color"] == "#3b82f6"
  assert _recents_by_id(client, auth)[chat["id"]]["project"]["color"] == "#3b82f6"


def test_project_chat_rename_is_durable_recent_activity(client, auth, db):
  project = _make_project(client, auth, "Rename activity")
  chat = _make_project_chat(client, auth, project)
  row = db.get(models.Chat, chat["id"])
  old_activity = now_naive_utc() - timedelta(days=3)
  row.activity_at = old_activity
  db.commit()

  renamed = client.patch(
    f"/api/chats/{chat['id']}", headers=auth,
    json={"title": "New project chat name"},
  )
  assert renamed.status_code == 200, renamed.text
  db.refresh(row)
  assert row.activity_at > old_activity
  assert _recents_by_id(client, auth)[chat["id"]]["activity_at"] > (
    old_activity.isoformat()
  )


def test_soft_deleted_projects_chat_is_absent_from_recents(client, auth):
  project = _make_project(client, auth, "Doomed")
  chat = _make_project_chat(client, auth, project)
  deleted = client.delete(f"/api/projects/{project['id']}", headers=auth)
  assert deleted.status_code == 204
  rows = _recents_by_id(client, auth)
  assert chat["id"] not in rows


def test_legacy_primary_chat_shows_with_project_chip(client, auth, db):
  # A pre-migration project keeps its primary chat via Project.chat_id, with the
  # chat's own project_id still NULL. The drawer join's legacy branch must still
  # attach the chip.
  chat = models.Chat(
    id=str(uuid.uuid4()), title="Legacy primary", messages=[],
  )
  db.add(chat)
  db.commit()
  project = models.Project(
    id=str(uuid.uuid4()),
    name="LegacyProj",
    project_type="blank",
    root_path=f"projects/{uuid.uuid4()}",
    chat_id=chat.id,
    template_snapshot_json={},
  )
  db.add(project)
  db.commit()

  rows = _recents_by_id(client, auth)
  assert chat.id in rows
  assert rows[chat.id]["project"] == {
    "id": project.id, "name": "LegacyProj", "root_path": project.root_path,
    "color": None,
  }


def test_ordinary_chat_has_no_project_chip(client, auth, db):
  chat = models.Chat(id=str(uuid.uuid4()), title="Plain chat", messages=[])
  db.add(chat)
  db.commit()
  rows = _recents_by_id(client, auth)
  assert chat.id in rows
  assert rows[chat.id]["project"] is None
