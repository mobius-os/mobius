"""A followed acquisition owes its result to the subscribing Goal, not its chat."""
import asyncio

import pytest

from app import chat as chat_mod, models
from app import goal_dependencies as deps
from app.chat_writer import (
  AcknowledgeProviderSuccess, AdmitProviderExecution, AppendPending,
  PromotePending, StartContinuation, StartContinuationBlocked, StartTurn,
  _PersistFailed, get_writer,
)
from app.timeutil import now_naive_utc


def _run(db, chat, name, goal_id=None, *, admitted=True):
  if goal_id and db.get(models.ChatGoal, goal_id) is None:
    db.add(models.ChatGoal(id=goal_id, chat_id=chat.id, objective=goal_id))
  run = models.ChatRun(id=name, root_run_id=name, chat_id=chat.id,
    goal_id=goal_id, status="completed", provider="claude",
    provider_execution_admitted=admitted)
  db.add(run)
  db.commit()
  return run


def _dependency(db, chat, run, name="dep", *, settled=True):
  row = models.GoalDependency(id=name, waiting_chat_id=chat.id,
    waiting_goal_id=run.goal_id, created_by_run_id=run.id,
    target_kind="claim_acquisition", target_key="claim:acquisition",
    target_claim_id="claim", target_acquisition_id="acquisition",
    target_chat_id="source", resolved_at=now_naive_utc() if settled else None,
    outcome_json={"kind": "released", "outcome": "Not performed", "summary": "Host repair"} if settled else None)
  db.add(row)
  db.flush()
  run.admitted_dependency_results_json = list(run.admitted_dependency_results_json or []) + [row.id]
  db.commit()
  return row


def _ack(chat, run, ids):
  get_writer().submit(AcknowledgeProviderSuccess(chat_id=chat.id,
    run_token=run.id, dependency_results=ids)).result(timeout=5)


def _source_claim(db, source, *, claim_id="source-claim", acquisition="source-acquisition"):
  owner = db.query(models.Owner).first()
  if owner is None:
    owner = models.Owner(username="dependency-test-owner", hashed_password="unused")
    db.add(owner)
    db.flush()
  claim = models.AgentWorkClaim(id=claim_id, acquisition_id=acquisition,
    owner_id=owner.id, owner_chat_id=source.id, owner_run_id="source-run",
    owner_goal_id="source-goal", work_key=claim_id, summary="Shared action")
  db.add(claim)
  db.commit()
  return claim


@pytest.mark.parametrize("state,expected", [
  ("clean_gap", None),
  ("failed", "owner_not_progressing"),
  ("already_attempted", "owner_not_progressing"),
  ("foreign_latest", "owner_not_progressing"),
])
def test_claim_source_clean_settlement_gap_is_exact_and_bounded(db, chat, state, expected):
  from app.chat_writer import create_chat
  from app.goal_dependency_sources import dependency_source_blocker, stage_reconcile_claim_sources
  source = create_chat(id="source-chat", title="Source", messages=[])
  db.add(source)
  source_run = _run(db, source, "source-run", "source-goal")
  claim = _source_claim(db, source)
  follower = _run(db, chat, "follower-run", "follower-goal")
  row = models.GoalDependency(id="follow-source", waiting_chat_id=chat.id,
    waiting_goal_id=follower.goal_id, target_kind="claim_acquisition",
    target_key="claim:source-acquisition", target_claim_id=claim.id,
    target_acquisition_id=claim.acquisition_id, target_chat_id=source.id)
  db.add(row)
  if state == "failed":
    source_run.status = "failed"
  elif state == "already_attempted":
    source_run.continuation_json = {"reason": "goal_settlement", "goal_id": "source-goal"}
  elif state == "foreign_latest":
    _run(db, source, "z-foreign-run", "foreign-goal")
  db.commit()

  assert dependency_source_blocker(db, row) == expected
  assert stage_reconcile_claim_sources(db) == (0 if expected is None else 1)
  db.refresh(row)
  assert (row.resolved_at is None) is (expected is None)


def test_claim_follow_cap_counts_owed_cards_and_excludes_inactive_rows(db, chat):
  from app.chat_writer import create_chat
  from app.goal_dependency_sources import outstanding_dependency_count, stage_follow_claim
  source = create_chat(id="cap-source", title="Source", messages=[])
  db.add(source)
  _run(db, source, "source-run", "source-goal")
  first = _source_claim(db, source)
  follower = _run(db, chat, "follower-run", "follower-goal")
  for index in range(65):
    row = models.GoalDependency(id=f"card-{index}", waiting_chat_id=chat.id,
      waiting_goal_id=follower.goal_id, target_kind="owner_card",
      target_key=f"card:source:{index}", target_chat_id=source.id,
      target_question_id=f"question-{index}")
    if index == 0:
      row.withdrawn_at = now_naive_utc()
    elif index == 1:
      row.owner_suppressed_at = now_naive_utc()
    elif index == 2:
      row.result_disposition = "delivered"
    elif index == 3:
      row.resolved_at = now_naive_utc()  # Owed results still consume capacity.
    db.add(row)
  db.commit()
  assert outstanding_dependency_count(db, follower.goal_id) == 62
  assert stage_follow_claim(db, first, chat_id=chat.id, run_id=follower.id) == "followed"
  assert outstanding_dependency_count(db, follower.goal_id) == 63
  second = _source_claim(db, source, claim_id="another-claim", acquisition="another-acquisition")
  assert stage_follow_claim(db, second, chat_id=chat.id, run_id=follower.id) == "followed"
  assert outstanding_dependency_count(db, follower.goal_id) == 64
  third = _source_claim(db, source, claim_id="third-claim", acquisition="third-acquisition")
  assert stage_follow_claim(db, third, chat_id=chat.id, run_id=follower.id) == "dependency_limit"


def test_two_goals_following_one_acquisition_and_goalless_turn_have_exact_receipts(db, chat):
  a = _run(db, chat, "a", "goal-a")
  b = _run(db, chat, "b", "goal-b")
  plain = _run(db, chat, "plain")
  da, dbb = _dependency(db, chat, a, "da"), _dependency(db, chat, b, "db")
  ids = da.id, dbb.id
  assert deps.owed_dependency_results(db, chat.id, "", run_id=plain.id) == ("", ())
  _ack(chat, plain, ids)
  db.expire_all()
  assert all(db.get(models.GoalDependency, id).received_run_id is None for id in ids)
  assert deps.owed_dependency_results(db, chat.id, "", run_id=b.id)[1] == (dbb.id,)
  _ack(chat, b, ids)
  db.expire_all()
  assert dbb.received_run_id == b.id and da.received_run_id is None
  _ack(chat, a, ids)
  db.expire_all()
  assert da.received_run_id == a.id
  assert deps.owed_dependency_results(db, chat.id, "", run_id=a.id) == ("", ())


def test_delivery_sweep_skips_writer_without_pending_claim_sources(db, chat, monkeypatch):
  from app import chat_writer
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  visited = []
  async def delivered(row_id):
    visited.append(row_id)
    return True
  monkeypatch.setattr(deps, "deliver_dependency", delivered)
  monkeypatch.setattr(deps, "withdraw_disposed_dependency_notices", delivered)
  writer_requests = []
  def unexpected_writer():
    writer_requests.append(True)
    raise AssertionError("no pending claim source should reach the writer")
  monkeypatch.setattr(chat_writer, "get_writer", unexpected_writer)
  assert asyncio.run(deps.deliver_pending_dependencies()) == 1
  assert row.id in visited
  assert writer_requests == []


def test_claim_reconcile_failure_does_not_starve_owed_delivery(db, chat, monkeypatch):
  from app import chat_writer
  a = _run(db, chat, "a", "goal-a")
  owed = _dependency(db, chat, a)
  pending = models.GoalDependency(id="pending", waiting_chat_id=chat.id,
    waiting_goal_id=a.goal_id, target_kind="claim_acquisition",
    target_key="claim:pending", target_claim_id="pending-claim",
    target_acquisition_id="pending-acquisition")
  db.add(pending)
  db.commit()
  visited = []
  async def delivered(row_id):
    visited.append(row_id)
    return True
  async def cleanup(_chat_id):
    return 0
  monkeypatch.setattr(deps, "deliver_dependency", delivered)
  monkeypatch.setattr(deps, "withdraw_disposed_dependency_notices", cleanup)
  monkeypatch.setattr(chat_writer, "get_writer", lambda: (_ for _ in ()).throw(
    RuntimeError("writer unavailable")))
  assert asyncio.run(deps.deliver_pending_dependencies()) == 1
  assert visited == [owed.id]


def test_one_dependency_delivery_failure_does_not_starve_another(db, chat, monkeypatch):
  a = _run(db, chat, "a", "goal-a")
  b = _run(db, chat, "b", "goal-b")
  first = _dependency(db, chat, a, "first")
  second = _dependency(db, chat, b, "second")
  seen = []
  async def maybe_deliver(row_id):
    seen.append(row_id)
    if row_id == first.id:
      raise RuntimeError("one provider continuation failed")
    return True
  async def cleanup(_chat_id):
    return 0
  monkeypatch.setattr(deps, "deliver_dependency", maybe_deliver)
  monkeypatch.setattr(deps, "withdraw_disposed_dependency_notices", cleanup)
  assert asyncio.run(deps.deliver_pending_dependencies()) == 1
  assert set(seen) == {first.id, second.id}


def test_unrelated_latest_goal_cannot_steal_continuation(db, chat, monkeypatch):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  _run(db, chat, "b", "goal-b")
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(deps.deliver_dependency(row.id))
  db.expire_all()
  receiver = db.get(models.ChatRun, "dependency-resume-" + row.id)
  assert receiver.goal_id == a.goal_id and len(scheduled) == 1
  assert row.received_run_id is None
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=receiver.id,
    dependency_results=(row.id,))).result(timeout=5)
  _ack(chat, receiver, (row.id,))
  db.expire_all()
  assert row.result_disposition == "delivered" and row.received_run_id == receiver.id
  assert not asyncio.run(deps.deliver_dependency(row.id))


def test_hold_after_selection_keeps_result_until_exact_owner_continuation(db, chat):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  db.get(models.ChatGoal, a.goal_id).status = "stopped"
  db.commit()
  _ack(chat, a, (row.id,))
  db.expire_all()
  assert row.received_run_id is None
  assert deps.owed_dependency_results(db, chat.id, "", run_id=a.id) == ("", ())
  assert not asyncio.run(deps.deliver_dependency(row.id))
  assert db.get(models.ChatGoal, a.goal_id).status == "stopped"
  db.get(models.ChatGoal, a.goal_id).status = "open"
  db.commit()
  continued = _run(db, chat, "continued", a.goal_id)
  continued.admitted_dependency_results_json = [row.id]
  db.commit()
  _ack(chat, continued, (row.id,))
  db.expire_all()
  assert row.received_run_id == continued.id


@pytest.mark.parametrize("change", ["hold", "retire", "delete", "withdraw"])
def test_changed_dependency_cannot_cross_provider_admission(db, chat, change):
  a = _run(db, chat, "a", "goal-a", admitted=False)
  row = _dependency(db, chat, a)
  a.status = "running"
  if change == "hold":
    db.get(models.ChatGoal, a.goal_id).status = "stopped"
  elif change == "retire":
    deps.stage_retire_goal_dependencies(db, chat.id, a.goal_id)
  elif change == "delete":
    chat.deleted_at = now_naive_utc()
    deps.stage_retire_chat_dependencies(db, chat.id)
  else:
    row.withdrawn_at = now_naive_utc()
  db.commit()
  with pytest.raises(_PersistFailed, match="run is not eligible" if change == "delete" else "dependency result is no longer receivable"):
    get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=a.id,
      dependency_results=(row.id,))).result(timeout=5)
  db.expire_all()
  assert row.received_run_id is None


def test_result_resolved_after_prompt_snapshot_cannot_be_receipted(db, chat):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  a.admitted_dependency_results_json = []
  db.commit()
  _ack(chat, a, (row.id,))
  db.expire_all()
  assert row.result_disposition is None


def _notice(row):
  return {"role": "user", "kind": "dependency_result", "hidden": True,
    "cid": "dependency-result-" + row.id, "source_work_id": row.created_by_run_id,
    "content": deps.dependency_notice(row), "ts": 1}


def test_held_queued_dependency_never_starts_goalless_work(db, chat):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  db.get(models.ChatGoal, a.goal_id).status = "stopped"
  db.commit()
  get_writer().submit(AppendPending(chat_id=chat.id, run_token="", user_msg=_notice(row))).result(timeout=5)
  result = get_writer().submit(PromotePending(chat_id=chat.id, run_token="unwanted")).result(timeout=5)
  assert result["promoted"] is None
  with pytest.raises(_PersistFailed, match="dependency result"):
    get_writer().submit(StartTurn(chat_id=chat.id, run_token="unwanted",
      user_msg=_notice(row), title_source="Result", default_provider="claude")).result(timeout=5)
  result = get_writer().submit(StartContinuation(chat_id=chat.id,
    run_token="dependency-resume-" + row.id, root_run_id=a.id,
    cid="dependency-result-" + row.id, content=deps.dependency_notice(row),
    reason="dependency_result", message_kind="dependency_result", source_work_id=a.id,
    hidden=True)).result(timeout=5)
  assert isinstance(result, StartContinuationBlocked) and result.reason == "goal_held"
  assert db.get(models.ChatRun, "unwanted") is None


@pytest.mark.parametrize("settled", [False, True])
def test_delete_restore_retires_pending_and_owed_without_touching_source(db, chat, settled):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a, settled=settled)
  old_outcome = row.outcome_json
  deps.stage_retire_chat_dependencies(db, chat.id)
  chat.deleted_at = now_naive_utc()
  db.commit()
  chat.deleted_at = None
  db.commit()
  assert not asyncio.run(deps.deliver_dependency(row.id))
  db.refresh(row)
  assert row.result_disposition == "withdrawn" and row.withdrawn_at is not None
  assert row.outcome_json == old_outcome and row.received_run_id is None


@pytest.mark.parametrize("state", ["completed", "cancelled", "cannot_complete", "dismissed"])
def test_closed_goal_retires_without_receipt(db, chat, state):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  db.get(models.ChatGoal, a.goal_id).status = state
  db.commit()
  assert not asyncio.run(deps.deliver_dependency(row.id))
  db.refresh(row)
  assert row.result_disposition == "retired_by_goal_outcome" and row.received_run_id is None


def test_missing_subscribing_run_queues_exact_goal_without_invented_lineage(db, chat):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  row.created_by_run_id = None
  db.commit()
  assert not asyncio.run(deps.deliver_dependency(row.id))
  db.refresh(chat)
  assert chat.pending_messages[0]["source_work_id"] is None
  result = get_writer().submit(PromotePending(chat_id=chat.id, run_token="unused")).result(timeout=5)
  assert result["promoted"] is not None
  db.expire_all()
  receiver = db.get(models.ChatRun, "dependency-resume-" + row.id)
  assert receiver.goal_id == a.goal_id and receiver.root_run_id == receiver.id


@pytest.mark.parametrize("carrier_status,admitted,expected", [
  ("failed", True, "dependency-resume-dep.retry"),
  ("interrupted", True, "dependency-resume-dep"),
  ("interrupted", False, "dependency-resume-dep"),
  ("completed", True, "dependency-resume-dep"),
])
def test_carrier_retries_only_bounded_authorized_failure(db, chat, carrier_status, admitted, expected):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  carrier = _run(db, chat, "dependency-resume-dep", a.goal_id, admitted=admitted)
  carrier.status = carrier_status
  db.commit()
  assert deps.selected_dependency_carrier(db, row)[0] == expected
  if expected.endswith(".retry"):
    retry = _run(db, chat, expected, a.goal_id)
    retry.status = "completed"  # Provider ran but its receipt failed: no third try.
    db.commit()
  assert deps.dependency_resume_blocker(db, row) == "manual_resume"
  assert not asyncio.run(deps.deliver_dependency(row.id))
  assert row.received_run_id is None


def test_owner_card_barrier_queues_one_result_without_early_receipt(db, chat):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  chat.pending_question_id = "owner-choice"
  db.commit()
  assert not asyncio.run(deps.deliver_dependency(row.id))
  assert not asyncio.run(deps.deliver_dependency(row.id))
  db.refresh(chat)
  assert len(chat.pending_messages) == 1 and row.received_run_id is None
  assert deps.dependency_resume_blocker(db, row) == "owner_input"


def test_commit_before_schedule_orphan_is_exact_and_restart_safe(db, chat, monkeypatch):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  def cannot_schedule(**kw):
    raise RuntimeError("simulated task creation failure")
  monkeypatch.setattr(chat_mod, "_schedule_continuation", cannot_schedule)
  with pytest.raises(RuntimeError, match="task creation failure"):
    asyncio.run(deps.deliver_dependency(row.id))
  db.expire_all()
  carrier = db.get(models.ChatRun, "dependency-resume-" + row.id)
  assert carrier.provider_execution_admitted is False
  assert deps.safe_dependency_startup_orphan(db, chat, carrier)
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(deps.deliver_dependency(row.id))
  assert len(scheduled) == 1
  db.refresh(carrier)
  carrier.provider_execution_admitted = True
  db.commit()
  assert not deps.safe_dependency_startup_orphan(db, chat, carrier)
  assert row.received_run_id is None


def test_success_receipt_withdraws_only_its_stale_queue_not_owner_work(db, chat):
  a = _run(db, chat, "a", "goal-a")
  row = _dependency(db, chat, a)
  get_writer().submit(AppendPending(chat_id=chat.id, run_token="", user_msg=_notice(row))).result(timeout=5)
  get_writer().submit(AppendPending(chat_id=chat.id, run_token="", user_msg={
    "role": "user", "cid": "owner-followup", "content": "Different work", "ts": 2,
  })).result(timeout=5)
  _ack(chat, a, (row.id,))
  assert asyncio.run(deps.withdraw_disposed_dependency_notices(chat.id)) == 1
  db.refresh(chat)
  assert [msg["cid"] for msg in chat.pending_messages] == ["owner-followup"]


@pytest.mark.parametrize("event", ["completion", "transfer", "ambiguous_old_revision", "ordinary_interrupt", "quiet_backlog"])
def test_legacy_claim_carrier_cutover_never_retires_transfer_or_ordinary_interrupt(db, chat, event):
  import json
  from app.schema_migrations import _retire_legacy_claim_notice_carriers
  owner = models.Owner(username="legacy-owner", hashed_password="unused")
  db.add(owner)
  db.flush()
  claim = models.AgentWorkClaim(id="legacy-claim", owner_id=owner.id,
    owner_chat_id=chat.id, owner_run_id="legacy-source", work_key="legacy:action",
    summary="Action", revision=2,
    completed_at=None if event == "transfer" else now_naive_utc())
  db.add(claim)
  note = models.AgentCoordinationMessage(id="old-notice", room_kind="workspace", room_id=str(owner.id),
    from_chat_id=chat.id, to_chat_id=chat.id, from_run_id="work-claim:legacy-claim",
    send_id="revision:1" if event == "ambiguous_old_revision" else "revision:2",
    send_target_key=chat.id, delivery="interrupt", kind="handoff", body="Claim event")
  db.add(note)
  ids = [note.id]
  if event in {"ordinary_interrupt", "quiet_backlog"}:
    ordinary = models.AgentCoordinationMessage(id="ordinary", room_kind="workspace", room_id=str(owner.id),
      from_chat_id=chat.id, to_chat_id=chat.id, from_run_id="ordinary-run", send_id="send",
      send_target_key=chat.id, delivery="interrupt" if event == "ordinary_interrupt" else "next_turn", body="Keep this note")
    db.add(ordinary)
    ids.append(ordinary.id)
  db.commit()
  message = {"role": "user", "hidden": True, "kind": "peer_message", "cid": "peer-steer:"+ids[-1],
    "peer_message_through": {"id": ids[-1], "created_at": now_naive_utc().isoformat()},
    "content": "<agent_coordination>" + json.dumps({"self_chat_id": chat.id,
      "messages": [{"id": identity} for identity in ids]}) + "</agent_coordination>", "ts": 1}
  should_retire = event in {"completion", "quiet_backlog"}
  get_writer().submit(AppendPending(chat_id=chat.id, run_token="", user_msg=message)).result(timeout=5)
  _retire_legacy_claim_notice_carriers(db.get_bind())
  _retire_legacy_claim_notice_carriers(db.get_bind())  # interrupted ledger write is safe
  db.refresh(chat)
  assert bool(chat.pending_messages) is not should_retire
  assert db.query(models.AgentCoordinationMessage).count() == len(ids)
  # No peer cursor is falsely acknowledged and no durable note is erased.
  assert not db.query(models.ChatRun).filter(models.ChatRun.peer_message_through_id.is_not(None)).count()


def test_provider_success_receipts_exact_dependency_and_kept_side_question_together(db, chat):
  from app import transcript_rows
  from app.chat_writer import AppendSideQuestion
  run = _run(db, chat, "receiving", "goal")
  row = _dependency(db, chat, run)
  get_writer().submit(AppendSideQuestion(chat_id=chat.id, cid="side-1",
    question="Which checks?", answer="The focused checks.", provider="claude")).result(timeout=5)
  get_writer().submit(AcknowledgeProviderSuccess(chat_id=chat.id, run_token=run.id,
    dependency_results=(row.id,), side_question_cids=("side-1",))).result(timeout=5)
  db.expire_all()
  assert row.result_disposition == "delivered" and row.received_run_id == run.id
  assert transcript_rows.owed_side_questions(db, db.get(models.Chat, chat.id)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_sweep", ["wait", "dependency"])
async def test_supervisor_wait_and_dependency_sweeps_survive_each_others_failure(monkeypatch, failed_sweep):
  from app import broadcast as broadcasts, chat_waits
  from tests.test_runtime_supervisors import _supervisors
  broadcast = broadcasts.SystemBroadcast()
  monkeypatch.setattr(broadcasts, "get_system_broadcast", lambda: broadcast)
  calls, tick = [], asyncio.Event()
  async def waits(**_kwargs):
    calls.append("wait")
    if failed_sweep == "wait":
      raise RuntimeError("wait sweep broke")
  async def dependencies():
    calls.append("dependency")
    tick.set()
    if failed_sweep == "dependency":
      raise RuntimeError("dependency sweep broke")
  monkeypatch.setattr(chat_waits, "sweep_due_waits", waits)
  monkeypatch.setattr(deps, "deliver_pending_dependencies", dependencies)
  supervisors = _supervisors()
  spawn = supervisors._spawn
  def only_wait_loop(name, coroutine):
    if name == "chat-wait-sweep":
      spawn(name, coroutine)
    else:
      coroutine.close()
  monkeypatch.setattr(supervisors, "_spawn", only_wait_loop)
  await supervisors._start_chat_supervisors()
  try:
    await asyncio.sleep(0)
    for _ in range(2):
      tick.clear()
      broadcast.publish({"type": "platform_boot_ready"})
      await asyncio.wait_for(tick.wait(), timeout=1)
    assert calls == ["wait", "dependency", "wait", "dependency"]
  finally:
    await supervisors.stop()


def test_reconcile_after_clean_finish_preserves_unattempted_source_settlement(db, chat):
  from app.chat_writer import create_chat, FinishRun, ReconcileClaimSources
  from app.goal_dependency_sources import stage_follow_claim
  source = create_chat(id="source", title="Claim owner", messages=[])
  db.add(source)
  db.add(models.ChatGoal(id="source-goal", chat_id=source.id, objective="Shared action"))
  db.commit()
  get_writer().submit(StartTurn(chat_id=source.id, run_token="source-run",
    user_msg={"role": "user", "content": "Do shared action", "ts": 1})).result(5)
  db.expire_all()
  db.get(models.ChatRun, "source-run").goal_id = "source-goal"
  db.commit()
  claim = _source_claim(db, source)
  follower = _run(db, chat, "follower-run", "follower-goal")
  assert stage_follow_claim(db, claim, chat_id=chat.id, run_id=follower.id) == "followed"
  db.commit()
  get_writer().submit(AdmitProviderExecution(chat_id=source.id,
    run_token="source-run")).result(5)
  get_writer().submit(FinishRun(chat_id=source.id, run_token="source-run",
    terminal_status="completed")).result(5)
  assert get_writer().submit(ReconcileClaimSources()).result(5) == 0
  db.expire_all()
  row = db.query(models.GoalDependency).one()
  assert row.resolved_at is None
  assert db.get(models.ChatRun, "source-run").status == "completed"


def test_source_reconciliation_shares_visited_edges_across_diamond_branches(db, chat, monkeypatch):
  from collections import Counter
  from types import SimpleNamespace
  from app import chat_handoffs
  from app.chat_writer import create_chat
  from app.goal_dependency_sources import dependency_source_blocker
  claims = {}
  for name in ('a', 'b', 'c', 'd', 'e'):
    source = create_chat(id=f'source-{name}', messages=[])
    db.add(source)
    run = _run(db, source, f'run-{name}', f'goal-{name}')
    run.status = 'failed'
    claim = _source_claim(db, source, claim_id=f'claim-{name}', acquisition=f'acquisition-{name}')
    claim.owner_goal_id, claim.owner_run_id = run.goal_id, run.id
    claims[name] = claim
  follower = _run(db, chat, 'follower', 'follower-goal')
  edges = [(follower.goal_id, 'a'), ('goal-a', 'b'), ('goal-a', 'c'),
    ('goal-b', 'd'), ('goal-c', 'd'), ('goal-d', 'e')]
  for index, (waiting_goal, source) in enumerate(edges):
    claim = claims[source]
    db.add(models.GoalDependency(id=f'edge-{index}', waiting_goal_id=waiting_goal,
      waiting_chat_id=chat.id if index == 0 else f'source-{waiting_goal[-1]}',
      target_kind='claim_acquisition', target_key=f'claim:{claim.acquisition_id}',
      target_claim_id=claim.id, target_acquisition_id=claim.acquisition_id))
  db.commit()
  visits = Counter()
  def native(_db, goal):
    visits[goal.id] += 1
    return SimpleNamespace(progress_owned=False)
  monkeypatch.setattr(chat_handoffs, 'native_goal_handoffs', native)
  assert dependency_source_blocker(db, db.get(models.GoalDependency, 'edge-0')) == 'owner_not_progressing'
  assert visits['goal-e'] == 1  # Shared leaf edge, not a fresh traversal per branch.


def test_a_dependency_result_starts_while_a_stop_holds_the_owner_queue(db, chat, monkeypatch):
  """Held rows wait for the owner, not for this result: it starts now and
  leaves them held, instead of waiting for the idle-queue sweep."""
  a = _run(db, chat, "a-held", "goal-held")
  row = _dependency(db, chat, a, "dep-held")
  chat.pending_messages = [{"role": "user", "content": "later", "cid": "held", "ts": 1,
                            "held": True, "_owner_input_at": "2020-01-01T00:00:00+00:00"}]
  db.commit()
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(deps.deliver_dependency(row.id))
  db.expire_all()
  assert len(scheduled) == 1
  assert [(m["cid"], m.get("held")) for m in db.get(models.Chat, chat.id).pending_messages] == [
    ("held", True),
  ]
