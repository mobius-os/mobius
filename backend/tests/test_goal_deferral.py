"""Deferral is deliberate unfinished work, not abandonment or a false failure."""
from copy import deepcopy

import pytest

from app import models
from app.chat_writer import create_chat
from app import transcript_rows
from app.goals import goal_hold, update_goal_record
from app.goal_plans import GoalPlanConflict, presented_goal
from app.chat_writer import FinishRun, StartTurn, StartTurnRecoveryChanged
from tests.test_goal_plans import _active_goal
from tests.test_goal_update_route import _update, _seed_plan
from tests.test_goal_hold_lifecycle import seed, resume, submit
from tests.test_owner_approvals import approval_run  # noqa: F401 — shared live-card fixture


REASON = "Owner deferred provider tests; the reviewed implementation stays disabled."


def test_deferred_step_does_not_hold_other_authorized_work(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  response = _update(client, db, chat_id, {"tasks": [
    {"id": "paid", "title": "Paid tests", "status": "blocked", "note": "Owner chose Not now"},
    {"id": "docs", "title": "Finish independent documentation", "status": "running"},
  ], "next_action": "Finish the authorized documentation without running paid tests"})
  assert response.status_code == 200, response.text
  assert response.json()["goal"]["status"] == "open"
  assert response.json()["plan"]["summary"]["running"] == ["docs"]
  deferred = _update(client, db, chat_id, {
    "tasks": [{"id": "docs", "status": "completed", "result": "Documentation verified"}],
    "defer": REASON,
  })
  assert deferred.status_code == 200, deferred.text
  db.expire_all()
  goal = db.get(models.ChatGoal, "goal-1")
  assert goal.status == "stopped" and goal.completed_at is None and goal.result is None
  assert goal.objective == "Ship the release" and goal.next_action is None
  assert goal_hold(goal)["cause"] == "deferred" and goal_hold(goal)["reason"] == REASON
  shown = presented_goal(db, chat_id)
  assert shown["pause_reason"] == "deferred" and shown["hold_reason"] == REASON
  assert shown["handoff"] == {"kind": "none", "reason": None}
  assert shown["resumable"] and shown["revision"] == goal.revision
  assert db.get(models.ChatRun, "goal-root").status == "running"  # Does not interrupt closeout.


def test_defer_receipt_replay_is_exact_and_cannot_reopen_or_change_the_plan(client, owner_token, db):
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  body = {"defer": REASON, "tasks": [{"id": "inspect", "status": "blocked", "note": "Deferred"}]}
  first = _update(client, db, chat_id, body)
  assert first.status_code == 200, first.text
  for retry in (body, {**body, "goal_id": "goal-1"}):
    again = _update(client, db, chat_id, retry)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
  for changes in ({"defer": "Different reason"}, {"tasks": [{"id": "inspect", "note": "Changed"}]}):
    refused = _update(client, db, chat_id, {**body, **changes})
    assert refused.status_code == 409, refused.text
  assert _update(client, db, chat_id, {"next_action": "Implicitly retry"}).status_code == 409
  assert _update(client, db, chat_id, {"goal_id": "goal-1", "next_action": "Still same attempt"}).status_code == 409
  assert _update(client, db, chat_id, {}).json() == first.json()


@pytest.mark.parametrize("extra", [{"complete": "Done"}, {"cancel": "Cancelled"},
  {"cannot_complete": {"reason": "No", "efforts": "Tried", "unmet_outcome": "Not shipped"}},
  {"next_action": "Retry"}, {"finished_claims": ["claimed-success"]}])
def test_defer_cannot_disguise_an_outcome_or_retry(client, owner_token, db, extra):
  _, chat_id = _active_goal(client, owner_token, db)
  response = _update(client, db, chat_id, {"defer": REASON, **extra})
  assert response.status_code == 422, response.text
  db.expire_all()
  assert db.get(models.ChatGoal, "goal-1").status == "open"


@pytest.mark.parametrize("reason", ["", "   "])
def test_defer_requires_a_nonblank_reason(client, owner_token, db, reason):
  _, chat_id = _active_goal(client, owner_token, db)
  response = _update(client, db, chat_id, {"defer": reason})
  assert response.status_code == 422, response.text


@pytest.mark.parametrize("blocker", ["armed", "met", "helper", "card"])
def test_defer_never_abandons_a_handoff_and_refusal_is_atomic(client, owner_token, db, monkeypatch, blocker):
  from app import chat_waits, goal_plans
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  if blocker in {"armed", "met"}:
    chat_waits.declare_wait(db, chat_id=chat_id, created_by_run_id="goal-root",
      description="Existing automatic work", kind="timer", delay_secs=60)
    wait = db.query(models.ChatWait).filter_by(chat_id=chat_id).one()
    wait.status = blocker
    db.commit()
  elif blocker == "helper":
    monkeypatch.setattr(goal_plans, "active_goal_helpers", lambda *args: ["live-helper"])
  else:
    db.get(models.Chat, chat_id).pending_question_id = "existing-card"
    db.commit()
  goal = db.get(models.ChatGoal, "goal-1")
  before = deepcopy(goal.plan_json), goal.revision
  response = _update(client, db, chat_id, {"defer": REASON,
    "tasks": [{"id": "inspect", "status": "completed", "result": "Checked"}]})
  assert response.status_code == 422, response.text
  assert response.json()["detail"]["code"] == "goal_deferral_blocked"
  db.expire_all()
  assert goal.status == "open" and goal.hold_json is None
  assert (goal.plan_json, goal.revision) == before
  if blocker in {"armed", "met"}:
    assert wait.status == blocker and wait.resume_delivered_at is None


def test_defer_releases_only_exact_goal_claims_without_calling_them_complete(client, owner_token, db):
  from app.agent_work_claims import claim_work
  _, chat_id = _active_goal(client, owner_token, db)
  for key in ("this-goal", "other-goal"):
    claim_work(db, owner_id=db.query(models.Owner.id).scalar(), chat_id=chat_id,
               run_id="goal-root", work_key=key, summary=key)
  other = db.query(models.AgentWorkClaim).filter_by(work_key="other-goal").one()
  other.owner_goal_id = "other"
  db.commit()
  response = _update(client, db, chat_id, {"defer": REASON})
  assert response.status_code == 200, response.text
  db.expire_all()
  own = db.query(models.AgentWorkClaim).filter_by(work_key="this-goal").one()
  assert own.released_at is not None and own.completed_at is None
  assert other.released_at is None and other.completed_at is None


def test_stale_deferral_leaves_goal_and_checklist_unchanged(db, chat):
  goal, run = seed(db, chat)
  before = deepcopy(goal.plan_json)
  with pytest.raises(GoalPlanConflict):
    update_goal_record(db, run, goal, goal.revision - 1, defer=REASON,
      tasks=[{"id": "verify", "note": "Should roll back"}])
  db.expire_all()
  assert goal.status == "open" and goal.hold_json is None and goal.plan_json == before


def test_deferral_does_not_drop_an_owed_helper_result_from_an_earlier_attempt(db, chat, monkeypatch):
  from types import SimpleNamespace
  from app import delegations
  from app.goal_plans import GoalPlanError
  goal, run = seed(db, chat)
  db.add(models.ChatRun(id="earlier", root_run_id="earlier-root", chat_id=chat.id,
                       goal_id=goal.id, status="completed"))
  db.commit()
  monkeypatch.setattr(delegations, "_self_resuming_helper_rows", lambda *args: [
    (SimpleNamespace(id="owed-result", parent_root_run_id="earlier-root"), "completed"),
  ])
  with pytest.raises(GoalPlanError, match="helper:owed-result"):
    update_goal_record(db, run, goal, goal.revision, defer=REASON)
  assert goal.status == "open" and goal.hold_json is None


@pytest.mark.parametrize("operation", ["defer", "complete"])
def test_settlement_cannot_hide_an_older_live_helper_behind_a_newer_finished_attempt(db, chat, operation):
  from datetime import UTC, datetime, timedelta
  from app.goal_plans import GoalPlanError, serialize_plan
  goal, run = seed(db, chat)
  now = datetime.now(UTC)
  for label, status, started in [("old", "running", now - timedelta(minutes=1)), ("new", "completed", now)]:
    root = "root-" + label
    db.add(models.ChatRun(id=root, root_run_id=root, chat_id=chat.id, goal_id=goal.id,
      goal_objective=goal.objective, status="completed", started_at=started))
    child = create_chat(id="child-" + label, title=label, messages=[])
    db.add(child)
    db.flush()
    db.add(models.ChatRun(id="child-run-" + label, chat_id=child.id, status=status))
    db.add(models.Delegation(id="helper-" + label, parent_chat_id=chat.id,
      parent_root_run_id=root, child_chat_id=child.id, task_key="verify", goal_task_id="verify",
      provider="codex", scope="read", cwd="/data", prompt_sha256="0" * 64,
      notify_parent_on_complete=False, created_at=started))
  db.commit()
  assert [h["id"] for h in serialize_plan(db, run, goal)["delegations"]] == ["helper-new"]
  with pytest.raises(GoalPlanError, match="verify"):
    update_goal_record(db, run, goal, goal.revision, **{operation: True if operation == "complete" else "Reviewed"})
  assert goal.status == "open" and goal.hold_json is None


def test_deferred_goal_resumes_only_by_exact_owner_continuation(db, chat):
  from app.run_state import goal_identity_for_run_start
  goal, run = seed(db, chat)
  update_goal_record(db, run, goal, goal.revision, defer=REASON)
  submit(FinishRun(chat_id=chat.id, run_token=run.id, terminal_status="completed"))
  assert goal_identity_for_run_start(db, chat.id, {
    "kind": "continuation", "continuation_reason": "restart", "goal_id": goal.id,
  }) == (None, None)
  assert isinstance(submit(resume(chat, goal, revision=goal.revision - 1)), StartTurnRecoveryChanged)
  submit(StartTurn(chat_id=chat.id, run_token="unrelated", owner_input=True,
    user_msg={"role": "user", "content": "What time is it?", "cid": "different"}))
  db.expire_all()
  assert goal.status == "stopped" and db.get(models.ChatRun, "unrelated").goal_id is None
  submit(FinishRun(chat_id=chat.id, run_token="unrelated", terminal_status="completed"))
  submit(resume(chat, goal))
  db.expire_all()
  assert goal.status == "open" and goal.hold_json is None
  assert db.get(models.ChatRun, "resumed").goal_id == goal.id


@pytest.mark.asyncio
async def test_normal_closeout_after_deferral_never_starts_settlement_or_invents_failure(db, chat, monkeypatch):
  from app import chat as runtime, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink
  from tests.test_goal_turn_end import _add_goal_run
  _add_goal_run(db, chat)
  run, goal = db.get(models.ChatRun, "goal-run"), db.get(models.ChatGoal, "goal-run")
  update_goal_record(db, run, goal, goal.revision, defer=REASON,
    tasks=[{"id": "finish", "status": "blocked", "note": "Owner deferred tests"}])
  scheduled = []
  monkeypatch.setattr(runtime, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  monkeypatch.setattr(runtime, "_publish_chat_run_finished", lambda *_: None)
  bc = create_broadcast(chat.id)
  sink = ChatEventSink(bc, chat.id, run_token=run.id)
  sink.publish({"type": "text", "content": "The remaining tests are on hold as requested."})
  try:
    disposition = await runtime._complete_turn(bc=bc, sink=sink, db=db, chat_id=chat.id,
      run_gen=None, provider_id="codex", cost_usd=0, close_browser=False)
  finally:
    remove_broadcast(chat.id)
  assert disposition is not chat_queue.TerminalDisposition.CONTINUATION_PROMOTED
  db.expire_all()
  assert scheduled == [] and db.query(models.ChatRun).filter_by(chat_id=chat.id).count() == 1
  # _complete_turn closes its session; inspect fresh durable rows, not detached objects.
  assert db.get(models.ChatRun, "goal-run").status == "completed"
  assert db.get(models.ChatGoal, "goal-run").status == "stopped"
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_question_id is None and not saved.pending_messages
  assert not [b for m in transcript_rows.history(saved) for b in m.get("blocks", []) if b.get("type") == "error"]


def test_normal_not_now_card_answer_can_defer_without_a_second_question_or_recovery(
    client, chat, auth, db, approval_run, monkeypatch):
  from app import chat as runtime
  from app.chat_writer import PromoteRunToGoal
  from app.chat_event_sink import ChatEventSink
  from app.broadcast import create_broadcast
  from app.routes import chats_stream
  from tests.test_owner_approvals import _ask, _answer, _finish
  original = approval_run[0].run_token
  submit(PromoteRunToGoal(chat_id=chat.id, run_token=original, objective="Verify the paid test"))
  qid = _ask(client, chat, approval_run).json()["question_id"]
  _finish(chat, approval_run[0])
  scheduled = []
  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  response = _answer(client, chat, auth, qid)
  assert response.status_code == 202, response.text
  db.expire_all()
  successor = db.query(models.ChatRun).filter_by(chat_id=chat.id, status="running").one()
  assert successor.goal_id == original and len(scheduled) == 1
  # The normal card answer woke an agent. It can finish authorized independent
  # work, then record its reasoned deferral; the option label performs no hold.
  assert db.get(models.ChatGoal, original).status == "open"
  deferred = _update(client, db, chat.id, {"defer": REASON}, run_id=successor.id)
  assert deferred.status_code == 200, deferred.text
  db.expire_all()
  successor.provider_execution_admitted = True
  db.commit()
  monkeypatch.setattr(runtime, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  sink = ChatEventSink(create_broadcast(chat.id), chat.id, run_token=successor.id)
  sink.publish({"type": "text", "content": "The tests remain deferred; nothing will retry automatically."})
  _finish(chat, sink)
  db.expire_all()
  assert successor.status == "completed" and len(scheduled) == 1
  assert db.get(models.ChatGoal, original).status == "stopped"
  assert chat.pending_question_id is None
  blocks = [b for m in transcript_rows.history(chat) for b in m.get("blocks", [])]
  assert len([b for b in blocks if b.get("type") == "question"]) == 1
  assert not [b for b in blocks if b.get("type") == "error"]


def test_claim_settlement_refusal_rolls_back_hold_and_tasks_together(client, owner_token, db, monkeypatch):
  from app import agent_work_claims
  from app.goal_plans import GoalPlanError
  _, chat_id = _active_goal(client, owner_token, db)
  _seed_plan(client, db, chat_id)
  def refuse(*args, **kwargs):
    raise GoalPlanError("Injected claim settlement refusal")
  monkeypatch.setattr(agent_work_claims, "stage_settle_goal_claims", refuse)
  response = _update(client, db, chat_id, {"defer": REASON,
    "tasks": [{"id": "inspect", "status": "completed", "result": "Checked"}]})
  assert response.status_code == 422
  db.expire_all()
  goal = db.get(models.ChatGoal, "goal-1")
  assert goal.status == "open" and goal.hold_json is None
  assert goal.plan_json["tasks"][0]["status"] == "pending"
