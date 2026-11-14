"""Claim-acquisition source facts for Goal dependencies.

A claim's current row may be reacquired; resolved dependency outcomes are the
immutable record of the acquisition that a Goal actually followed.
"""
from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.decision_cards import fence_owner
from app.goal_dependencies import pending_follow_predicate
from app.timeutil import now_naive_utc


def _eligible_goal(db: Session, chat_id: str, run_id: str, waiting_goal_id=None):
  if db.query(models.Chat.id).filter(
    models.Chat.id == chat_id, models.Chat.deleted_at.is_(None),
  ).first() is None:
    return None
  if db.query(models.Delegation.id).filter(
    models.Delegation.child_chat_id == chat_id,
  ).first():
    return None
  if waiting_goal_id is not None:
    return db.query(models.ChatGoal).filter(
      models.ChatGoal.id == waiting_goal_id, models.ChatGoal.chat_id == chat_id,
      models.ChatGoal.status == 'open',
    ).first()
  return db.query(models.ChatGoal).join(
    models.ChatRun, models.ChatRun.goal_id == models.ChatGoal.id,
  ).filter(
    models.ChatRun.id == run_id,
    models.ChatRun.chat_id == chat_id,
    models.ChatGoal.chat_id == chat_id,
    models.ChatGoal.status == 'open',
  ).first()


def _direct_cycle(db: Session, *, waiting_goal_id: str, source_goal_id: str | None) -> bool:
  if source_goal_id is None:
    return False
  if source_goal_id == waiting_goal_id:
    return True
  pending = db.query(models.GoalDependency.id).filter(
    models.GoalDependency.waiting_goal_id == source_goal_id,
    pending_follow_predicate(),
  )
  claims = pending.join(models.AgentWorkClaim,
    models.AgentWorkClaim.id == models.GoalDependency.target_claim_id).filter(
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.AgentWorkClaim.owner_goal_id == waiting_goal_id,
    models.AgentWorkClaim.acquisition_id == models.GoalDependency.target_acquisition_id)
  cards = pending.join(models.SavedDecisionCard,
    (models.SavedDecisionCard.chat_id == models.GoalDependency.target_chat_id) &
    (models.SavedDecisionCard.question_id == models.GoalDependency.target_question_id)).filter(
    models.GoalDependency.target_kind == 'owner_card',
    models.SavedDecisionCard.source_goal_id == waiting_goal_id)
  return claims.first() is not None or cards.first() is not None


MAX_OUTSTANDING_DEPENDENCIES_PER_GOAL = 64


def outstanding_dependency_count(db: Session, waiting_goal_id: str) -> int:
  """Count all live follows, including resolved results still owed to a Goal."""
  return db.query(func.count(models.GoalDependency.id)).filter(
    models.GoalDependency.waiting_goal_id == waiting_goal_id,
    models.GoalDependency.withdrawn_at.is_(None),
    models.GoalDependency.owner_suppressed_at.is_(None),
    models.GoalDependency.result_disposition.is_(None),
  ).scalar() or 0


def stage_follow_claim(db: Session, claim: models.AgentWorkClaim, *,
                       chat_id: str, run_id: str, waiting_goal_id=None,
                       restore_owner_follow: bool = False) -> str:
  """Register under the claim's write lock; return followed or a truthful reason.

  The CAS write serializes with settlement/transfer, including on SQLite where
  SELECT FOR UPDATE alone is ineffective. The caller commits both together.
  """
  # Lock graph ownership, then re-read: an ORM object from the collision read
  # may predate a same-chat Goal rebind even when its acquisition is unchanged.
  expected_acquisition = claim.acquisition_id
  fence_owner(db, claim.owner_id)
  db.refresh(claim)
  if claim.acquisition_id != expected_acquisition:
    raise ValueError('The claim changed while following; inspect it before retrying.')
  changed = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.id == claim.id,
    models.AgentWorkClaim.revision == claim.revision,
    models.AgentWorkClaim.acquisition_id == expected_acquisition,
    models.AgentWorkClaim.released_at.is_(None),
    models.AgentWorkClaim.completed_at.is_(None),
  ).update({models.AgentWorkClaim.updated_at: now_naive_utc()}, synchronize_session=False)
  if changed != 1:
    raise ValueError('The claim changed while following; inspect it before retrying.')
  goal = _eligible_goal(db, chat_id, run_id, waiting_goal_id)
  if goal is None:
    return 'no_open_top_level_goal'
  # An ownership change can preserve a former Goal in the same chat; the
  # exact executing Goal itself can never subscribe to its own acquisition.
  if claim.owner_goal_id == goal.id or (
      claim.owner_chat_id == chat_id and not restore_owner_follow):
    return 'self_dependency'
  if not claim.acquisition_id:
    return 'unidentified_acquisition'
  target_key = f'claim:{claim.acquisition_id}'
  existing = db.query(models.GoalDependency).filter(
    models.GoalDependency.waiting_goal_id == goal.id,
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.GoalDependency.target_acquisition_id == claim.acquisition_id,
  ).order_by(models.GoalDependency.created_at.desc(),
             models.GoalDependency.id.desc()).first()
  if existing is not None:
    if existing.withdrawn_at is not None:
      return 'withdrawn'
    if existing.result_disposition is not None:
      # Recovery is a fact about the former attempt, not a terminal outcome
      # of the acquisition. A later attempt may follow only if the source is
      # progressing again. Transfer separately preserves a former owner's
      # follow, even when that owner previously received this recovery fact.
      if (existing.result_disposition == 'delivered'
          and isinstance(existing.outcome_json, dict)
          and existing.outcome_json.get('kind') == 'owner_not_progressing'):
        if not restore_owner_follow:
          current_source = models.GoalDependency(
            id=existing.id, target_kind='claim_acquisition',
            target_claim_id=claim.id, target_acquisition_id=claim.acquisition_id)
          blocker = dependency_source_blocker(db, current_source)
          if blocker is not None:
            return blocker
        existing = None
        target_key = f'{target_key}:{uuid4().hex}'
      else:
        return existing.result_disposition
    if existing is not None:
      if existing.owner_suppressed_at is None:
        return 'followed' if existing.resolved_at is None else 'result_owed'
      if not restore_owner_follow or existing.resolved_at is not None:
        return 'owner_suppressed'
  # Only adding or restoring an edge can create a cycle. In particular, an
  # explicitly withdrawn follow stays withdrawn across ownership changes.
  if _direct_cycle(db, waiting_goal_id=goal.id,
                   source_goal_id=claim.owner_goal_id):
    return 'direct_cycle'
  if outstanding_dependency_count(db, goal.id) >= MAX_OUTSTANDING_DEPENDENCIES_PER_GOAL:
    return 'dependency_limit'
  if existing is not None:
    existing.owner_suppressed_at = None
    existing.target_chat_id = claim.owner_chat_id
    db.flush()
    return 'followed'
  source_run = db.get(models.ChatRun, run_id)
  created_by_run_id = (run_id if source_run is not None and source_run.chat_id == chat_id
    and source_run.goal_id == goal.id else None)
  db.add(models.GoalDependency(
    id=str(uuid4()), waiting_goal_id=goal.id, waiting_chat_id=chat_id,
    created_by_run_id=created_by_run_id, target_kind='claim_acquisition',
    target_key=target_key, target_claim_id=claim.id,
    target_acquisition_id=claim.acquisition_id,
    target_chat_id=claim.owner_chat_id,
  ))
  db.flush()
  return 'followed'


def stage_stamp_claim_outcome(db: Session, row: models.AgentWorkClaim,
                              kind: str, outcome: str) -> int:
  """Stamp every live follower before the claim settlement transaction commits."""
  if kind not in ('completed', 'released', 'owner_deleted'):
    raise ValueError('Invalid claim outcome kind')
  now = now_naive_utc()
  payload = {
    'kind': kind, 'work_key': row.work_key, 'summary': row.summary,
    'outcome': outcome, 'source_chat_id': row.owner_chat_id,
    'source_chat_title': (db.query(models.Chat.title).filter(
      models.Chat.id == row.owner_chat_id).scalar() or row.owner_chat_id),
  }
  return db.query(models.GoalDependency).filter(
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.GoalDependency.target_acquisition_id == row.acquisition_id,
    pending_follow_predicate(),
  ).update({models.GoalDependency.outcome_json: payload,
            models.GoalDependency.resolved_at: now}, synchronize_session=False)


def _run_diagnostics(run: models.ChatRun | None) -> dict | None:
  return ({key: getattr(run, key) for key in (
    'id', 'goal_id', 'root_run_id', 'status', 'provider_execution_admitted',
  )} if run is not None else None)


def _native_handoff_diagnostics(handoffs):
  # Diagnostic samples, not limits on eligible work. Keep persisted recovery
  # facts bounded even when a Goal has many helpers or accepted queue entries.
  sample_size = 8
  def sample(rows):
    return {'count': len(rows), 'sample': [
      item if isinstance(item, str) else asdict(item) for item in rows[:sample_size]
    ]}
  return {
    'running': handoffs.running, 'failed_attempt': handoffs.failed_attempt,
    'progress_owned': handoffs.progress_owned,
    'card': asdict(handoffs.card) if handoffs.card else None,
    'park': asdict(handoffs.park) if handoffs.park else None,
    'waits': sample(handoffs.waits), 'helpers': sample(handoffs.helpers),
    'queued': sample(handoffs.queued),
  }


def dependency_source_blocker(db: Session, row: models.GoalDependency, *,
                              _seen=None, diagnostics: dict | None = None) -> str | None:
  """Report why a pending claim source cannot be trusted to progress.

  This is observational, not a polling or takeover operation. Resolved and
  withdrawn rows already have their own authoritative outcome/disposition.
  A caller stamping recovery may collect bounded exact-owner evidence from
  this same observation, without reading transcripts or borrowing a newer Goal.
  """
  if (row.resolved_at is not None or row.withdrawn_at is not None
      or row.result_disposition is not None or row.owner_suppressed_at is not None):
    return None
  if row.target_kind != 'claim_acquisition':
    return None
  # Share traversal state across branches of a diamond as well as cycles.
  seen = _seen if _seen is not None else set()
  if row.id in seen:
    return 'owner_not_progressing'
  seen.add(row.id)
  claim = db.get(models.AgentWorkClaim, row.target_claim_id) if row.target_claim_id else None
  if diagnostics is not None:
    diagnostics['claim'] = ({key: getattr(claim, key) for key in (
      'id', 'acquisition_id', 'revision', 'owner_chat_id', 'owner_run_id', 'owner_goal_id',
    )} if claim is not None else None)
  if claim is None:
    return 'source_missing'
  run = (db.get(models.ChatRun, claim.owner_run_id)
    if diagnostics is not None or claim.owner_goal_id is None else None)
  goal = db.get(models.ChatGoal, claim.owner_goal_id) if claim.owner_goal_id else None
  if diagnostics is not None:
    diagnostics['owner_run'] = _run_diagnostics(run)
    diagnostics['owner_goal'] = {'id': goal.id, 'status': goal.status} if goal else None
    latest = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == claim.owner_chat_id,
    ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    diagnostics['latest_chat_run'] = _run_diagnostics(latest)
  if claim.acquisition_id != row.target_acquisition_id:
    return 'source_missing'
  if claim.completed_at is not None or claim.released_at is not None:
    return 'source_failed'
  chat = db.get(models.Chat, claim.owner_chat_id) if claim.owner_chat_id else None
  if chat is None or chat.deleted_at is not None:
    return 'owner_deleted'
  from app.chat_handoffs import native_goal_handoffs, native_handoffs
  handoffs = None
  if goal is not None and (goal.status == 'open' or diagnostics is not None):
    handoffs = native_goal_handoffs(db, goal)
  elif claim.owner_goal_id is None and run is not None:
    handoffs = native_handoffs(db, chat.id, goal_id=None, root_run_id=run.root_run_id or run.id)
  if diagnostics is not None:
    diagnostics['native_handoffs'] = _native_handoff_diagnostics(handoffs) if handoffs else None
  if claim.owner_goal_id:
    if goal is None or goal.status != 'open':
      return 'source_held' if goal is not None and goal.status == 'stopped' else 'source_failed'
    if handoffs.progress_owned:
      return None
    # FinishRun commits before PromotePending admits the one bounded clean
    # settlement. A reconciler in that gap must not declare the source dead.
    # Only the chat's latest exact Goal attempt can own this allowance; an
    # already-attempted settlement, failed run, or later foreign Goal cannot.
    latest = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == goal.chat_id,
    ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    if (latest is not None and latest.goal_id == goal.id
        and latest.status == 'completed'
        and latest.provider_execution_admitted is True
        and latest.initiated_by_app_id is None
        and chat.dismissed_goal_id != goal.id):
      from app.continuations import recovery_attempted
      if not recovery_attempted(db, latest, reason='goal_settlement'):
        return None
    # Follow a finite chain of real sources, not the existence of a stale row.
    # Direct cycles are rejected on writes. Longer cycles remain representable,
    # but a detected chain of stalled executors is recovery, not automatic work.
    from app.goal_dependencies import dependency_resume_blocker
    for dependency in db.query(models.GoalDependency).filter(
      models.GoalDependency.waiting_goal_id == goal.id,
      models.GoalDependency.result_disposition.is_(None),
      models.GoalDependency.withdrawn_at.is_(None),
      models.GoalDependency.owner_suppressed_at.is_(None),
    ).all():
      if dependency.resolved_at is not None:
        if dependency_resume_blocker(db, dependency) not in {
          'manual_resume', 'goal_closed', 'goal_held', 'chat_deleted', 'disposed', 'restart',
        }:
          return None
      elif dependency_source_blocker(db, dependency, _seen=seen) is None:
        return None
    return 'owner_not_progressing'

  else:
    if run is None:
      return 'owner_not_progressing'
    if handoffs.progress_owned:
      return None
    return 'owner_not_progressing'


def stage_reconcile_claim_sources(db: Session) -> int:
  """Resolve only conclusively nonviable claim sources in the caller's txn.

  A failed source Goal remains viable when a card, Wait, helper, or direct
  dependency still owns progress; otherwise its failure is surfaced once.
  Production calls this only inside ReconcileClaimSources on the chat writer,
  serialized with answer commits and queue-to-run promotion. No cross-chat
  asyncio lock is acquired, and no elapsed-time deadline is assumed.
  """
  rows = db.query(models.GoalDependency).filter(
    models.GoalDependency.target_kind == 'claim_acquisition',
    pending_follow_predicate(),
  ).all()
  settled = 0
  for row in rows:
    blocker = dependency_source_blocker(db, row)
    if blocker is None:
      continue
    claim = db.get(models.AgentWorkClaim, row.target_claim_id) if row.target_claim_id else None
    diagnostics = {}
    if claim is not None:
      # Only a stalled candidate needs a source write lock. Re-read after the
      # lock so a simultaneous finish/transfer wins over stale observation.
      changed = db.query(models.AgentWorkClaim).filter(
        models.AgentWorkClaim.id == claim.id,
        models.AgentWorkClaim.revision == claim.revision,
      ).update({models.AgentWorkClaim.updated_at: now_naive_utc()},
               synchronize_session=False)
      if changed != 1:
        continue
      db.expire(claim)
      db.refresh(claim)
    blocker = dependency_source_blocker(db, row, diagnostics=diagnostics)
    if blocker is None:
      continue
    settled += db.query(models.GoalDependency).filter(
      models.GoalDependency.id == row.id,
      pending_follow_predicate(),
    ).update({models.GoalDependency.outcome_json: {
        'kind': 'owner_not_progressing', 'reason': blocker,
        'target_acquisition_id': row.target_acquisition_id,
        'source_diagnostics': diagnostics,
        'source_chat_id': claim.owner_chat_id if claim else row.target_chat_id,
        'source_chat_title': (db.query(models.Chat.title).filter(
          models.Chat.id == claim.owner_chat_id).scalar() if claim else None),
        'summary': claim.summary if claim else 'Shared work',
        'work_key': claim.work_key if claim else None,
      }, models.GoalDependency.resolved_at: now_naive_utc()},
      synchronize_session=False)
  return settled
