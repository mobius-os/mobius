"""Atomic workspace ownership for work shared by otherwise independent chats."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re
import uuid

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.decision_cards import fence_owner
from app.goal_dependencies import pending_follow_predicate
from app.timeutil import now_naive_utc
from app.goal_dependency_sources import stage_follow_claim, stage_stamp_claim_outcome


# `#` and `+` appear naturally in PR references such as `mobius-os/mobius#1365`.
WORK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9:._/@#+-]{2,255}$")


def clean_work_key(value: str) -> str:
  key = value.strip()
  if not WORK_KEY_RE.fullmatch(key):
    raise ValueError(
      "work_key must be 3-256 lowercase letters, numbers, or : . _ / @ # + -"
    )
  return key


def _goal_id(db: Session, chat_id: str, run_id: str) -> str | None:
  # A turn may offer a new approval after completing its Goal. The new action
  # is chat-owned, not old work for that Goal's settlement to release.
  return db.query(models.ChatRun.goal_id).join(
    models.ChatGoal, models.ChatGoal.id == models.ChatRun.goal_id,
  ).filter(
    models.ChatRun.id == run_id,
    models.ChatRun.chat_id == chat_id,
    models.ChatGoal.chat_id == chat_id,
    models.ChatGoal.status == "open",
  ).scalar()


def _serialize(db: Session, row: models.AgentWorkClaim, state: str) -> dict:
  title = db.query(models.Chat.title).filter(
    models.Chat.id == row.owner_chat_id,
  ).scalar()
  result = {
    "id": row.id,
    "work_key": row.work_key,
    "summary": row.summary,
    "state": state,
    "owner_chat_id": row.owner_chat_id,
    "owner_name": title or row.owner_chat_id or "Deleted chat",
    "owner_goal_id": row.owner_goal_id,
    "revision": row.revision,
    "acquisition_id": row.acquisition_id,
    "notification_pending": (
      row.previous_owner_chat_id is not None
      and row.transfer_notice_revision is not None
      and row.notification_revision < row.transfer_notice_revision
    ),
    "transfer_notice_revision": row.transfer_notice_revision,
    "takeover_reason": row.takeover_reason,
    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    "outcome": row.outcome,
  }
  if state == "held_by_peer":
    result["next_action"] = (
      "Do not duplicate this exact action. Reconcile your own Goal now: "
      "continue any independent work and settle the overlapping plan task, "
      "or explicitly transfer the broader outcome. Following this claim does "
      "not transfer ownership of your whole Goal."
    )
  return result


def _follow(db: Session, row: models.AgentWorkClaim, *, chat_id: str,
            run_id: str) -> str:
  return stage_follow_claim(db, row, chat_id=chat_id, run_id=run_id)


def _stage_goal_owner_change(db: Session, row: models.AgentWorkClaim, *,
                             previous: str, previous_run_id: str,
                             previous_goal_id: str | None) -> str:
  """Move executor responsibility, preserving every other exact Goal edge.

  Caller holds the tenant graph fence and has staged the claim's new owner.
  A same-chat Goal rebind is the same graph transition as a cross-chat transfer.
  """
  goal_id, chat_id = row.owner_goal_id, row.owner_chat_id
  now = now_naive_utc()
  # Existing subscribers change source owner too. Check every affected edge,
  # not just the reverse link newly created for the former executor.
  from app.goal_dependency_sources import _direct_cycle
  if goal_id:
    follower_goals = db.query(models.GoalDependency.waiting_goal_id).filter(
      models.GoalDependency.target_acquisition_id == row.acquisition_id,
      models.GoalDependency.waiting_goal_id != goal_id,
      pending_follow_predicate(),
    ).all()
    if any(_direct_cycle(db, waiting_goal_id=follower_goal, source_goal_id=goal_id)
           for (follower_goal,) in follower_goals):
      db.rollback()
      raise ValueError("Transfer would create a direct Goal dependency cycle.")
  db.query(models.GoalDependency).filter(
    models.GoalDependency.target_acquisition_id == row.acquisition_id,
    pending_follow_predicate(),
  ).update({models.GoalDependency.target_chat_id: chat_id}, synchronize_session=False)
  # Only the acquiring Goal becomes executor. Other Goals in the same chat
  # remain subscribed, even when the acquisition completes before transfer out.
  db.query(models.GoalDependency).filter(
    models.GoalDependency.target_acquisition_id == row.acquisition_id,
    models.GoalDependency.waiting_goal_id == goal_id,
    pending_follow_predicate(),
  ).update({models.GoalDependency.owner_suppressed_at: now}, synchronize_session=False)
  previous_follow_status = "no_open_top_level_goal"
  if previous_goal_id and previous_goal_id != goal_id:
    previous_follow_status = stage_follow_claim(db, row, chat_id=previous,
      run_id=previous_run_id, waiting_goal_id=previous_goal_id, restore_owner_follow=True)
    if previous_follow_status not in {"followed", "result_owed", "no_open_top_level_goal", "withdrawn"}:
      db.rollback()
      if previous_follow_status == "direct_cycle":
        raise ValueError("Transfer would create a direct Goal dependency cycle.")
      raise ValueError("Transfer cannot preserve the former Goal's follow: " + previous_follow_status)
  return previous_follow_status


def claim_work(
  db: Session,
  *,
  owner_id: int,
  chat_id: str,
  run_id: str,
  work_key: str,
  summary: str,
  takeover_reason: str | None = None,
  expected_owner_chat_id: str | None = None,
) -> dict:
  """Acquire first claim, observe its owner, or deliberately transfer it."""
  key = clean_work_key(work_key)
  clean_summary = summary.strip()
  if not clean_summary or len(clean_summary) > 500:
    raise ValueError("summary must be 1-500 characters")
  reason = takeover_reason.strip() if takeover_reason else None
  if reason is not None and not 10 <= len(reason) <= 1000:
    raise ValueError("takeover_reason must be 10-1000 characters")
  goal_id = _goal_id(db, chat_id, run_id)

  row = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_id == owner_id,
    models.AgentWorkClaim.work_key == key,
  ).first()
  if row is None:
    row = models.AgentWorkClaim(
      id=str(uuid.uuid4()), owner_id=owner_id, work_key=key,
      summary=clean_summary, owner_chat_id=chat_id, owner_run_id=run_id,
      owner_goal_id=goal_id, acquisition_id=uuid.uuid4().hex,
    )
    db.add(row)
    try:
      db.commit()
      db.refresh(row)
      return _serialize(db, row, "claimed")
    except IntegrityError:
      # Another chat won the same first-claim race.
      db.rollback()
      row = db.query(models.AgentWorkClaim).filter(
        models.AgentWorkClaim.owner_id == owner_id,
        models.AgentWorkClaim.work_key == key,
      ).one()

  if row.completed_at is not None:
    return _serialize(db, row, "completed")
  if row.released_at is not None:
    previous_revision = row.revision
    next_revision = previous_revision + 1
    now = now_naive_utc()
    changed = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.id == row.id,
      models.AgentWorkClaim.revision == previous_revision,
      models.AgentWorkClaim.released_at.is_not(None),
      models.AgentWorkClaim.completed_at.is_(None),
    ).update({
      models.AgentWorkClaim.summary: clean_summary,
      models.AgentWorkClaim.owner_chat_id: chat_id,
      models.AgentWorkClaim.owner_run_id: run_id,
      models.AgentWorkClaim.owner_goal_id: goal_id,
      models.AgentWorkClaim.previous_owner_chat_id: None,
      models.AgentWorkClaim.takeover_reason: None,
      models.AgentWorkClaim.released_at: None,
      models.AgentWorkClaim.outcome: None,
      models.AgentWorkClaim.acquisition_id: uuid.uuid4().hex,
      models.AgentWorkClaim.claimed_at: now,
      models.AgentWorkClaim.updated_at: now,
      models.AgentWorkClaim.revision: next_revision,
      models.AgentWorkClaim.notification_revision: next_revision,
      models.AgentWorkClaim.transfer_notice_revision: None,
    }, synchronize_session=False)
    if changed != 1:
      db.rollback()
      raise ValueError("The released claim changed; inspect it before retrying.")
    db.commit()
    row = db.get(models.AgentWorkClaim, row.id)
    db.refresh(row)
    return _serialize(db, row, "claimed")
  if row.owner_chat_id == chat_id:
    # Refreshing ownership from a different Goal in the same chat also changes
    # graph edges. Serialize it with follows/transfers and reject reverse edges.
    fence_owner(db, owner_id)
    db.refresh(row)
    if row.owner_chat_id != chat_id:
      db.rollback()
      raise ValueError("The claim changed while refreshing; inspect it before retrying.")
    previous_revision = row.revision
    previous_goal_id, previous_run_id = row.owner_goal_id, row.owner_run_id
    changed = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.id == row.id,
      models.AgentWorkClaim.owner_chat_id == chat_id,
      models.AgentWorkClaim.owner_goal_id == row.owner_goal_id,
      models.AgentWorkClaim.revision == previous_revision,
      models.AgentWorkClaim.completed_at.is_(None),
      models.AgentWorkClaim.released_at.is_(None),
    ).update({
      models.AgentWorkClaim.summary: clean_summary,
      models.AgentWorkClaim.owner_run_id: run_id,
      models.AgentWorkClaim.owner_goal_id: goal_id or row.owner_goal_id,
      models.AgentWorkClaim.updated_at: now_naive_utc(),
    }, synchronize_session=False)
    if changed != 1:
      db.rollback()
      raise ValueError("The claim changed while refreshing; inspect it before retrying.")
    db.expire(row)
    db.refresh(row)
    if row.owner_goal_id != previous_goal_id:
      _stage_goal_owner_change(db, row, previous=chat_id,
        previous_run_id=previous_run_id, previous_goal_id=previous_goal_id)
    db.commit()
    db.refresh(row)
    pending_transfer = (
      row.previous_owner_chat_id is not None
      and row.transfer_notice_revision is not None
      and row.notification_revision < row.transfer_notice_revision
    )
    result = _serialize(db, row, "transferred" if pending_transfer else "owned")
    if pending_transfer:
      result["previous_owner_chat_id"] = row.previous_owner_chat_id
    return result
  if reason is None:
    follow_status = _follow(db, row, chat_id=chat_id, run_id=run_id)
    db.commit()
    db.refresh(row)
    result = _serialize(db, row, "held_by_peer")
    result["follow_status"] = follow_status
    if follow_status == "result_owed":
      result["next_action"] = (
        "Do not duplicate this exact action. Your Goal already follows this "
        "acquisition; its result is owed and may be incorporated on its next turn."
      )
    elif follow_status != "followed":
      result["next_action"] = (
        "Do not duplicate this exact action. No new Goal follow was created "
        f"({follow_status}); report the blocker to your coordinator or "
        "reconcile your own work."
      )
    return result
  if expected_owner_chat_id != row.owner_chat_id:
    raise ValueError(
      "The expected owner changed; inspect the current claim before transfer."
    )

  # Transfer may create a reverse Goal edge, so share the tenant-scoped graph
  # fence with ordinary follow registration before checking direct cycles.
  fence_owner(db, owner_id)
  db.expire(row)
  db.refresh(row)
  if row.owner_chat_id != expected_owner_chat_id:
    db.rollback()
    raise ValueError("The expected owner changed; inspect the current claim before transfer.")
  previous = row.owner_chat_id
  previous_run_id = row.owner_run_id
  previous_goal_id = row.owner_goal_id
  previous_revision = row.revision
  now = now_naive_utc()
  # Check the resulting graph in _stage_goal_owner_change under this fence:
  # the former owner's exact edge may already be withdrawn, not restored.
  changed = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.id == row.id,
    models.AgentWorkClaim.owner_chat_id == expected_owner_chat_id,
    models.AgentWorkClaim.revision == previous_revision,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).update({
    models.AgentWorkClaim.summary: clean_summary,
    models.AgentWorkClaim.previous_owner_chat_id: previous,
    models.AgentWorkClaim.owner_chat_id: chat_id,
    models.AgentWorkClaim.owner_run_id: run_id,
    models.AgentWorkClaim.owner_goal_id: goal_id,
    models.AgentWorkClaim.takeover_reason: reason,
    models.AgentWorkClaim.updated_at: now,
    models.AgentWorkClaim.revision: previous_revision + 1,
    models.AgentWorkClaim.transfer_notice_revision: previous_revision + 1,
  }, synchronize_session=False)
  if changed != 1:
    db.rollback()
    raise ValueError("The claim changed during transfer; inspect it before retrying.")
  db.expire(row)
  db.refresh(row)
  previous_follow_status = _stage_goal_owner_change(db, row, previous=previous,
    previous_run_id=previous_run_id, previous_goal_id=previous_goal_id)
  db.commit()
  db.refresh(row)
  result = _serialize(db, row, "transferred")
  # Settlement may have committed between transfer and refresh. The event's
  # recipient, identity and obligation belong to the transfer transaction,
  # not the claim's later state.
  result["transfer_notice_revision"] = previous_revision + 1
  result["notification_pending"] = True
  result["previous_owner_chat_id"] = previous
  result["previous_follow_status"] = previous_follow_status
  return result


@dataclass(frozen=True)
class ReleasedClaim:
  """One open exact action settled because its owner can no longer act."""

  claim_id: str
  work_key: str
  revision: int
  state: str = "released"
  outcome: str = ""


_DELETED_OWNER_OUTCOME = "Owning chat was deleted before this action completed."


def _stage_settle(
  db: Session, row: models.AgentWorkClaim, *, complete: bool, outcome: str,
) -> ReleasedClaim | None:
  """Settle one still-open claim in the caller's txn, fenced by revision.

  A concurrent explicit finish/transfer wins: the revision fence makes this a
  no-op instead of overwriting an outcome the owner already recorded.
  """
  now = now_naive_utc()
  claim_id, work_key = row.id, row.work_key
  previous_revision = row.revision
  owner_chat_id = row.owner_chat_id
  changed = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.id == claim_id,
    models.AgentWorkClaim.owner_chat_id == owner_chat_id,
    models.AgentWorkClaim.revision == previous_revision,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).update({
    models.AgentWorkClaim.outcome: outcome[:1000],
    models.AgentWorkClaim.updated_at: now,
    models.AgentWorkClaim.revision: previous_revision + 1,
    (
      models.AgentWorkClaim.completed_at if complete
      else models.AgentWorkClaim.released_at
    ): now,
  }, synchronize_session=False)
  if changed != 1:
    return None
  stage_stamp_claim_outcome(
    db, row, ("completed" if complete else
              "owner_deleted" if outcome == _DELETED_OWNER_OUTCOME else "released"),
    outcome[:1000],
  )
  db.expire(row)
  return ReleasedClaim(
    claim_id=claim_id,
    work_key=work_key,
    revision=previous_revision + 1,
    state="completed" if complete else "released",
    outcome=outcome[:1000],
  )


def _open_claims_for_chat(db: Session, chat_id: str):
  return db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_chat_id == chat_id,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).all()


def stage_release_claims_for_chat(
  db: Session, chat_id: str,
) -> list[ReleasedClaim]:
  """Release a deleted chat's unfinished exact actions in the caller's txn.

  A chat owns its promised Goal, but each workspace claim owns only one shared
  action. Deleting the chat ends both responsibilities: followers may reclaim
  the action, while completed claims remain untouched as idempotency history.
  The caller commits the release atomically with the chat tombstone and then
  nudges exact-Goal dependency delivery.
  """
  released = [
    settled for row in _open_claims_for_chat(db, chat_id)
    if (settled := _stage_settle(
      db, row, complete=False, outcome=_DELETED_OWNER_OUTCOME,
    )) is not None
  ]
  db.flush()
  return released


def _goal_outcome(
  status: str, result: str | None, finished: bool,
) -> tuple[bool, str]:
  """Map an ended Goal to (complete, outcome) for one claim it still owns.

  Only a claim the completing Goal names as finished completes. A completed
  claim is terminal for its key, so an unnamed one (declined, deferred, or
  simply not done) is released with the Goal result for followers to judge.
  """
  text = " ".join(str(result or "").split())
  if status == "completed" and finished:
    return True, f"Owning Goal completed: {text}" if text else (
      "Owning Goal completed."
    )
  if status == "completed":
    return False, (
      "Owning Goal completed without recording this action as done"
      + (f": {text}" if text else ".")
    )
  return False, (f"Owning Goal was {status} before this action finished"
                 + (f": {text}" if text else "."))


def stage_settle_goal_claims(
  db: Session, *, chat_id: str, goal_id: str, status: str,
  result: str | None = None, finished_keys: Iterable[str] = (),
) -> list[ReleasedClaim]:
  """Settle one ended Goal's still-open claims in the caller's transaction.

  Called by every lifecycle write that moves a Goal out of ``open`` so the
  claim and the Goal end atomically; a restart can never observe an ended
  Goal that still owns an open exact action. A completing Goal completes only
  the claims it names in ``finished_keys``; every other open claim, and every
  claim of a stopped or dismissed Goal, is released so followers may reclaim
  it. The caller may nudge exact-Goal result delivery after commit; immutable
  subscribed outcomes do not depend on that nudge.
  """
  if not goal_id or status == "open":
    return []
  finished = set(finished_keys)
  settled = []
  # A claim the chat took before this Goal existed (an approval answered
  # before promote_goal) stays Goal-less; completion finishes it only when
  # the Goal names it, and never releases it otherwise.
  owned_by_goal = models.AgentWorkClaim.owner_goal_id == goal_id
  named_chat_claim = and_(
    models.AgentWorkClaim.owner_goal_id.is_(None),
    models.AgentWorkClaim.work_key.in_(finished),
  )
  for row in db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_chat_id == chat_id,
    or_(owned_by_goal, named_chat_claim) if finished else owned_by_goal,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).all():
    complete, outcome = _goal_outcome(status, result, row.work_key in finished)
    item = _stage_settle(db, row, complete=complete, outcome=outcome)
    if item is not None:
      settled.append(item)
  db.flush()
  return settled


def open_claim_keys(
  db: Session, *, chat_id: str, goal_id: str | None = None,
) -> set[str]:
  """Exact actions this chat (or one of its Goals) still owns."""
  query = db.query(models.AgentWorkClaim.work_key).filter(
    models.AgentWorkClaim.owner_chat_id == chat_id,
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  )
  if goal_id is not None:
    query = query.filter(models.AgentWorkClaim.owner_goal_id == goal_id)
  return {key for (key,) in query.all()}


def completable_claim_keys(db: Session, *, chat_id: str, goal_id: str) -> set[str]:
  """Open keys a completing Goal may name: its own and the chat's Goal-less ones."""
  query = db.query(models.AgentWorkClaim.work_key).filter(
    models.AgentWorkClaim.owner_chat_id == chat_id,
    or_(
      models.AgentWorkClaim.owner_goal_id == goal_id,
      models.AgentWorkClaim.owner_goal_id.is_(None),
    ),
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  )
  return {key for (key,) in query.all()}


_HELD_KEYS_SHOWN = 10


def held_claims_hint(keys: set[str]) -> str:
  """Name the exact keys an owner holds, for a refusal of a mistyped key.

  Agents settle claims long after taking them and tend to retype a long key
  from memory (a short commit hash, a dropped suffix). Work keys are exact
  identities, so the refusal never guesses a match; it shows the real keys so
  the next call can use one verbatim.
  """
  if not keys:
    return "No open work claims are held here."
  shown = sorted(keys)[:_HELD_KEYS_SHOWN]
  more = len(keys) - len(shown)
  suffix = f" (and {more} more)" if more else ""
  return "Open claims held here: " + ", ".join(shown) + suffix + "."


def stage_settle_claims_with_owner(
  db: Session, chat_id: str,
) -> list[ReleasedClaim]:
  """Idempotent repair: settle claims whose owning Goal or chat has ended.

  Lifecycle writes normally settle claims atomically through
  ``stage_settle_goal_claims``. This re-derives the same result from durable
  Goal state at each post-commit seam, so a Goal ended by any older or
  unforeseen path still cannot strand its claims. Claims taken outside a Goal
  have no Goal lifecycle; they wait for an explicit finish or chat deletion.
  """
  deleted = db.query(models.Chat.deleted_at).filter(
    models.Chat.id == chat_id,
  ).scalar()
  if deleted is not None:
    return stage_release_claims_for_chat(db, chat_id)
  goal_ids = {
    goal_id for (goal_id,) in db.query(models.AgentWorkClaim.owner_goal_id).filter(
      models.AgentWorkClaim.owner_chat_id == chat_id,
      models.AgentWorkClaim.owner_goal_id.is_not(None),
      models.AgentWorkClaim.completed_at.is_(None),
      models.AgentWorkClaim.released_at.is_(None),
    ).all()
  }
  if not goal_ids:
    return []
  settled: list[ReleasedClaim] = []
  for goal in db.query(models.ChatGoal).filter(
    models.ChatGoal.chat_id == chat_id,
    models.ChatGoal.id.in_(goal_ids),
    models.ChatGoal.status != "open",
  ).all():
    settled.extend(stage_settle_goal_claims(
      db, chat_id=chat_id, goal_id=goal.id, status=goal.status,
      result=goal.result,
    ))
  return settled


def finish_work(
  db: Session,
  *,
  owner_id: int,
  chat_id: str,
  work_key: str,
  outcome: str,
  release: bool,
) -> dict:
  key = clean_work_key(work_key)
  clean_outcome = outcome.strip()
  if not clean_outcome or len(clean_outcome) > 1000:
    raise ValueError("outcome must be 1-1000 characters")
  row = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_id == owner_id,
    models.AgentWorkClaim.work_key == key,
  ).first()
  if row is None:
    raise ValueError(
      "No work claim exists for this key. "
      + held_claims_hint(open_claim_keys(db, chat_id=chat_id))
    )
  if row.owner_chat_id != chat_id:
    raise ValueError("Only the current claim owner can finish or release it.")
  if (
    row.completed_at is None and row.released_at is None
    and _stage_settle(
      db, row, complete=not release, outcome=clean_outcome,
    ) is None
  ):
    db.rollback()
    raise ValueError("The claim changed while finishing; inspect it before retrying.")
  db.commit()
  db.refresh(row)
  state = "completed" if row.completed_at is not None else "released"
  return _serialize(db, row, state)


def acknowledge_transfer_notice(
  db: Session, *, claim_id: str, revision: int,
) -> None:
  row = db.get(models.AgentWorkClaim, claim_id)
  if row is None or row.transfer_notice_revision != revision:
    return
  if row.notification_revision < revision:
    row.notification_revision = revision
  db.commit()
