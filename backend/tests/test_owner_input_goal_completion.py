"""Which open owner requests hold a Goal's completion, and what completion
does to the claim of an approval the chat still asks."""
import pytest

from app import models, owner_input_requests as requests
from app.database import SessionLocal
from tests import owner_input_fixtures as inputs
from tests.test_goal_plans import _active_goal, _agent_run_auth, _update


TASKS = [{"id": "build", "title": "Build"}, {"id": "deploy", "title": "Deploy"}]
OFFER = {"prompt": "Contribute this fix upstream too?"}


def _planned_goal(client, owner_token, db):
  _auth, chat_id = _active_goal(client, owner_token, db)
  assert _update(client, db, chat_id, {"tasks": TASKS}).status_code == 200
  return chat_id


def _finish_tasks(client, db, chat_id):
  finished = _update(client, db, chat_id, {"tasks": [
    {"id": task["id"], "status": "completed"} for task in TASKS]})
  assert finished.status_code == 200, finished.text


def _goal_status(goal_id="goal-1"):
  with SessionLocal() as session:
    return session.get(models.ChatGoal, goal_id).status


@pytest.mark.parametrize("goal_id, blocks", [
  pytest.param("goal-1", None, id="offer-without-blocks"),
  pytest.param("goal-1", {"work": "Opening the upstream pull request"}, id="work-only"),
  pytest.param("goal-other", {"tasks": ["other-task"]}, id="another-goals"),
  pytest.param(None, {"tasks": ["unrelated"]}, id="no-goal"),
])
def test_an_offer_left_open_does_not_hold_a_finished_goal(
  client, owner_token, db, goal_id, blocks,
):
  chat_id = _planned_goal(client, owner_token, db)
  _finish_tasks(client, db, chat_id)
  requests.add_request(db, chat_id=chat_id, run_id="goal-root", goal_id=goal_id,
                       key="contribute-upstream", spec=OFFER, blocks=blocks)

  completed = _update(client, db, chat_id, {"complete": True})

  assert completed.status_code == 200, completed.text
  assert _goal_status() == "completed"
  with SessionLocal() as session:
    assert [row.key for row in requests.open_requests(session, chat_id)] == [
      "contribute-upstream"]


def test_a_request_fencing_this_goals_task_holds_its_completion(client, owner_token, db):
  chat_id = _planned_goal(client, owner_token, db)
  _finish_tasks(client, db, chat_id)
  requests.add_request(db, chat_id=chat_id, run_id="goal-root", goal_id="goal-1",
                       key="release-target", spec={"prompt": "Staging or production?"},
                       blocks={"tasks": ["deploy"]})

  refused = _update(client, db, chat_id, {"complete": True})

  assert refused.status_code == 422 and "release-target" in refused.text, refused.text
  assert _goal_status() == "open"


def test_a_goal_task_a_request_names_must_exist_in_the_plan(client, owner_token, db):
  chat_id = _planned_goal(client, owner_token, db)
  agent = _agent_run_auth(db, chat_id, "goal-root")

  refused = inputs.add(client, chat_id, agent, "publish", **inputs.APPROVAL,
                       blocks={"tasks": ["deploy-prod"]})
  recorded = inputs.add(client, chat_id, agent, "tone", prompt="Casual or formal?",
                        blocks={"tasks": ["deploy"]})
  revised = inputs.edit(client, chat_id, agent, op="update", key="tone",
                        blocks={"tasks": ["ship"]})

  assert refused.status_code == 422, refused.text
  assert "deploy-prod" in refused.text and "build, deploy" in refused.text
  # Refused before anything was claimed.
  with SessionLocal() as session:
    assert session.query(models.AgentWorkClaim).count() == 0
  assert recorded.status_code == 200, recorded.text
  assert revised.status_code == 422 and "ship" in revised.text


def test_an_approval_still_asked_keeps_its_claim_past_goal_completion(client, owner_token, db):
  chat_id = _planned_goal(client, owner_token, db)
  agent = _agent_run_auth(db, chat_id, "goal-root")
  offered = inputs.add(client, chat_id, agent, "publish", **inputs.APPROVAL)
  assert offered.status_code == 200, offered.text
  work_key = inputs.APPROVAL["work_key"]
  with SessionLocal() as session:
    assert session.query(models.AgentWorkClaim).filter_by(
      work_key=work_key).one().owner_goal_id == "goal-1"
  _finish_tasks(client, db, chat_id)

  completed = _update(client, db, chat_id, {"complete": True})

  assert completed.status_code == 200, completed.text
  from app.agent_work_claims import stage_settle_claims_with_owner

  with SessionLocal() as session:
    # The repair pass after a Goal ends leaves the detached claim alone too.
    stage_settle_claims_with_owner(session, chat_id)
    session.commit()
    claim = session.query(models.AgentWorkClaim).filter_by(work_key=work_key).one()
    assert (claim.owner_chat_id, claim.owner_goal_id) == (chat_id, None)
    assert claim.released_at is None and claim.completed_at is None

  # Withdrawing the approval later releases it.
  with SessionLocal() as session:
    _row, released = requests.withdraw(session, chat_id=chat_id, ref="publish",
                                       reason="The owner chose not to publish.")
  assert released is True
  with SessionLocal() as session:
    assert session.query(models.AgentWorkClaim).filter_by(
      work_key=work_key).one().released_at is not None


def test_an_approval_named_as_finished_completes_with_its_goal(client, owner_token, db):
  chat_id = _planned_goal(client, owner_token, db)
  agent = _agent_run_auth(db, chat_id, "goal-root")
  inputs.add(client, chat_id, agent, "publish", **inputs.APPROVAL)
  _finish_tasks(client, db, chat_id)

  completed = _update(client, db, chat_id, {
    "complete": True, "finished_claims": [inputs.APPROVAL["work_key"]]})

  assert completed.status_code == 200, completed.text
  with SessionLocal() as session:
    claim = session.query(models.AgentWorkClaim).filter_by(
      work_key=inputs.APPROVAL["work_key"]).one()
    assert claim.completed_at is not None
