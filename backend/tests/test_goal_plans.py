"""Durable Goal-plan validation, ordering, progress, and route contracts."""

from app import models


def _active_goal(client, owner_token, db):
  auth = {"Authorization": f"Bearer {owner_token}"}
  response = client.post("/api/chats", json={"title": "Planned goal"}, headers=auth)
  assert response.status_code == 200, response.text
  chat_id = response.json()["id"]
  db.add(models.ChatRun(
    id="goal-root",
    root_run_id="goal-root",
    chat_id=chat_id,
    status="running",
    provider="codex",
    goal_objective="Ship the release",
  ))
  db.commit()
  return auth, chat_id


def test_parallel_roots_release_dependent_task_only_after_all_complete(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  tasks = [
    {"id": "a", "title": "Run A", "depends_on": []},
    {"id": "b", "title": "Run B", "depends_on": []},
    {"id": "c", "title": "Run C", "depends_on": ["a", "b"]},
  ]
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": tasks}, headers=auth,
  )
  assert created.status_code == 200, created.text
  plan = created.json()["plan"]
  assert plan["revision"] == 1
  assert plan["summary"] == {
    "completed": 0, "total": 3, "running": [], "ready": ["a", "b"],
  }

  a_running = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/a",
    json={"expected_revision": 1, "status": "running"}, headers=auth,
  )
  assert a_running.status_code == 200, a_running.text
  assert a_running.json()["plan"]["summary"]["running"] == ["a"]

  premature = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/c",
    json={"expected_revision": 2, "status": "running"}, headers=auth,
  )
  assert premature.status_code == 422
  assert "dependencies complete" in premature.json()["detail"]

  revision = 2
  for task_id, status in (
    ("a", "completed"),
    ("b", "running"),
    ("b", "completed"),
    ("c", "running"),
  ):
    response = client.patch(
      f"/api/chats/{chat_id}/goal-plan/tasks/{task_id}",
      json={"expected_revision": revision, "status": status}, headers=auth,
    )
    assert response.status_code == 200, response.text
    revision += 1
  final = response.json()["plan"]
  assert final["summary"]["running"] == ["c"]
  assert final["summary"]["completed"] == 2


def test_repeated_task_needs_full_progress_and_stale_revision_cannot_overwrite(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [{
        "id": "repeat",
        "title": "Run the audit three times",
        "status": "running",
        "depends_on": [],
        "progress": {"current": 0, "total": 3},
      }],
    },
    headers=auth,
  )
  assert created.status_code == 200, created.text

  partial = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 1, "progress": {"current": 2, "total": 3}},
    headers=auth,
  )
  assert partial.status_code == 200, partial.text
  not_done = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 2, "status": "completed"}, headers=auth,
  )
  assert not_done.status_code == 422
  assert "repeated progress is full" in not_done.json()["detail"]

  stale = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 1, "note": "stale writer"}, headers=auth,
  )
  assert stale.status_code == 409

  full = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 2, "progress": {"current": 3, "total": 3}},
    headers=auth,
  )
  assert full.status_code == 200, full.text
  completed = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 3, "status": "completed"}, headers=auth,
  )
  assert completed.status_code == 200, completed.text
  assert completed.json()["plan"]["summary"]["completed"] == 1


def test_plan_rejects_cycles_missing_dependencies_and_non_goal_runs(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  cycle = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [
        {"id": "a", "title": "A", "depends_on": ["b"]},
        {"id": "b", "title": "B", "depends_on": ["a"]},
      ],
    }, headers=auth,
  )
  assert cycle.status_code == 422
  assert "cycle" in cycle.json()["detail"]

  missing = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [{"id": "a", "title": "A", "depends_on": ["gone"]}],
    }, headers=auth,
  )
  assert missing.status_code == 422
  assert "missing task" in missing.json()["detail"]

  db.query(models.ChatRun).filter(models.ChatRun.id == "goal-root").update({
    models.ChatRun.status: "completed",
  })
  db.commit()
  inactive = client.get(f"/api/chats/{chat_id}/goal-plan", headers=auth)
  assert inactive.status_code == 200
  assert inactive.json() == {"plan": None}
  rejected = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [{"id": "a", "title": "A"}]},
    headers=auth,
  )
  assert rejected.status_code == 409
