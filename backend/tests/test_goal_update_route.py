"""The single agent-facing Goal operation behind the update_goal tool."""

from datetime import UTC, datetime, timedelta

import pytest

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


def test_identical_task_edit_does_not_manufacture_a_revision(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  seeded = _seed_plan(client, db, chat_id)
  repeat = _update(client, db, chat_id, {"tasks": [{"id": "inspect", "title": "Inspect"}]})
  assert repeat.status_code == 200, repeat.text
  assert repeat.json()["goal"]["revision"] == seeded["goal"]["revision"]


def test_completion_refusal_rolls_back_task_edits_in_the_same_call(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)

  refused = _update(client, db, chat_id, {
    "tasks": [{"id": "inspect", "status": "completed", "result": "Done"}],
    "complete": True,
  })

  assert refused.status_code == 422
  assert "Task edits were saved" not in refused.json()["detail"]["message"]
  assert refused.json()["detail"]["code"] == "goal_completion_blocked"
  assert refused.json()["detail"]["completion_blockers"] == ["build"]
  assert "build" in refused.json()["detail"]["message"]
  db.expire_all()
  goal = db.get(models.ChatGoal, "goal-1")
  assert goal.status == "open"
  tasks = {task["id"]: task for task in goal.plan_json["tasks"]}
  assert tasks["inspect"]["status"] == "pending"


def test_cannot_complete_settles_reasoned_unmet_tasks_without_shrinking_objective(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  before = db.get(models.ChatGoal, "goal-1").revision
  response = _update(client, db, chat_id, {
    "tasks": [
      {"id": "inspect", "status": "failed", "note": "Vendor API permanently unavailable"},
      {"id": "build", "status": "blocked", "note": "Needs the vendor API"},
    ],
    "cannot_complete": {
      "reason": "Vendor removed the API and owner has no replacement",
      "efforts": "Inspected integration; preserved current build",
      "unmet_outcome": "The original live integration is not delivered",
    },
  })
  assert response.status_code == 200, response.text
  goal = response.json()["goal"]
  assert goal["status"] == "cannot_complete"
  assert goal["revision"] == before + 1
  db.expire_all()
  saved = db.get(models.ChatGoal, "goal-1")
  assert saved.objective != "The original live integration is not delivered"
  assert "Unmet outcome:" in saved.result


def test_cannot_complete_refuses_unexplained_or_pending_tasks_atomically(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  original = db.get(models.ChatGoal, "goal-1").revision
  outcome = {"reason": "No access", "efforts": "Tried", "unmet_outcome": "Not shipped"}
  refused = _update(client, db, chat_id, {
    "tasks": [{"id": "inspect", "status": "failed"}],
    "cannot_complete": outcome,
  })
  assert refused.status_code == 422
  db.expire_all()
  saved = db.get(models.ChatGoal, "goal-1")
  assert saved.revision == original
  assert saved.plan_json["tasks"][0]["status"] == "pending"


def test_cancel_requires_reasoned_settled_checklist(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  refused = _update(client, db, chat_id, {"cancel": "Owner called off this goal"})
  assert refused.status_code == 422
  ended = _update(client, db, chat_id, {
    "tasks": [
      {"id": "inspect", "status": "cancelled", "note": "Owner called off"},
      {"id": "build", "status": "cancelled", "note": "Owner called off"},
    ],
    "cancel": "Owner called off this goal",
  })
  assert ended.status_code == 200, ended.text
  assert ended.json()["goal"]["status"] == "cancelled"


def test_cannot_complete_retry_is_idempotent_at_record_boundary(
  client, owner_token, db,
):
  from app.goals import update_goal_record

  _, chat_id = _active_goal(client, owner_token, db)
  run = db.get(models.ChatRun, "goal-root")
  goal = db.get(models.ChatGoal, "goal-1")
  outcome = {"reason": "No source", "efforts": "Looked", "unmet_outcome": "Not shipped"}
  first = update_goal_record(db, run, goal, goal.revision,
                             cannot_complete=outcome)
  again = update_goal_record(db, run, goal, first["revision"] - 1,
                             cannot_complete=outcome)
  assert again == first


def test_outcome_tool_receipt_retry_is_idempotent_but_cannot_edit_settled_work(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  body = {
    "tasks": [
      {"id": "inspect", "status": "completed", "result": "Checked"},
      {"id": "build", "status": "completed", "result": "Verified"},
    ],
    "complete": True,
  }
  first = _update(client, db, chat_id, body)
  assert first.status_code == 200, first.text
  again = _update(client, db, chat_id, body)
  assert again.status_code == 200, again.text
  assert again.json()["goal"] == first.json()["goal"]
  changed = _update(client, db, chat_id, {
    **body, "tasks": [{"id": "build", "result": "Different claim"}],
  })
  assert changed.status_code == 409
  opposite = _update(client, db, chat_id, {"cancel": "Actually cancelled"})
  assert opposite.status_code == 409
  guessed = _update(client, db, chat_id, {**body, "finished_claims": ["not-performed"]})
  assert guessed.status_code == 422
  assert _update(client, db, chat_id, {}).json()["goal"] == first.json()["goal"]


def test_goal_returns_exact_held_work_keys_and_completion_settles_only_named_work(
  client, owner_token, db,
):
  from app.agent_work_claims import claim_work

  _, chat_id = _active_goal(client, owner_token, db)
  owner_id = db.query(models.Owner.id).scalar()
  for key in ("test:performed", "test:unneeded", "test:another-goal"):
    claim_work(db, owner_id=owner_id, chat_id=chat_id, run_id="goal-root",
               work_key=key, summary=key)
  other = db.query(models.AgentWorkClaim).filter_by(work_key="test:another-goal").one()
  other.owner_goal_id = "retained-goal"
  db.commit()
  read = _update(client, db, chat_id, {})
  assert read.status_code == 200, read.text
  assert read.json()["goal"]["held_work_keys"] == ["test:performed", "test:unneeded"]

  refused = _update(client, db, chat_id, {
    "complete": True, "finished_claims": ["test:guessed"],
  })
  assert refused.status_code == 422
  finished = _update(client, db, chat_id, {
    "complete": True, "finished_claims": ["test:performed"],
  })
  assert finished.status_code == 200, finished.text
  assert finished.json()["goal"]["held_work_keys"] == []
  claims = {row.work_key: row for row in db.query(models.AgentWorkClaim).all()}
  assert claims["test:performed"].completed_at is not None
  assert claims["test:unneeded"].released_at is not None
  assert claims["test:unneeded"].completed_at is None
  assert claims["test:another-goal"].completed_at is None
  assert claims["test:another-goal"].released_at is None




def test_the_final_task_edit_and_completion_can_share_one_call(
  client, owner_token, db,
):
  _, chat_id = _active_goal(client, owner_token, db)
  _update(client, db, chat_id, {"tasks": [{"id": "only", "title": "Only step"}]})

  completed = _update(client, db, chat_id, {
    "tasks": [{"id": "only", "status": "completed", "result": "Shipped"}],
    "complete": True,
  })

  assert completed.status_code == 200, completed.text
  assert completed.json()["goal"]["status"] == "completed"
  db.expire_all()
  assert db.get(models.ChatGoal, "goal-1").result is None
  assert completed.json()["plan"]["tasks"][0]["result"] == "Shipped"


@pytest.mark.parametrize("value", [False, 0, 1, "", " ", [], {}, "x" * 4001])
def test_completion_flag_rejects_accidental_coercion(client, owner_token, db, value):
  _, chat_id = _active_goal(client, owner_token, db)
  response = _update(client, db, chat_id, {"complete": value})
  assert response.status_code == 422
  db.expire_all()
  assert db.get(models.ChatGoal, "goal-1").status == "open"


def test_legacy_string_completion_remains_accepted_by_route(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  response = _update(client, db, chat_id, {"complete": "Original verified result"})
  assert response.status_code == 200, response.text
  db.refresh(db.get(models.ChatGoal, "goal-1"))
  assert db.get(models.ChatGoal, "goal-1").result == "Original verified result"


def test_completing_through_the_route_withdraws_its_fired_waits_resume(
  client, owner_token, db,
):
  """This route settles a completion exactly as patch_goal_record does: a
  verified completion takes delivery of its fired Wait, so the queued resume
  that would otherwise wake the finished Goal is withdrawn while the owner's
  own follow-up stays. (Mirrors PR #1450's patch_goal_record settlement.)"""
  from app import chat_waits

  _, chat_id = _active_goal(client, owner_token, db)
  _update(client, db, chat_id, {"tasks": [{"id": "only", "title": "Only step"}]})
  wait = chat_waits.declare_wait(
    db, chat_id=chat_id, created_by_run_id="goal-root",
    description="Checks finishing", kind="timer", delay_secs=60,
  )
  wait.status = "met"
  chat = db.get(models.Chat, chat_id)
  chat.pending_messages = [{
    "role": "user", "content": "A wait you declared has completed.",
    "ts": 1, "cid": f"wait-result-{wait.id}", "hidden": True,
    "kind": "wait_result", "source_work_id": "goal-root",
  }, {"role": "user", "content": "Owner follow-up", "ts": 2, "cid": "owner-1"}]
  db.commit()

  completed = _update(client, db, chat_id, {
    "tasks": [{"id": "only", "status": "completed", "result": "Shipped"}],
    "complete": True,
  })

  assert completed.status_code == 200, completed.text
  assert completed.json()["goal"]["status"] == "completed"
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is not None
  assert [m["cid"] for m in db.get(models.Chat, chat_id).pending_messages] == [
    "owner-1",
  ]


def test_next_action_leaves_a_handoff_checkpoint(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)

  saved = _update(client, db, chat_id, {"next_action": "Run the live check"})

  assert saved.status_code == 200, saved.text
  assert saved.json()["goal"]["next_action"] == "Run the live check"
  both = _update(client, db, chat_id, {"next_action": "x", "complete": True})
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


def test_a_task_note_may_run_to_a_thousand_characters(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)

  kept = _update(client, db, chat_id, {"tasks": [{"id": "inspect", "note": "n" * 1000}]})
  extended = _update(client, db, chat_id, {"tasks": [{"id": "inspect", "note": "n" * 1001}]})

  assert kept.status_code == 200, kept.text
  assert extended.status_code == 422, extended.text
  assert "at most 1000 characters" in extended.json()["detail"]["message"]
