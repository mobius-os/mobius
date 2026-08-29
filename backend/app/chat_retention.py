"""Permanent cleanup for chats whose owner-deletion window has expired.

Only chats explicitly tombstoned through the delete lifecycle belong here.
Ordinary reads must not infer abandonment from age or content, and notification
history follows its own product lifecycle rather than piggybacking on chat
listing.
"""

import shutil
from pathlib import Path

from sqlalchemy import select
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


def stage_chat_graph_purge(db: Session, chat_ids: list[str]) -> list[str]:
  """Stage relational deletion for quiescent tombstoned chat graphs.

  Callers own the surrounding transaction. The returned IDs include hidden
  Delegation children discovered from the supplied controllers. Filesystem and
  process cleanup must run only after that transaction commits successfully.
  """
  if not chat_ids:
    return []

  # A timed-out SDK stop deliberately keeps the Gauntlet and target lease in
  # ``stopping``. Never hard-purge its controller/critic rows out from under a
  # still-live execution; the next retention sweep retries after supervision
  # proves quiescence.
  blocked_ids = {row[0] for row in db.query(
    models.GauntletRun.parent_chat_id,
  ).filter(
    models.GauntletRun.parent_chat_id.in_(chat_ids),
    models.GauntletRun.status.in_(("running", "stopping")),
  ).all()}
  blocked_ids.update(row[0] for row in db.query(
    models.Delegation.child_chat_id,
  ).join(
    models.GauntletTask,
    models.GauntletTask.delegation_id == models.Delegation.id,
  ).join(
    models.GauntletRun,
    models.GauntletRun.id == models.GauntletTask.gauntlet_run_id,
  ).filter(
    models.Delegation.child_chat_id.in_(chat_ids),
    models.GauntletRun.status.in_(("running", "stopping")),
  ).all())
  # Standalone Delegations share the same physical ChatRun supervision as
  # Gauntlet critics. The soft-delete boundary normally cancels them, but a
  # timed-out provider (or an older tombstone from before that rule) must keep
  # the entire parent/child graph recoverable until it is truly quiescent.
  from app.delegations import active_delegation_ids_for_chat
  blocked_ids.update(
    chat_id
    for chat_id in chat_ids
    if active_delegation_ids_for_chat(db, chat_id)
  )
  chat_ids = [chat_id for chat_id in chat_ids if chat_id not in blocked_ids]
  if not chat_ids:
    return []

  # Workflow-owned critic chats are part of their controller's durable
  # lifecycle. Purging either side must first remove the coordinator/task/
  # delegation control rows, and purging a controller also reclaims its hidden
  # children rather than leaving inaccessible transcripts behind.
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

  dependent_models = (
    models.ChatEmbedGrant,
    models.AgentLifecycleEvent,
    models.AgentLifecycleRunUpdate,
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
  db.query(models.ContributionAutopilot).filter(
    models.ContributionAutopilot.followup_chat_id.in_(chat_ids),
  ).update(
    {models.ContributionAutopilot.followup_chat_id: None},
    synchronize_session=False,
  )
  # Search rows are derived transcript data without a foreign key because the
  # SQLite FTS trigger owns their lifecycle. Remove them in the same durable
  # transaction as the source row rather than retaining a hard-deleted chat's
  # prose until a future search happens to reconcile the index.
  from app.chat_search import purge_chat_docs
  purge_chat_docs(db, chat_ids)
  db.query(models.Chat).filter(
    models.Chat.id.in_(chat_ids),
  ).delete(synchronize_session=False)
  return chat_ids


def finalize_purged_chats(chat_ids: list[str]) -> None:
  """Release process state and derived files after relational commit."""

  for chat_id in chat_ids:
    questions.cancel(chat_id)
    forget_chat(chat_id)
    _purge_chat_storage(chat_id)


def purge_expired_chat_tombstones(db: Session) -> list[str]:
  """Permanently remove chats explicitly deleted more than seven days ago.

  The candidate query selects IDs only, so this lifecycle sweep never decodes
  transcript or pending-message JSON. Database deletion commits before
  best-effort process/filesystem cleanup; a failed transaction therefore
  cannot erase recoverable data outside the database.
  """
  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  expired_chat_ids = select(models.Chat.id).where(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
  )
  chat_ids = stage_chat_graph_purge(
    db, list(db.scalars(expired_chat_ids).all()),
  )
  if not chat_ids:
    return []
  db.commit()
  finalize_purged_chats(chat_ids)
  return chat_ids
