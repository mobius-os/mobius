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
