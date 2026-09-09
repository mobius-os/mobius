"""Permanent cleanup for chats whose owner-deletion window has expired.

Only chats explicitly tombstoned through the delete lifecycle belong here.
Ordinary reads must not infer abandonment from age or content, and notification
history follows its own product lifecycle rather than piggybacking on chat
listing.
"""

import shutil
from pathlib import Path

from sqlalchemy import literal, or_, select
from sqlalchemy.orm import Session

from app import models, questions
from app.chat import forget_chat
from app.config import get_settings
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


def _purge_chat_storage(chat_id: str) -> None:
  """Remove data derived from a chat after its recovery window has closed."""
  data_dir = Path(get_settings().data_dir)
  shutil.rmtree(data_dir / "chats" / chat_id, ignore_errors=True)
  shutil.rmtree(
    data_dir / "agent-browser-profiles" / f"chat-{chat_id}",
    ignore_errors=True,
  )
  shutil.rmtree(
    data_dir / "shared" / "memory" / "chats" / chat_id,
    ignore_errors=True,
  )


def purge_expired_chat_tombstones(db: Session) -> list[str]:
  """Permanently remove chats explicitly deleted more than seven days ago.

  The candidate query selects IDs only, so this lifecycle sweep never decodes
  transcript or pending-message JSON. Database deletion commits before
  best-effort process/filesystem cleanup; a failed transaction therefore
  cannot erase recoverable data outside the database.
  """
  # Release expired project/chat pairs first. The project sweep commits its row
  # deletion before touching owned roots, so this chat query can only see a
  # linked chat after the project's recovery contract has ended durably.
  from app.project_retention import purge_expired_project_tombstones
  purge_expired_project_tombstones(db)

  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  expired_chat_ids = select(models.Chat.id).where(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
    # A project tombstone keeps its root + chat collection recoverable together.
    # A live project may still outlive one independently deleted chat, so only
    # a recoverable *deleted* project blocks that chat here. Once the expired
    # project row is purged above, its chats become ordinary cleanup candidates.
    or_(
      models.Chat.project_id.is_(None),
      ~models.Chat.project_id.in_(
        select(models.Project.id).where(models.Project.deleted_at.isnot(None))
      ),
    ),
  )
  chat_ids = [
    chat_id for chat_id in db.scalars(expired_chat_ids).all()
  ]
  if not chat_ids:
    return []

  # Delegations share the same physical ChatRun supervision as their chats.
  # The soft-delete boundary normally cancels them, but a
  # timed-out provider (or an older tombstone from before that rule) must keep
  # the entire parent/child graph recoverable until it is truly quiescent.
  from app.delegations import active_delegation_ids_for_chat
  blocked_ids = {
    chat_id
    for chat_id in chat_ids
    if active_delegation_ids_for_chat(db, chat_id)
  }
  # Defensive: never hard-purge a tombstoned chat that still owns a NONTERMINAL
  # run (running / parked / resume_pending). delete_chat normally stops runs and
  # cancels waits, but a timed-out provider stop — or a tombstone from before a
  # rule change — could leave a live or resumable turn; purging it would delete
  # transcript/tool-output/session-link data still coupled to an active,
  # resumable turn. Leave it for the next sweep, which retries once the run is
  # terminal. Idempotent: a still-blocked chat is simply reconsidered later.
  blocked_ids.update(
    row[0] for row in db.query(models.ChatRun.chat_id).filter(
      models.ChatRun.chat_id.in_(chat_ids),
      models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES),
    ).all()
  )
  chat_ids = [chat_id for chat_id in chat_ids if chat_id not in blocked_ids]
  if not chat_ids:
    return []

  # Delegated child chats are part of their parent's durable lifecycle.
  # Purging either side reclaims the complete parent/child graph. Legacy
  # Gauntlet rows are included only so data created by older releases can be
  # removed safely after the owning chat's recovery window expires.
  chat_id_set = set(chat_ids)
  delegation_rows = db.query(
    models.Delegation.id, models.Delegation.child_chat_id,
  ).filter(
    (models.Delegation.parent_chat_id.in_(chat_id_set))
    | (models.Delegation.child_chat_id.in_(chat_id_set))
  ).all()
  delegation_ids = {row[0] for row in delegation_rows}
  chat_id_set.update(row[1] for row in delegation_rows)
  gauntlet_ids = {row[0] for row in db.query(
    models.GauntletRun.id,
  ).filter(models.GauntletRun.parent_chat_id.in_(chat_id_set)).all()}
  if delegation_ids:
    gauntlet_ids.update(row[0] for row in db.query(
      models.GauntletTask.gauntlet_run_id,
    ).filter(
      models.GauntletTask.delegation_id.in_(delegation_ids),
    ).all())
  if gauntlet_ids:
    owned_delegations = db.query(
      models.GauntletTask.delegation_id,
    ).filter(
      models.GauntletTask.gauntlet_run_id.in_(gauntlet_ids),
      models.GauntletTask.delegation_id.isnot(None),
    ).all()
    delegation_ids.update(row[0] for row in owned_delegations)
  if delegation_ids:
    child_rows = db.query(models.Delegation.child_chat_id).filter(
      models.Delegation.id.in_(delegation_ids),
    ).all()
    chat_id_set.update(row[0] for row in child_rows)
  chat_ids = sorted(chat_id_set)

  if gauntlet_ids:
    db.query(models.GauntletTask).filter(
      models.GauntletTask.gauntlet_run_id.in_(gauntlet_ids),
    ).delete(synchronize_session=False)
    db.query(models.GauntletRun).filter(
      models.GauntletRun.id.in_(gauntlet_ids),
    ).delete(synchronize_session=False)
  if delegation_ids:
    # Defensive: a standalone delegation can be reclaimed without a Gauntlet.
    db.query(models.GauntletTask).filter(
      models.GauntletTask.delegation_id.in_(delegation_ids),
    ).delete(synchronize_session=False)
    db.query(models.Delegation).filter(
      models.Delegation.id.in_(delegation_ids),
    ).delete(synchronize_session=False)

  db.query(models.ChatActivityPosition).filter(or_(
    models.ChatActivityPosition.event_id.in_(
      [f"delegation:{value}:completed" for value in delegation_ids]
    ),
    models.ChatActivityPosition.event_id.in_(select(
      literal("peer:") + models.AgentCoordinationMessage.id,
    ).where(or_(
      models.AgentCoordinationMessage.from_chat_id.in_(chat_ids),
      models.AgentCoordinationMessage.to_chat_id.in_(chat_ids),
    ))),
  )).delete(synchronize_session=False)

  dependent_models = (
    models.ChatActivityPosition,
    models.ChatLiveAssistant,
    models.ChatEmbedGrant,
    models.AgentLifecycleEvent,
    models.AgentLifecycleRunUpdate,
    models.PlatformRestartExecution,
    models.ChatRun,
    models.ChatWait,
    models.ToolOutput,
    models.ThinkingTrace,
    models.ChatSessionLink,
  )
  for model in dependent_models:
    db.query(model).filter(
      model.chat_id.in_(chat_ids),
    ).delete(synchronize_session=False)
  # Peer notes are explicitly outside chat transcripts, so reclaim both sides
  # before the chat rows they reference. Keep the legacy recovery mailbox on
  # the same lifecycle even though new source no longer writes it.
  db.query(models.AgentCoordinationMessage).filter(
    (models.AgentCoordinationMessage.from_chat_id.in_(chat_ids))
    | (models.AgentCoordinationMessage.to_chat_id.in_(chat_ids))
  ).delete(synchronize_session=False)
  db.query(models.ProjectAgentMessage).filter(
    (models.ProjectAgentMessage.from_chat_id.in_(chat_ids))
    | (models.ProjectAgentMessage.to_chat_id.in_(chat_ids))
  ).delete(synchronize_session=False)
  # A Project may outlive one of its chats. Remove that chat's short-lived
  # collaboration claims explicitly before deleting the Chat row; SQLite
  # deployments cannot rely on foreign-key cascades being enabled here.
  db.query(models.ProjectWorkClaim).filter(
    models.ProjectWorkClaim.chat_id.in_(chat_ids),
  ).delete(synchronize_session=False)
  db.query(models.ContributionAutopilot).filter(
    models.ContributionAutopilot.followup_chat_id.in_(chat_ids),
  ).update(
    {models.ContributionAutopilot.followup_chat_id: None},
    synchronize_session=False,
  )
  # Exact-action completion is workspace idempotency history, not transcript
  # data. Sever the expiring chat provenance explicitly (SQLite deployments do
  # not rely on FK enforcement for lifecycle cleanup). Any legacy unfinished
  # claim reaching this seven-day boundary becomes reclaimable before its last
  # owner identity is removed.
  now = now_naive_utc()
  db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_chat_id.in_(chat_ids),
    models.AgentWorkClaim.completed_at.is_(None),
    models.AgentWorkClaim.released_at.is_(None),
  ).update({
    models.AgentWorkClaim.released_at: now,
    models.AgentWorkClaim.updated_at: now,
    models.AgentWorkClaim.outcome: (
      "Owning chat was deleted before this action completed."
    ),
    models.AgentWorkClaim.revision: models.AgentWorkClaim.revision + 1,
  }, synchronize_session=False)
  db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.owner_chat_id.in_(chat_ids),
  ).update({
    models.AgentWorkClaim.owner_chat_id: None,
  }, synchronize_session=False)
  # Do not rely on SQLite FK enforcement for the follower side either.
  db.query(models.AgentWorkInterest).filter(
    models.AgentWorkInterest.chat_id.in_(chat_ids),
  ).delete(synchronize_session=False)
  # Search rows are derived transcript data without a foreign key because the
  # SQLite FTS trigger owns their lifecycle. Remove them in the same durable
  # transaction as the source row rather than retaining a hard-deleted chat's
  # prose until a future search happens to reconcile the index.
  from app.chat_search import purge_chat_docs
  purge_chat_docs(db, chat_ids)
  db.query(models.Chat).filter(
    models.Chat.id.in_(chat_ids),
  ).delete(synchronize_session=False)
  db.commit()

  for chat_id in chat_ids:
    questions.cancel(chat_id)
    forget_chat(chat_id)
    _purge_chat_storage(chat_id)

  return chat_ids
