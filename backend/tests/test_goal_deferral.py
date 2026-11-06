"""Deferral is deliberate unfinished work, not abandonment or a false failure."""
from datetime import UTC, datetime
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
    monkeypatch.setattr(goal_plans, "_delegation_tree", lambda *args, **kwargs: [
      {"id": "live", "task_key": "live-helper", "status": "running", "children": []},
    ])
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
    (SimpleNamespace(id="owed-result", parent_root_run_id="earlier-root", goal_id=goal.id,
                     app_id=None, parent_chat_id=chat.id), "completed"),
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
      parent_root_run_id=root, goal_id=goal.id, child_chat_id=child.id, task_key="verify", goal_task_id="verify",
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
  promoted = submit(PromoteRunToGoal(chat_id=chat.id, run_token=original, objective="Verify the paid test"))
  goal_id = promoted["goal_id"]
  qid = _ask(client, chat, approval_run).json()["question_id"]
  _finish(chat, approval_run[0])
  scheduled = []
  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  response = _answer(client, chat, auth, qid)
  assert response.status_code == 202, response.text
  db.expire_all()
  successor = db.query(models.ChatRun).filter_by(chat_id=chat.id, status="running").one()
  assert successor.goal_id == goal_id and len(scheduled) == 1
  # The normal card answer woke an agent. It can finish authorized independent
  # work, then record its reasoned deferral; the option label performs no hold.
  assert db.get(models.ChatGoal, goal_id).status == "open"
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
  assert db.get(models.ChatGoal, goal_id).status == "stopped"
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


def test_completion_waits_for_a_failed_helper_result_its_agent_has_not_received(db, chat):
  """A helper failure that lands just before settlement stays the agent's to
  handle: completion is refused until the agent reads it, so the failure can
  never be stranded behind a closed Goal or turned into owner work."""
  from app.delegations import record_result_read_by_parent
  from app.goal_plans import GoalPlanError
  goal, run = seed(db, chat)
  child = create_chat(id="failed-helper-chat", title="Helper", messages=[])
  db.add(child)
  db.flush()
  db.add(models.ChatRun(id="failed-helper-run", chat_id=child.id, status="failed",
                       provider="claude", ended_at=datetime.now(UTC)))
  db.add(models.Delegation(id="failed-helper", parent_chat_id=chat.id,
    parent_root_run_id=run.id, goal_id=goal.id, child_chat_id=child.id,
    task_key="cleanup", provider="claude", scope="write", cwd="/data",
    prompt_sha256="0" * 64, notify_parent_on_complete=True))
  db.commit()

  with pytest.raises(GoalPlanError, match="helper:failed-helper") as refused:
    update_goal_record(db, run, goal, goal.revision, complete=True)
  assert "list_agents" in str(refused.value)
  db.expire_all()
  assert goal.status == "open"

  assert record_result_read_by_parent(db, db.get(models.Delegation, "failed-helper"))
  db.commit()
  update_goal_record(db, run, goal, goal.revision, complete=True)
  db.expire_all()
  assert goal.status == "completed"


def _helper(db, *, id, parent_chat_id, goal, run, status, app_id=None, delivered=False):
  child = create_chat(id=id + "-chat", title=id, messages=[])
  db.add(child)
  db.flush()
  db.add(models.ChatRun(id=id + "-run", chat_id=child.id, status=status,
                       provider="claude", ended_at=datetime.now(UTC)))
  db.add(models.Delegation(id=id, parent_chat_id=parent_chat_id, app_id=app_id,
    parent_root_run_id=run.id, goal_id=goal.id, child_chat_id=child.id,
    task_key=id, provider="claude", scope="write", cwd="/data",
    prompt_sha256="0" * 64, notify_parent_on_complete=True,
    delivered_run_id=id + "-run" if delivered else None))
  db.flush()
  return child


def test_completion_waits_for_a_nested_failure_owed_to_a_finished_mid_level_helper(db, chat):
  """A sub-helper that fails after its own parent helper finished still owes
  that helper a wake; closing the Goal first would strand it."""
  from app.goal_plans import GoalPlanError
  goal, run = seed(db, chat)
  middle = _helper(db, id="middle", parent_chat_id=chat.id, goal=goal, run=run,
                   status="completed", delivered=True)
  _helper(db, id="nested", parent_chat_id=middle.id, goal=goal, run=run, status="failed")
  db.commit()
  with pytest.raises(GoalPlanError, match="helper:nested"):
    update_goal_record(db, run, goal, goal.revision, complete=True)
  db.expire_all()
  assert goal.status == "open"


def test_app_owned_helper_results_never_hold_a_chat_goal_open(db, chat):
  goal, run = seed(db, chat)
  app = models.App(slug="owned-helper-app", source_dir="/tmp/mobius-tests/owned-helper-app",
                   name="Owner app", description="", jsx_source="")
  db.add(app)
  db.flush()
  _helper(db, id="app-helper", parent_chat_id=chat.id, goal=goal, run=run,
          status="failed", app_id=app.id)
  db.commit()
  update_goal_record(db, run, goal, goal.revision, complete=True)
  db.expire_all()
  assert goal.status == "completed"


def test_a_failed_mid_level_helper_does_not_trap_its_coordinator(db, chat):
  """Only a helper may read its own children's results, and a failed helper
  is never woken again. Its failure already reached the coordinator, so a
  child result it can no longer receive must not block completion forever."""
  goal, run = seed(db, chat)
  middle = _helper(db, id="failed-middle", parent_chat_id=chat.id, goal=goal, run=run,
                   status="failed", delivered=True)
  _helper(db, id="orphaned-nested", parent_chat_id=middle.id, goal=goal, run=run,
          status="failed")
  db.commit()
  update_goal_record(db, run, goal, goal.revision, complete=True)
  db.expire_all()
  assert goal.status == "completed"


def _goal_result(db, chat, run, kind, *, resolved=True):
  from app.chat_waits import declare_wait
  if kind == "wait":
    row = declare_wait(db, chat_id=chat.id, created_by_run_id=run.id,
      description="Exact Goal result", kind="timer", delay_secs=60)
    row.status = "met" if resolved else "armed"
  else:
    row = models.GoalDependency(id="dependency", waiting_chat_id=chat.id,
      waiting_goal_id=run.goal_id, created_by_run_id=run.id,
      target_kind="claim_acquisition", target_key="claim:acquisition",
      target_claim_id="claim", target_acquisition_id="acquisition",
      resolved_at=datetime.now(UTC) if resolved else None,
      outcome_json={"kind": "completed", "outcome": "Verified"} if resolved else None)
    db.add(row)
  db.commit()
  return row


@pytest.mark.parametrize("kind", ["wait", "dependency"])
@pytest.mark.parametrize("admitted", [False, True])
def test_completion_cannot_retire_a_result_missing_from_its_admitted_prompt(db, chat, kind, admitted):
  from app.goal_plans import GoalPlanError
  from app.chat_writer import AppendPending
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind)
  # A selected-but-unadmitted snapshot is not evidence of provider receipt.
  run.provider_execution_admitted = admitted
  setattr(run, f"admitted_{kind}_results_json", [] if admitted else [row.id])
  db.commit()
  submit(AppendPending(chat_id=chat.id, user_msg={"role": "user", "content": "Result",
    "cid": f"{kind}-result-{row.id}", "kind": f"{kind}_result", "source_work_id": run.id}))
  db.expire_all()
  before = deepcopy(goal.plan_json), goal.revision, deepcopy(db.get(models.Chat, chat.id).pending_messages)
  with pytest.raises(GoalPlanError, match=f"{kind}:" + row.id):
    update_goal_record(db, run, goal, goal.revision, complete=True,
      tasks=[{"id": "verify", "result": "Not committed"}])
  db.expire_all()
  assert goal.status == "open" and row.result_disposition is None
  assert (goal.plan_json, goal.revision, db.get(models.Chat, chat.id).pending_messages) == before
  # The next exact-Goal turn carries it; completion need not wait for the
  # end-of-turn provider-success acknowledgement to record an honest outcome.
  receiver = models.ChatRun(id="receiving", chat_id=chat.id, root_run_id=run.id,
    goal_id=goal.id, provider="codex", status="running", provider_execution_admitted=True)
  setattr(receiver, f"admitted_{kind}_results_json", [row.id])
  db.add(receiver); db.commit()
  update_goal_record(db, receiver, goal, goal.revision, complete=True)
  db.expire_all()
  assert goal.status == "completed"
  assert row.result_disposition == "retired_by_goal_outcome" and row.received_run_id is None


@pytest.mark.parametrize("kind", ["wait", "dependency"])
@pytest.mark.parametrize("disposition", ["delivered", "withdrawn", "foreign_goal"])
def test_completion_ignores_results_no_longer_owed_to_its_exact_goal(db, chat, kind, disposition):
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind)
  if disposition == "foreign_goal":
    other = models.ChatGoal(id="other", chat_id=chat.id, objective="Separate work")
    db.add(other); db.flush()
    setattr(row, "goal_id" if kind == "wait" else "waiting_goal_id", other.id)
  else:
    row.result_disposition = disposition
  db.commit()
  update_goal_record(db, run, goal, goal.revision, complete=True)
  db.refresh(row)
  assert row.result_disposition == (None if disposition == "foreign_goal" else disposition)


@pytest.mark.parametrize("kind", ["wait", "dependency"])
@pytest.mark.parametrize("outcome", ["cancel", "cannot_complete"])
def test_explicit_non_success_can_retire_an_unseen_result_without_claiming_receipt(db, chat, kind, outcome):
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind)
  detail = "Owner called off this outcome" if outcome == "cancel" else {
    "reason": "No feasible route", "efforts": "Verified constraints", "unmet_outcome": "Not shipped"}
  update_goal_record(db, run, goal, goal.revision, **{outcome: detail})
  db.refresh(row)
  assert goal.status == ("cancelled" if outcome == "cancel" else outcome)
  assert row.result_disposition == "retired_by_goal_outcome" and row.received_run_id is None


@pytest.mark.parametrize("resolved", [False, True])
def test_deferral_requires_resolving_or_withdrawing_the_exact_dependency(db, chat, resolved):
  from app.goal_plans import GoalPlanError
  from app.chat_writer import WithdrawGoalDependency
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, "dependency", resolved=resolved)
  before = goal.revision
  with pytest.raises(GoalPlanError, match="dependency:" + row.id):
    update_goal_record(db, run, goal, goal.revision, defer=REASON)
  db.expire_all()
  assert goal.status == "open" and goal.revision == before and row.result_disposition is None
  submit(WithdrawGoalDependency(chat_id=chat.id, dependency_id=row.id))
  db.expire_all()
  update_goal_record(db, run, goal, goal.revision, defer=REASON)
  assert goal.status == "stopped" and row.result_disposition == "withdrawn"


def test_completion_can_retire_an_unresolved_dependency(db, chat):
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, "dependency", resolved=False)
  update_goal_record(db, run, goal, goal.revision, complete=True)
  db.refresh(row)
  assert row.result_disposition == "retired_by_goal_outcome"


@pytest.mark.parametrize("kind", ["wait", "dependency"])
def test_result_resolving_between_blocker_read_and_completion_cas_wins(db, chat, kind, monkeypatch):
  from app import goals
  from app.database import SessionLocal
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind, resolved=False)
  original = goals._unseen_goal_result_queries
  calls = 0
  def resolve_before_cas(*args):
    nonlocal calls
    calls += 1
    if calls == 2:
      # A different source transaction wins after the friendly blocker read.
      with SessionLocal() as source_db:
        source = source_db.get(type(row), row.id)
        if kind == "wait":
          source.status = "met"
        else:
          source.resolved_at = datetime.now(UTC)
          source.outcome_json = {"kind": "completed", "outcome": "Verified"}
        source_db.commit()
    return original(*args)
  monkeypatch.setattr(goals, "_unseen_goal_result_queries", resolve_before_cas)
  with pytest.raises(GoalPlanConflict, match="Goal changed"):
    update_goal_record(db, run, goal, goal.revision, complete=True)
  assert calls == 2
  db.expire_all()
  assert goal.status == "open" and goal.revision == 4
  assert row.result_disposition is None
  assert (row.status == "met") if kind == "wait" else row.resolved_at is not None


@pytest.mark.parametrize("kind", ["wait", "dependency"])
def test_deferral_receipts_admitted_results_only_after_success(db, chat, kind):
  from app.chat_waits import owed_wait_results, stage_wait_results_delivered
  from app.goal_dependencies import owed_dependency_results, stage_dependency_results_delivered
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind)
  run.provider_execution_admitted = True
  setattr(run, f"admitted_{kind}_results_json", [row.id])
  db.commit()
  update_goal_record(db, run, goal, goal.revision, defer=REASON)
  db.refresh(row)
  assert row.result_disposition is None  # Tools do not promise provider success.
  owed = owed_wait_results if kind == "wait" else owed_dependency_results
  receipt = stage_wait_results_delivered if kind == "wait" else stage_dependency_results_delivered
  assert owed(db, chat.id, "", run_id=run.id) == ("", ())  # Never admit held work.
  assert receipt(db, chat.id, (row.id,), run_id=run.id) == 1
  db.commit(); db.refresh(row)
  assert row.result_disposition == "delivered" and row.received_run_id == run.id


@pytest.mark.parametrize("kind", ["wait", "dependency"])
@pytest.mark.parametrize("hold", ["owner_stop", "other_run", "legacy"])
def test_success_receipt_cannot_bypass_a_different_hold(db, chat, kind, hold):
  from app.chat_waits import stage_wait_results_delivered
  from app.goal_dependencies import stage_dependency_results_delivered
  from app.goals import _new_hold
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind)
  run.provider_execution_admitted = True
  setattr(run, f"admitted_{kind}_results_json", [row.id])
  goal.status = "stopped"
  goal.hold_json = None if hold == "legacy" else _new_hold(
    cause="stop" if hold == "owner_stop" else "deferred",
    actor="owner" if hold == "owner_stop" else "agent",
    run_id=run.id if hold == "owner_stop" else "other-run", source_id="hold", reason=REASON)
  db.commit()
  receipt = stage_wait_results_delivered if kind == "wait" else stage_dependency_results_delivered
  assert receipt(db, chat.id, (row.id,), run_id=run.id) == 0
  db.refresh(row)
  assert row.result_disposition is None


@pytest.mark.parametrize("kind", ["wait", "dependency"])
@pytest.mark.parametrize("admitted", [False, True])
def test_deferral_blocks_results_not_in_the_admitted_prompt(db, chat, kind, admitted):
  from app.goal_plans import GoalPlanError
  goal, run = seed(db, chat)
  row = _goal_result(db, chat, run, kind)
  run.provider_execution_admitted = admitted
  setattr(run, f"admitted_{kind}_results_json", [] if admitted else [row.id])
  db.commit()
  with pytest.raises(GoalPlanError, match=f"{kind}:" + row.id):
    update_goal_record(db, run, goal, goal.revision, defer=REASON)
  assert goal.status == "open" and row.result_disposition is None


@pytest.mark.parametrize("action", ["stop", "dismiss"])
def test_owner_end_survives_partial_claim_settlement_failure_and_retries(db, chat, monkeypatch, caplog, action):
  import asyncio
  from app import agent_work_claims, agent_coordination
  from app.chat_writer import PrepareChatStop, ClearPresentedGoal
  goal, run = seed(db, chat)
  owner = models.Owner(username="claim-owner", hashed_password="unused")
  db.add(owner); db.commit()
  agent_work_claims.claim_work(db, owner_id=owner.id, chat_id=chat.id,
    run_id=run.id, work_key="repair-stop", summary="Repair stop")
  claim = db.query(models.AgentWorkClaim).one()
  original = agent_work_claims.stage_settle_goal_claims
  def fail_after_changes(*args, **kwargs):
    original(*args, **kwargs)
    raise RuntimeError("Injected partial claim settlement failure")
  monkeypatch.setattr(agent_work_claims, "stage_settle_goal_claims", fail_after_changes)
  command = (PrepareChatStop(chat_id=chat.id, actor="owner") if action == "stop" else
    ClearPresentedGoal(chat_id=chat.id, expected_goal_id=goal.id))
  submit(command)
  db.expire_all()
  assert goal.status == ("stopped" if action == "stop" else "dismissed")
  assert claim.released_at is None and claim.completed_at is None
  assert "settlement notice will retry" in caplog.text
  monkeypatch.setattr(agent_work_claims, "stage_settle_goal_claims", original)
  asyncio.run(agent_coordination.settle_claims_with_owner(chat.id))
  db.expire_all()
  assert claim.released_at is not None and claim.completed_at is None


def test_a_running_helper_is_one_completion_blocker(db, chat):
  from app.goals import _helper_blockers
  goal, run = seed(db, chat)
  _helper(db, id="live", parent_chat_id=chat.id, goal=goal, run=run, status="running")
  db.commit()
  assert _helper_blockers(db, run, goal) == ["live"]
