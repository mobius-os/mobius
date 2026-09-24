"""Workspace work ownership stays singular while explicit transfer remains possible."""

import pytest

from app.timeutil import now_naive_utc

from app import models
from app.agent_work_claims import (
  acknowledge_notice,
  claim_work,
  finish_work,
  stage_release_claims_for_chat,
)


def _fixture(db):
  owner = models.Owner(username="owner", hashed_password="not-used")
  first = models.Chat(id="claim-first", title="Original integrator", messages=[])
  second = models.Chat(id="claim-second", title="Broader author", messages=[])
  db.add_all([owner, first, second])
  db.flush()
  runs = [
    models.ChatRun(
      id="claim-run-first", root_run_id="claim-run-first",
      goal_id="claim-goal-first", goal_objective="Ship the change",
      chat_id=first.id, status="running", provider="codex",
    ),
    models.ChatRun(
      id="claim-run-second", root_run_id="claim-run-second",
      goal_id="claim-goal-second", goal_objective="Align the platform",
      chat_id=second.id, status="running", provider="claude",
    ),
  ]
  db.add_all(runs)
  db.commit()
  return owner, first, second


def test_first_claim_wins_and_loser_follows_without_duplicate_ownership(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"

  won = claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  lost = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )

  assert won["state"] == "claimed"
  assert lost["state"] == "held_by_peer"
  assert lost["owner_chat_id"] == first.id
  assert db.query(models.AgentWorkClaim).count() == 1
  interest = db.query(models.AgentWorkInterest).one()
  assert (interest.chat_id, interest.goal_id) == (
    second.id, "claim-goal-second",
  )
  assert "does not transfer ownership of your whole Goal" in lost["next_action"]


def test_transfer_requires_current_owner_identity_and_records_reason(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )

  try:
    claim_work(
      db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
      work_key=key, summary="Finish the broader authored integration",
      takeover_reason="I authored the current PR head and the first owner is blocked.",
      expected_owner_chat_id="stale-owner",
    )
  except ValueError as exc:
    assert "expected owner changed" in str(exc)
  else:  # pragma: no cover - protects the deliberate transfer boundary
    raise AssertionError("stale transfer unexpectedly succeeded")

  moved = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Finish the broader authored integration",
    takeover_reason="I authored the current PR head and the first owner is blocked.",
    expected_owner_chat_id=first.id,
  )
  assert moved["state"] == "transferred"
  assert moved["owner_chat_id"] == second.id
  assert moved["previous_owner_chat_id"] == first.id
  assert moved["takeover_reason"].startswith("I authored")
  assert moved["notification_pending"] is True

  # If delivery failed after the atomic transfer, the new owner can repeat the
  # same claim and recover the exact pending handoff instead of losing it.
  retried = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Finish the broader authored integration",
    takeover_reason="I authored the current PR head and the first owner is blocked.",
    expected_owner_chat_id=first.id,
  )
  assert retried["state"] == "transferred"
  assert retried["previous_owner_chat_id"] == first.id
  assert retried["notification_pending"] is True


def test_completion_resolves_followers_and_same_key_cannot_be_reclaimed(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )
  finished = finish_work(
    db, owner_id=owner.id, chat_id=first.id, work_key=key,
    outcome="Merged as 0b44dc9d", release=False,
  )

  assert finished.interested_chat_ids == [second.id]
  assert finished.claim["state"] == "completed"
  acknowledge_notice(
    db, claim_id=finished.claim["id"], revision=finished.claim["revision"],
    resolve_interests=True,
  )
  later = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Try the completed operation again",
  )
  assert later["state"] == "completed"
  assert later["owner_chat_id"] == first.id


def test_deleted_follower_cannot_suppress_live_follower_notification(db):
  owner, first, deleted = _fixture(db)
  live = models.Chat(id="claim-live", title="Live follower", messages=[])
  live_run = models.ChatRun(
    id="claim-run-live", root_run_id="claim-run-live",
    goal_id="claim-goal-live", goal_objective="Follow exact work",
    chat_id=live.id, status="running", provider="codex",
  )
  db.add_all([live, live_run])
  db.commit()
  key = "platform:claim-fanout:test"
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id="claim-run-first", work_key=key, summary="Do exact work")
  for follower, run_id in ((deleted, "claim-run-second"), (live, live_run.id)):
    claim_work(db, owner_id=owner.id, chat_id=follower.id, run_id=run_id,
               work_key=key, summary="Follow exact work")
  deleted.deleted_at = now_naive_utc()
  db.commit()

  finished = finish_work(db, owner_id=owner.id, chat_id=first.id,
                         work_key=key, outcome="Done", release=False)

  assert finished.interested_chat_ids == [live.id]


def test_exact_action_claim_never_masquerades_as_whole_goal_handoff(db):
  from app.goal_plans import goal_handoff_owner_kind

  owner, first, second = _fixture(db)
  first.pending_question_id = "approval-card"
  db.commit()
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )

  assert goal_handoff_owner_kind(db, second.id, "claim-goal-second") is None


def test_deleting_owner_releases_only_unfinished_exact_action(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )

  released = stage_release_claims_for_chat(db, first.id)
  db.commit()

  assert len(released) == 1
  assert released[0].interested_chat_ids == [second.id]
  row = db.query(models.AgentWorkClaim).one()
  assert row.released_at is not None
  assert "deleted" in row.outcome
  reclaimed = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Take over the released action",
  )
  assert reclaimed["state"] == "claimed"
  assert reclaimed["owner_chat_id"] == second.id


# --- Claims settle with their owner's Goal -------------------------------

KEY = "github:mobius-os/mobius:pr:1079:3134e050:merge"


def _owned_goal_claim(db, *, task_status="completed"):
  """First chat's open Goal owns KEY; the second chat's Goal follows it."""
  owner, first, second = _fixture(db)
  db.add_all([
    models.ChatGoal(
      id="claim-goal-first", chat_id=first.id, objective="Ship the change",
      plan_json={"tasks": [{
        "id": "ship", "title": "Ship", "status": task_status,
        "depends_on": [],
      }]},
      revision=1,
    ),
    models.ChatGoal(
      id="claim-goal-second", chat_id=second.id, objective="Align the platform",
    ),
  ])
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
             work_key=KEY, summary="Merge the reviewed PR")
  follower = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Merge the same reviewed PR",
  )
  assert follower["state"] == "held_by_peer"
  return owner, first, second


def _claim_row(db):
  db.expire_all()
  return db.query(models.AgentWorkClaim).filter_by(work_key=KEY).one()


def test_goal_completion_completes_its_open_claims_with_the_verified_result(db):
  from app.agent_work_claims import pending_settlement_notices
  from app.goals import update_goal_record

  owner, first, second = _owned_goal_claim(db)
  run = db.get(models.ChatRun, "claim-run-first")
  goal = db.get(models.ChatGoal, "claim-goal-first")

  update_goal_record(db, run, goal, 1, result="Merged as 0b44dc9d; CI green")

  row = _claim_row(db)
  assert row.completed_at is not None and row.released_at is None
  assert row.outcome == "Owning Goal completed: Merged as 0b44dc9d; CI green"
  # The follower notice is owed durably until a seam delivers it.
  [pending] = pending_settlement_notices(db, first.id)
  assert (pending.state, pending.interested_chat_ids) == (
    "completed", [second.id],
  )
  again = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Try the finished merge again",
  )
  assert again["state"] == "completed"


@pytest.mark.parametrize("run_token", ["claim-run-first", ""])
def test_stop_releases_the_goal_claims_in_the_same_writer_commit(db, run_token):
  from app.chat_writer import FinishRun, get_writer

  owner, first, second = _owned_goal_claim(db, task_status="running")
  get_writer().submit(FinishRun(
    chat_id=first.id, run_token=run_token, terminal_status="stopped",
  )).result(timeout=5)

  db.expire_all()
  assert db.get(models.ChatGoal, "claim-goal-first").status == "stopped"
  row = _claim_row(db)
  assert row.released_at is not None and row.completed_at is None
  assert "stopped" in row.outcome
  taken = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Take over the released merge",
  )
  assert (taken["state"], taken["owner_chat_id"]) == ("claimed", second.id)


def test_dismissing_an_unfinished_goal_releases_its_claims(db):
  from app.chat_writer import ClearPresentedGoal, get_writer

  _owner, first, _second = _owned_goal_claim(db, task_status="running")
  receipt = get_writer().submit(ClearPresentedGoal(
    chat_id=first.id, expected_goal_id="claim-goal-first",
    preserve_execution=False,
  )).result(timeout=5)

  assert receipt["status"] == "cleared"
  row = _claim_row(db)
  assert row.released_at is not None
  assert "dismissed" in row.outcome


def test_restart_interruption_keeps_the_claim_with_its_open_goal(db):
  from app.agent_work_claims import stage_settle_claims_with_owner
  from app.chat_writer import FinishRun, get_writer

  _owner, first, _second = _owned_goal_claim(db, task_status="running")
  get_writer().submit(FinishRun(
    chat_id=first.id, run_token="claim-run-first",
    terminal_status="interrupted",
  )).result(timeout=5)

  assert stage_settle_claims_with_owner(db, first.id) == []
  row = _claim_row(db)
  assert row.completed_at is None and row.released_at is None
  assert row.owner_chat_id == first.id


def test_goal_settlement_never_overwrites_an_explicit_finish(db):
  from app.chat_writer import FinishRun, get_writer

  owner, first, _second = _owned_goal_claim(db, task_status="running")
  finish_work(db, owner_id=owner.id, chat_id=first.id, work_key=KEY,
              outcome="Merged as 0b44dc9d", release=False)
  get_writer().submit(FinishRun(
    chat_id=first.id, run_token="claim-run-first", terminal_status="stopped",
  )).result(timeout=5)

  row = _claim_row(db)
  assert row.completed_at is not None and row.released_at is None
  assert row.outcome == "Merged as 0b44dc9d"


def test_post_commit_seam_repairs_claims_of_a_goal_ended_elsewhere(db):
  from app.agent_work_claims import stage_settle_claims_with_owner

  _owner, first, second = _owned_goal_claim(db, task_status="running")
  # A Goal ended by a path that did not settle in-commit (e.g. an upgrade).
  db.get(models.ChatGoal, "claim-goal-first").status = "stopped"
  db.commit()

  [settled] = stage_settle_claims_with_owner(db, first.id)
  db.commit()
  assert (settled.state, settled.interested_chat_ids) == (
    "released", [second.id],
  )
  assert stage_settle_claims_with_owner(db, first.id) == []


def test_claim_taken_outside_a_goal_waits_for_explicit_finish(db):
  from app.agent_work_claims import stage_settle_claims_with_owner

  owner, first, _second = _fixture(db)
  db.add(models.ChatRun(
    id="claim-run-plain", root_run_id="claim-run-plain", chat_id=first.id,
    status="running", provider="codex",
  ))
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id="claim-run-plain", work_key=KEY, summary="Merge it")

  assert stage_settle_claims_with_owner(db, first.id) == []
  assert _claim_row(db).completed_at is None


def test_racing_first_claims_leave_one_owner_and_one_follower(db):
  """Two chats insert the same key at once: the unique key picks one owner."""
  from sqlalchemy import event
  from sqlalchemy.orm import Session

  from app.database import SessionLocal

  owner, first, second = _fixture(db)
  db.add(models.ChatGoal(id="claim-goal-second", chat_id=second.id,
                         objective="Align the platform"))
  db.commit()
  raced = []

  def first_chat_wins_inside_the_gap(session, _ctx, _instances):
    pending = [
      row for row in session.new
      if isinstance(row, models.AgentWorkClaim) and row.owner_chat_id == second.id
    ]
    if not pending or raced:
      return
    raced.append(True)
    with SessionLocal() as other:
      won = claim_work(other, owner_id=owner.id, chat_id=first.id,
                       run_id="claim-run-first", work_key=KEY,
                       summary="Merge the reviewed PR")
      assert won["state"] == "claimed"

  event.listen(Session, "before_flush", first_chat_wins_inside_the_gap)
  try:
    lost = claim_work(db, owner_id=owner.id, chat_id=second.id,
                      run_id="claim-run-second", work_key=KEY,
                      summary="Merge the same reviewed PR")
  finally:
    event.remove(Session, "before_flush", first_chat_wins_inside_the_gap)

  assert raced == [True]
  assert (lost["state"], lost["owner_chat_id"]) == ("held_by_peer", first.id)
  assert db.query(models.AgentWorkClaim).count() == 1
  assert db.query(models.AgentWorkInterest).one().chat_id == second.id
