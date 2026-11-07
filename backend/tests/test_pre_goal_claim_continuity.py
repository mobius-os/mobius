"""Only the exact continuing action gains the promoted Goal's liveness."""
from datetime import timedelta

import pytest

from app import models
from app.agent_work_claims import claim_work, finish_work
from app.chat_writer import PromoteRunToGoal, create_chat, get_writer
from app.goal_dependency_sources import stage_reconcile_claim_sources
from app.timeutil import now_naive_utc


KEY = "platform:continuing-action"


def setup_claim(db):
  owner = models.Owner(username="owner", hashed_password="unused")
  source = create_chat(id="source", title="Source", messages=[])
  follower = create_chat(id="follower", title="Follower", messages=[])
  run = models.ChatRun(id="source-run", root_run_id="source-run",
    chat_id=source.id, status="running")
  db.add_all([owner, source, follower, run,
    models.ChatGoal(id="follower-goal", chat_id=follower.id, objective="Follow"),
    models.ChatRun(id="follower-run", chat_id=follower.id, goal_id="follower-goal", status="running")])
  db.commit()
  acquired = claim_work(db, owner_id=owner.id, chat_id=source.id,
    run_id=run.id, work_key=KEY, summary="Exact action")
  assert claim_work(db, owner_id=owner.id, chat_id=follower.id,
    run_id="follower-run", work_key=KEY, summary="Follow")["follow_status"] == "followed"
  return owner, source, run, acquired


def promote(db, source, run):
  return get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
    chat_id=source.id, run_token=run.id, objective="Finish the exact action"))


@pytest.mark.parametrize("handoff", ["helper", "wait"])
def test_pre_goal_claim_promotion_keeps_native_handoff_and_one_real_follower_result(db, monkeypatch, handoff):
  import asyncio
  from app import chat as chat_mod, goal_dependencies as deps
  from app.chat_writer import AdmitProviderExecution, AcknowledgeProviderSuccess
  owner, source, run, acquired = setup_claim(db)
  if handoff == "helper":
    from app.delegations import DelegationIntent, create_or_attach_delegation
    helper, _ = create_or_attach_delegation(db, DelegationIntent(
      app_id=None, parent_chat_id=source.id, parent_root_run_id=run.id,
      task_key="executor", prompt="Complete the action", provider="codex",
      model=None, effort=None, cwd="/data"))
    db.add(models.ChatRun(id="helper-run", chat_id=helper.child_chat_id, status="running"))
    db.commit()
  result = promote(db, source, run)
  if handoff == "wait":
    from app.chat_waits import declare_wait
    declare_wait(db, chat_id=source.id, created_by_run_id=run.id,
      description="Action's external condition", kind="timer", delay_secs=60)
  run.status = "completed"
  db.commit()
  assert stage_reconcile_claim_sources(db) == 0
  claim = db.get(models.AgentWorkClaim, acquired["id"])
  assert claim.owner_goal_id == result["goal_id"]
  assert claim.acquisition_id == acquired["acquisition_id"]
  assert claim.revision == acquired["revision"]
  finish_work(db, owner_id=owner.id, chat_id=source.id, work_key=KEY,
    outcome="Verified real result", release=False)
  assert stage_reconcile_claim_sources(db) == 0
  db.expire_all()
  dependency = db.query(models.GoalDependency).one()
  assert dependency.outcome_json["kind"] == "completed"
  assert dependency.outcome_json["outcome"] == "Verified real result"
  assert dependency.target_acquisition_id == acquired["acquisition_id"]
  db.get(models.ChatRun, "follower-run").status = "completed"
  db.commit()
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(deps.deliver_dependency(dependency.id))
  db.expire_all()
  receiving = db.get(models.ChatRun, "dependency-resume-" + dependency.id)
  assert receiving.goal_id == "follower-goal" and len(scheduled) == 1
  writer = get_writer()
  writer.submit(AdmitProviderExecution(chat_id="follower", run_token=receiving.id,
    dependency_results=(dependency.id,))).result(timeout=5)
  writer.submit(AcknowledgeProviderSuccess(chat_id="follower", run_token=receiving.id,
    dependency_results=(dependency.id,))).result(timeout=5)
  db.expire_all()
  assert dependency.received_run_id == receiving.id
  assert not asyncio.run(deps.deliver_dependency(dependency.id))


def test_promotion_never_adopts_prior_attempts_claim_even_with_same_causal_root(db):
  owner, source, old, acquired = setup_claim(db)
  old.status = "failed"
  current = models.ChatRun(id="current", root_run_id=old.id, chat_id=source.id,
    status="running", started_at=now_naive_utc() + timedelta(seconds=1))
  db.add(current); db.commit()
  promote(db, source, current)
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id is None
  assert stage_reconcile_claim_sources(db) == 1


def test_successor_goal_never_auto_adopts_legacy_pre_goal_claim_from_same_run(db):
  _, source, run, acquired = setup_claim(db)
  # A historical NULL claim predating adoption can outlive the first Goal.
  # A second Goal in the same physical run is not proof it continues this action.
  db.add(models.ChatGoal(id="completed-goal", chat_id=source.id,
    objective="Former work", status="completed"))
  run.goal_id = "completed-goal"
  db.commit()
  promote(db, source, run)
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id is None
  run.status = "failed"
  db.commit()
  assert stage_reconcile_claim_sources(db) == 1


def test_explicit_reclaim_adopts_only_named_pre_goal_action(db):
  owner, source, old, acquired = setup_claim(db)
  claim_work(db, owner_id=owner.id, chat_id=source.id, run_id=old.id,
    work_key="platform:unrelated-action", summary="Separate action")
  old.status = "completed"
  current = models.ChatRun(id="current", root_run_id=old.id, chat_id=source.id,
    status="running", started_at=now_naive_utc() + timedelta(seconds=1))
  db.add(current); db.commit()
  goal_id = promote(db, source, current)["goal_id"]
  refreshed = claim_work(db, owner_id=owner.id, chat_id=source.id,
    run_id=current.id, work_key=KEY, summary="Explicitly continue this exact action")
  assert refreshed["owner_goal_id"] == goal_id
  assert refreshed["acquisition_id"] == acquired["acquisition_id"]
  assert db.query(models.AgentWorkClaim).filter_by(work_key="platform:unrelated-action").one().owner_goal_id is None
  assert stage_reconcile_claim_sources(db) == 0


def test_held_goal_never_gains_pre_goal_claim_through_refresh(db):
  owner, source, run, acquired = setup_claim(db)
  db.add(models.ChatGoal(id="held", chat_id=source.id, objective="Held", status="stopped"))
  run.goal_id = "held"
  db.commit()
  refreshed = claim_work(db, owner_id=owner.id, chat_id=source.id,
    run_id=run.id, work_key=KEY, summary="Still held")
  assert refreshed["owner_goal_id"] is None
  assert stage_reconcile_claim_sources(db) == 1


def test_refresh_rechecks_goal_hold_after_graph_fence(db, monkeypatch):
  from app import agent_work_claims
  from app.database import SessionLocal
  owner, source, run, acquired = setup_claim(db)
  db.add(models.ChatGoal(id="becoming-held", chat_id=source.id, objective="Work"))
  run.goal_id = "becoming-held"
  db.commit()
  original = agent_work_claims.fence_owner
  def hold_before_fence(session, owner_id):
    with SessionLocal() as other:
      other.get(models.ChatGoal, "becoming-held").status = "stopped"
      other.commit()
    original(session, owner_id)
  monkeypatch.setattr(agent_work_claims, "fence_owner", hold_before_fence)
  refreshed = claim_work(db, owner_id=owner.id, chat_id=source.id,
    run_id=run.id, work_key=KEY, summary="Cannot adopt held work")
  assert refreshed["owner_goal_id"] is None
  assert refreshed["acquisition_id"] == acquired["acquisition_id"]


def test_pre_goal_refresh_refuses_reverse_dependency_cycle(db):
  owner, source, old, acquired = setup_claim(db)
  old.status = "completed"
  current = models.ChatRun(id="current", root_run_id=old.id, chat_id=source.id,
    status="running", started_at=now_naive_utc() + timedelta(seconds=1))
  db.add(current); db.commit()
  promote(db, source, current)
  claim_work(db, owner_id=owner.id, chat_id="follower", run_id="follower-run",
    work_key="platform:reverse-action", summary="Other action")
  claim_work(db, owner_id=owner.id, chat_id=source.id, run_id=current.id,
    work_key="platform:reverse-action", summary="Follow the other action")
  with pytest.raises(ValueError, match="direct Goal dependency cycle"):
    claim_work(db, owner_id=owner.id, chat_id=source.id, run_id=current.id,
      work_key=KEY, summary="Would make a cycle")
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id is None
  assert all(dep.resolved_at is None for dep in db.query(models.GoalDependency))


def test_promotion_rolls_back_goal_and_exact_claim_if_adoption_conflicts(db, monkeypatch):
  from app import agent_work_claims
  from app.chat_writer import GoalPromotionRejected
  _, source, run, acquired = setup_claim(db)
  def refuse_adoption(*args, **kwargs):
    raise ValueError("Transfer would create a direct Goal dependency cycle.")
  monkeypatch.setattr(agent_work_claims, "_stage_goal_owner_change", refuse_adoption)
  result = promote(db, source, run)
  assert isinstance(result, GoalPromotionRejected)
  assert result.reason == "claim_adoption_conflict"
  db.expire_all()
  assert run.goal_id is None
  assert db.query(models.ChatGoal).filter_by(chat_id=source.id).count() == 0
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id is None
  assert db.query(models.GoalDependency).one().resolved_at is None


def test_refresh_cannot_silently_adopt_a_different_acquisition_after_graph_fence(db, monkeypatch):
  from app import agent_work_claims
  from app.database import SessionLocal
  owner, source, run, acquired = setup_claim(db)
  original = agent_work_claims.fence_owner
  def reacquire_before_fence(session, owner_id):
    monkeypatch.setattr(agent_work_claims, "fence_owner", original)
    with SessionLocal() as other:
      finish_work(other, owner_id=owner.id, chat_id=source.id,
        work_key=KEY, outcome="Release old acquisition", release=True)
      claim_work(other, owner_id=owner.id, chat_id=source.id, run_id=run.id,
        work_key=KEY, summary="Different acquisition")
    original(session, owner_id)
  monkeypatch.setattr(agent_work_claims, "fence_owner", reacquire_before_fence)
  with pytest.raises(ValueError, match="claim changed while refreshing"):
    claim_work(db, owner_id=owner.id, chat_id=source.id, run_id=run.id,
      work_key=KEY, summary="Refresh original acquisition")
  db.rollback()
  assert db.get(models.AgentWorkClaim, acquired["id"]).acquisition_id != acquired["acquisition_id"]


@pytest.mark.parametrize("goal_owned", [False, True])
def test_stall_records_exact_owner_and_native_handoffs_not_an_unrelated_goal(db, goal_owned):
  _, source, run, acquired = setup_claim(db)
  goal_id = promote(db, source, run)["goal_id"] if goal_owned else None
  run.status = "failed"
  db.add_all([models.ChatGoal(id="unrelated", chat_id=source.id, objective="Other work"),
    models.ChatRun(id="unrelated-run", chat_id=source.id, goal_id="unrelated",
      status="running", started_at=now_naive_utc() + timedelta(seconds=1))])
  db.commit()
  assert stage_reconcile_claim_sources(db) == 1
  db.commit(); db.expire_all()
  outcome = db.query(models.GoalDependency).one().outcome_json
  assert outcome["kind"] == "owner_not_progressing"
  evidence = outcome["source_diagnostics"]
  assert evidence["claim"] == {"id": acquired["id"], "acquisition_id": acquired["acquisition_id"],
    "revision": acquired["revision"], "owner_chat_id": source.id,
    "owner_run_id": run.id, "owner_goal_id": goal_id}
  assert evidence["owner_run"]["root_run_id"] == run.id
  assert evidence["owner_run"]["status"] == "failed"
  assert evidence["owner_goal"] == ({"id": goal_id, "status": "open"} if goal_owned else None)
  assert evidence["latest_chat_run"]["goal_id"] == "unrelated"
  assert evidence["native_handoffs"]["running"] is False
  assert evidence["native_handoffs"]["waits"] == {"count": 0, "sample": []}


def test_stall_handoff_evidence_is_bounded_without_losing_counts():
  from app.chat_handoffs import GoalHandoffs, GoalWaitHandoff
  from app.goal_dependency_sources import _native_handoff_diagnostics
  handoffs = GoalHandoffs(card=None, running=False, helpers=(), dependencies=(),
    park=None, failed_attempt=True, waits=tuple(
      GoalWaitHandoff(wait_id=f"wait-{index}", kind="timer",
        delivery_pending=True, resume_blocker="manual_resume") for index in range(20)))
  evidence = _native_handoff_diagnostics(handoffs)
  assert evidence["waits"]["count"] == 20
  assert len(evidence["waits"]["sample"]) == 8
  assert evidence["waits"]["sample"][0] == {
    "wait_id": "wait-0", "kind": "timer", "delivery_pending": True, "resume_blocker": "manual_resume"}
  assert evidence["progress_owned"] is False


@pytest.mark.parametrize("target_status", ["open", "stopped"])
def test_goal_less_attempt_attaches_or_owner_resumes_existing_goal_with_its_claim(db, target_status):
  from app.chat_waits import declare_wait
  from app.goals import stage_goal_hold
  _, source, run, acquired = setup_claim(db)
  goal = models.ChatGoal(id="retained", chat_id=source.id, objective="Retained work")
  db.add(goal); db.flush()
  if target_status == "stopped":
    stage_goal_hold(goal, cause="stop", actor="owner", source_id="hold", run_id="earlier-run")
    # Only later owner input authorizes reopening; adoption grants no resume.
    run.owner_input_at = now_naive_utc() + timedelta(seconds=1)
  db.commit()
  result = get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
    chat_id=source.id, run_token=run.id, objective=goal.objective, resume_goal_id=goal.id))
  assert result["goal_id"] == goal.id
  declare_wait(db, chat_id=source.id, created_by_run_id=run.id,
    description="Retained action's condition", kind="timer", delay_secs=60)
  run.status = "completed"
  db.commit()
  assert stage_reconcile_claim_sources(db) == 0
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id == goal.id
  assert db.query(models.GoalDependency).one().resolved_at is None


def test_claim_adoption_itself_never_reopens_a_held_goal(db):
  from app.agent_work_claims import stage_adopt_pre_goal_claims
  from app.decision_cards import fence_owner
  from app.goals import stage_goal_hold
  _, source, run, acquired = setup_claim(db)
  goal = models.ChatGoal(id="held-adoption", chat_id=source.id, objective="Held work")
  db.add(goal); db.flush()
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="hold", run_id=run.id)
  hold = dict(goal.hold_json)
  run.goal_id = goal.id
  db.commit()
  fence_owner(db)
  stage_adopt_pre_goal_claims(db, run)
  db.commit()
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id == goal.id
  assert goal.status == "stopped" and goal.hold_json == hold
  assert stage_reconcile_claim_sources(db) == 1
  db.commit(); db.expire_all()
  assert db.query(models.GoalDependency).one().outcome_json["reason"] == "source_held"


def test_automatic_attempt_cannot_attach_claims_to_reopen_a_held_goal(db):
  from app.chat_writer import GoalPromotionRejected
  from app.goals import stage_goal_hold
  _, source, run, acquired = setup_claim(db)
  goal = models.ChatGoal(id="held-target", chat_id=source.id, objective="Held work")
  db.add(goal); db.flush()
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="hold", run_id="earlier-run")
  db.commit()
  result = get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
    chat_id=source.id, run_token=run.id, objective=goal.objective, resume_goal_id=goal.id))
  assert isinstance(result, GoalPromotionRejected)
  assert result.reason == "owner_continuation_required"
  assert goal.status == "stopped" and run.goal_id is None
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id is None


def test_attaching_existing_goal_rolls_back_a_real_claim_dependency_cycle(db):
  from app.chat_writer import GoalPromotionRejected
  owner, source, run, acquired = setup_claim(db)
  goal = models.ChatGoal(id="retained", chat_id=source.id, objective="Retained work")
  prior = models.ChatRun(id="prior", chat_id=source.id, goal_id=goal.id,
    status="completed", started_at=run.started_at - timedelta(minutes=1))
  db.add_all([goal, prior]); db.commit()
  claim_work(db, owner_id=owner.id, chat_id="follower", run_id="follower-run",
    work_key="platform:reverse-attach", summary="Other action")
  claim_work(db, owner_id=owner.id, chat_id=source.id, run_id=prior.id,
    work_key="platform:reverse-attach", summary="Retained Goal follows other action")
  result = get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
    chat_id=source.id, run_token=run.id, objective=goal.objective, resume_goal_id=goal.id))
  assert isinstance(result, GoalPromotionRejected)
  assert result.reason == "claim_adoption_conflict"
  assert run.goal_id is None and goal.status == "open"
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id is None


@pytest.mark.parametrize("reacquire", [False, True])
def test_claim_acquisition_serializes_goal_snapshot_with_promotion(db, monkeypatch, reacquire):
  from app import agent_work_claims
  from app.database import SessionLocal
  owner, source, run, _ = setup_claim(db)
  key = KEY if reacquire else "platform:first-acquisition"
  if reacquire:
    finish_work(db, owner_id=owner.id, chat_id=source.id, work_key=key,
      outcome="Previous acquisition released", release=True)
  original_goal = agent_work_claims._goal_id
  original_fence = agent_work_claims.fence_owner
  fenced = False
  promoted = None
  def mark_fence(session, owner_id):
    nonlocal fenced
    original_fence(session, owner_id)
    fenced = True
  def promote_after_unfenced_goal_snapshot(session, chat_id, run_id):
    nonlocal promoted
    snapshot = original_goal(session, chat_id, run_id)
    if not fenced:
      # Force the writer to commit between the stale snapshot and claim write.
      # Once acquisition owns the fence this interleaving is impossible.
      with SessionLocal() as other:
        promoted = get_writer()._promote_run_to_goal(other, PromoteRunToGoal(
          chat_id=source.id, run_token=run.id, objective="Exact action"))
    return snapshot
  monkeypatch.setattr(agent_work_claims, "fence_owner", mark_fence)
  monkeypatch.setattr(agent_work_claims, "_goal_id", promote_after_unfenced_goal_snapshot)
  acquired = claim_work(db, owner_id=owner.id, chat_id=source.id,
    run_id=run.id, work_key=key, summary="Acquiring during promotion")
  if promoted is None:
    promoted = get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
      chat_id=source.id, run_token=run.id, objective="Exact action"))
  db.expire_all()
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id == promoted["goal_id"]


@pytest.mark.parametrize("existing_goal", [False, True])
@pytest.mark.parametrize("handoff", ["wait", "helper"])
def test_first_goal_attachment_adopts_the_same_attempts_pre_goal_handoff(db, existing_goal, handoff):
  _, source, run, acquired = setup_claim(db)
  if handoff == "wait":
    from app.chat_waits import declare_wait
    native = declare_wait(db, chat_id=source.id, created_by_run_id=run.id,
      description="Exact action's condition", kind="timer", delay_secs=60)
  else:
    from app.delegations import DelegationIntent, create_or_attach_delegation
    native, _ = create_or_attach_delegation(db, DelegationIntent(
      app_id=None, parent_chat_id=source.id, parent_root_run_id=run.id,
      task_key="executor", prompt="Complete the action", provider="codex",
      model=None, effort=None, cwd="/data"))
    db.add(models.ChatRun(id="helper-run", chat_id=native.child_chat_id, status="running"))
    db.commit()
  assert native.goal_id is None
  if existing_goal:
    goal = models.ChatGoal(id="retained", chat_id=source.id, objective="Retained work")
    db.add(goal); db.commit()
    result = get_writer()._promote_run_to_goal(db, PromoteRunToGoal(
      chat_id=source.id, run_token=run.id, objective=goal.objective, resume_goal_id=goal.id))
  else:
    result = promote(db, source, run)
  run.status = "completed"
  db.commit()
  assert stage_reconcile_claim_sources(db) == 0
  db.refresh(native)
  assert native.goal_id == result["goal_id"]
  assert db.get(models.AgentWorkClaim, acquired["id"]).owner_goal_id == result["goal_id"]


def test_wait_adoption_excludes_earlier_attempts_disposed_results_and_activation(db):
  from app.chat_waits import declare_wait
  _, source, run, _ = setup_claim(db)
  previous = models.ChatRun(id="previous", root_run_id="previous", chat_id=source.id,
    status="completed", started_at=run.started_at - timedelta(minutes=1))
  db.add(previous)
  run.root_run_id = previous.id
  db.commit()
  def wait(run_id, label):
    return declare_wait(db, chat_id=source.id, created_by_run_id=run_id,
      description=label, kind="timer", delay_secs=60)
  older = wait(previous.id, "Earlier attempt")
  current = wait(run.id, "Current attempt")
  disposed = wait(run.id, "Already received")
  disposed.status = "met"
  disposed.result_disposition = "delivered"
  activation = wait(run.id, "Card-owned activation")
  activation.kind = "platform_activation"
  db.commit()
  goal_id = promote(db, source, run)["goal_id"]
  db.expire_all()
  assert current.goal_id == goal_id
  assert older.goal_id is disposed.goal_id is activation.goal_id is None
  assert current.root_run_id == older.root_run_id == previous.id


def test_source_stall_diagnostics_stay_stored_but_never_enter_follower_notices(db):
  from app.goal_dependencies import dependency_notice, dependency_summary
  _, source, run, _ = setup_claim(db)
  goal_id = promote(db, source, run)["goal_id"]
  run.status = "failed"
  db.commit()
  assert stage_reconcile_claim_sources(db) == 1
  db.commit(); db.expire_all()
  dependency = db.query(models.GoalDependency).one()
  stored = dict(dependency.outcome_json)
  assert stored["source_diagnostics"]["owner_run"]["id"] == run.id
  notice = dependency_notice(dependency)
  assert "source_diagnostics" not in notice
  assert run.id not in notice and goal_id not in notice
  assert "owner_not_progressing" in notice and KEY in notice
  assert "source_diagnostics" not in dependency_summary(db, dependency)["outcome"]
  db.refresh(dependency)
  assert dependency.outcome_json == stored


def test_wait_declaration_serializes_its_goal_snapshot_with_first_attachment(db, monkeypatch):
  from sqlalchemy import event
  from sqlalchemy.orm import Session
  from app import chat_waits
  from app.database import SessionLocal
  from app.decision_cards import fence_owner
  _, source, run, _ = setup_claim(db)
  fenced = False
  promoted = None
  def mark_fence(session):
    nonlocal fenced
    fence_owner(session)
    fenced = True
  def promote_before_unfenced_wait_flush(session, _ctx, _instances):
    nonlocal promoted
    if fenced or promoted is not None or not any(
      isinstance(row, models.ChatWait) for row in session.new
    ):
      return
    with SessionLocal() as other:
      promoted = get_writer()._promote_run_to_goal(other, PromoteRunToGoal(
        chat_id=source.id, run_token=run.id, objective="Exact action"))
  monkeypatch.setattr(chat_waits, "fence_owner", mark_fence, raising=False)
  event.listen(Session, "before_flush", promote_before_unfenced_wait_flush)
  try:
    wait = chat_waits.declare_wait(db, chat_id=source.id, created_by_run_id=run.id,
      description="Concurrent handoff", kind="timer", delay_secs=60)
  finally:
    event.remove(Session, "before_flush", promote_before_unfenced_wait_flush)
  if promoted is None:
    promoted = promote(db, source, run)
  db.refresh(wait)
  assert wait.goal_id == promoted["goal_id"]


def test_wait_declaration_refreshes_the_exact_run_after_promotion_wins(db):
  from app.chat_waits import declare_wait
  from app.database import SessionLocal
  _, source, run, _ = setup_claim(db)
  with SessionLocal() as other:
    promoted = get_writer()._promote_run_to_goal(other, PromoteRunToGoal(
      chat_id=source.id, run_token=run.id, objective="Exact action"))
  # This session still has the pre-promotion physical attempt cached.
  assert run.goal_id is None
  wait = declare_wait(db, chat_id=source.id, created_by_run_id=run.id,
    description="Promotion already committed", kind="timer", delay_secs=60)
  assert wait.goal_id == promoted["goal_id"]
