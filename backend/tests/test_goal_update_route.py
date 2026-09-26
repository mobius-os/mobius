"""The single agent-facing Goal operation behind the update_goal tool."""

from datetime import UTC, datetime, timedelta

from app import models
from tests.goal_fixtures import goal_run as make_goal_run
from tests.test_goal_plans import _active_goal, _agent_run_auth


def _update(client, db, chat_id, body, run_id="goal-root"):
  return client.post(
    f"/api/chats/{chat_id}/goal/update", json=body,
    headers=_agent_run_auth(db, chat_id, run_id),
  )


def _seed_plan(client, db, chat_id):
  response = _update(client, db, chat_id, {"tasks": [
    {"id": "inspect", "title": "Inspect"},
    {"id": "build", "title": "Build", "depends_on": ["inspect"]},
  ]})
  assert response.status_code == 200, response.text
  return response.json()


def test_finishing_one_task_and_starting_its_dependant_is_one_revision(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  seeded = _seed_plan(client, db, chat_id)
  _update(client, db, chat_id, {"tasks": [{"id": "inspect", "status": "running"}]})

  # Listed dependant-first: the whole edit validates once, not in list order.
  advanced = _update(client, db, chat_id, {"tasks": [
    {"id": "build", "status": "running"},
    {"id": "inspect", "status": "completed", "result": "Found the cause"},
  ]})

  assert advanced.status_code == 200, advanced.text
  body = advanced.json()
  assert body["goal"]["revision"] == seeded["goal"]["revision"] + 2
  by_id = {task["id"]: task for task in body["plan"]["tasks"]}
  assert by_id["inspect"]["status"] == "completed"
  assert by_id["inspect"]["result"] == "Found the cause"
  assert by_id["inspect"]["title"] == "Inspect"
  assert by_id["build"]["status"] == "running"


def test_a_new_task_id_is_added_and_an_unknown_field_is_refused(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)

  added = _update(client, db, chat_id, {"tasks": [
    {"id": "edge", "title": "Check edge", "parent_id": "build"},
  ]})
  assert added.status_code == 200, added.text
  assert [task["id"] for task in added.json()["plan"]["tasks"]] == [
    "inspect", "build", "edge",
  ]

  untitled = _update(client, db, chat_id, {"tasks": [{"id": "orphan"}]})
  assert untitled.status_code == 422
  unknown = _update(client, db, chat_id, {"tasks": [{"id": "build", "owner": "x"}]})
  assert unknown.status_code == 422
  assert "owner" in unknown.json()["detail"]["message"]


def test_completion_refusal_says_the_task_edits_in_the_same_call_were_saved(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)

  refused = _update(client, db, chat_id, {
    "tasks": [{"id": "inspect", "status": "completed", "result": "Done"}],
    "complete": "Everything verified",
  })

  assert refused.status_code == 422
  assert refused.json()["detail"]["message"].startswith(
    "Task edits were saved, but",
  )
  db.expire_all()
  goal = db.get(models.ChatGoal, "goal-1")
  assert goal.status == "open"
  tasks = {task["id"]: task for task in goal.plan_json["tasks"]}
  assert tasks["inspect"]["status"] == "completed"


def test_the_final_task_edit_and_completion_can_share_one_call(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _update(client, db, chat_id, {"tasks": [{"id": "only", "title": "Only step"}]})

  completed = _update(client, db, chat_id, {
    "tasks": [{"id": "only", "status": "completed", "result": "Shipped"}],
    "complete": "Release verified live",
  })

  assert completed.status_code == 200, completed.text
  assert completed.json()["goal"]["status"] == "completed"
  db.expire_all()
  assert db.get(models.ChatGoal, "goal-1").result == "Release verified live"


def test_next_action_leaves_a_handoff_checkpoint(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)

  saved = _update(client, db, chat_id, {"next_action": "Run the live check"})

  assert saved.status_code == 200, saved.text
  assert saved.json()["goal"]["next_action"] == "Run the live check"
  both = _update(client, db, chat_id, {"next_action": "x", "complete": "y"})
  assert both.status_code == 422


def _ordinary_turn_after_a_goal_attempt(client, owner_token, db):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Resumed goal"}, headers=owner_auth,
  ).json()["id"]
  now = datetime.now(UTC).replace(tzinfo=None)
  db.add_all([
    make_goal_run(db,
      id="previous-goal-run", root_run_id="previous-goal-run", chat_id=chat_id,
      status="failed", provider="codex", goal_objective="Finish the work",
      goal_id="active-goal", started_at=now - timedelta(minutes=1),
    ),
    make_goal_run(db,
      id="ordinary-run", root_run_id="ordinary-run", chat_id=chat_id,
      status="running", provider="codex", started_at=now,
    ),
  ])
  db.commit()
  return chat_id


def test_a_write_attaches_an_ordinary_turn_to_the_presented_goal(
  client, owner_token, db,
):
  chat_id = _ordinary_turn_after_a_goal_attempt(client, owner_token, db)

  written = _update(
    client, db, chat_id,
    {"tasks": [{"id": "finish", "title": "Finish the work"}]},
    run_id="ordinary-run",
  )

  assert written.status_code == 200, written.text
  assert written.json()["goal"]["id"] == "active-goal"
  db.expire_all()
  assert db.get(models.ChatRun, "ordinary-run").goal_id == "active-goal"


def test_a_read_with_no_fields_does_not_attach_the_turn(client, owner_token, db):
  chat_id = _ordinary_turn_after_a_goal_attempt(client, owner_token, db)

  read = _update(client, db, chat_id, {}, run_id="ordinary-run")

  assert read.status_code == 200, read.text
  assert read.json()["goal"]["id"] == "active-goal"
  db.expire_all()
  assert db.get(models.ChatRun, "ordinary-run").goal_id is None


def test_a_chat_without_a_goal_is_told_to_promote_first(client, owner_token, db):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "No goal"}, headers=owner_auth,
  ).json()["id"]
  db.add(make_goal_run(db,
    id="plain-run", root_run_id="plain-run", chat_id=chat_id,
    status="running", provider="codex",
  ))
  db.commit()

  written = _update(
    client, db, chat_id, {"tasks": [{"id": "a", "title": "A"}]},
    run_id="plain-run",
  )

  assert written.status_code == 409
  assert "Promote one first" in written.json()["detail"]["message"]
