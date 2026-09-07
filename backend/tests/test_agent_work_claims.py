"""Workspace work ownership stays singular while explicit transfer remains possible."""

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
  from app.chat import _goal_handoff_is_owned

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

  assert _goal_handoff_is_owned(
    db, second.id, "claim-goal-second",
  ) is False


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
