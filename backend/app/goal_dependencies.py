"""Exact-Goal dependency results, delivered by the existing chat continuation owner.

Source transitions stamp immutable outcomes in their own transaction. This
module owns selection, admission and receipt, not claim execution. Reconciliation
is level-triggered from the existing supervisor; nudges are only latency. No
result grants takeover authority, and a held Goal is never reopened by delivery.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from app import models, transcript_rows
from app.continuations import DEPENDENCY_RESULT_MESSAGE_KIND
from app.timeutil import now_naive_utc
from app.prompt_blocks import json_block_data

_LOG = logging.getLogger("moebius.goal_dependencies")


def pending_follow_predicate():
  """An unresolved edge still owned by its follower, not a terminal receipt."""
  return (models.GoalDependency.resolved_at.is_(None)
    & models.GoalDependency.withdrawn_at.is_(None)
    & models.GoalDependency.owner_suppressed_at.is_(None)
    & models.GoalDependency.result_disposition.is_(None))


def undisposed_dependencies(db: Session):
  return db.query(models.GoalDependency).filter(
    models.GoalDependency.result_disposition.is_(None),
    models.GoalDependency.withdrawn_at.is_(None),
  )


def outstanding_dependencies(db: Session):
  return undisposed_dependencies(db).filter(models.GoalDependency.owner_suppressed_at.is_(None))


def dependency_delivery_blocker(db, row):
  if row.withdrawn_at is not None or row.result_disposition is not None or row.owner_suppressed_at is not None:
    return "disposed"
  chat = db.get(models.Chat, row.waiting_chat_id)
  if chat is None or chat.deleted_at is not None:
    return "chat_deleted"
  goal = db.get(models.ChatGoal, row.waiting_goal_id)
  if goal is None or goal.chat_id != row.waiting_chat_id or goal.status not in {"open", "stopped"}:
    return "goal_closed"
  if goal.status == "stopped":
    return "goal_held"
  if row.resolved_at is None:
    return "pending"
  return None


def dependency_result_row(db, chat_id, message):
  cid = message.get("cid")
  if not isinstance(cid, str) or not cid.startswith("dependency-result-"):
    return None
  return db.query(models.GoalDependency).filter(
    models.GoalDependency.id == cid.removeprefix("dependency-result-").removesuffix(".retry"),
    models.GoalDependency.waiting_chat_id == chat_id,
    models.GoalDependency.created_by_run_id == message.get("source_work_id"),
  ).first()


def dependency_result_message_blocker(db, chat_id, message):
  if message.get("kind") != DEPENDENCY_RESULT_MESSAGE_KIND:
    return None
  row = dependency_result_row(db, chat_id, message)
  return dependency_delivery_blocker(db, row) if row is not None else "dependency_missing"


def receivable_dependency_results(db, chat_id, run_id, *, receipt=False):
  run = db.get(models.ChatRun, run_id) if run_id else None
  goal_id = run.goal_id if run is not None and run.chat_id == chat_id else None
  from app.goals import result_receiving_goals
  open_goals = result_receiving_goals(db, chat_id, run_id, receipt=receipt)
  active_chats = db.query(models.Chat.id).filter(models.Chat.deleted_at.is_(None))
  return outstanding_dependencies(db).filter(
    models.GoalDependency.waiting_chat_id == chat_id,
    models.GoalDependency.waiting_goal_id == goal_id,
    models.GoalDependency.waiting_goal_id.in_(open_goals),
    models.GoalDependency.waiting_chat_id.in_(active_chats),
    models.GoalDependency.resolved_at.is_not(None),
  )


def dependency_notice(row):
  # Startup orphan checks compare notice text; retain the existing JSON layout.
  body = json_block_data({"dependency_id": row.id, "goal_id": row.waiting_goal_id,
    "target_kind": row.target_kind, "target_key": row.target_key,
    "outcome": row.outcome_json}, separators=(", ", ": "))
  return ("A dependency of this exact Goal has resolved. This is factual evidence, "
    "not permission to perform another chat's action or take over its work. "
    "Verify the result and continue or re-plan your own Goal.\n"
    f"<goal_dependency_result>{body}</goal_dependency_result>")


def owed_dependency_results(db, chat_id, turn_message, *, run_id):
  rows = receivable_dependency_results(db, chat_id, run_id).order_by(
    models.GoalDependency.created_at, models.GoalDependency.id,
  ).all()
  return "\n\n".join(notice for row in rows
    if (notice := dependency_notice(row)) != turn_message), tuple(row.id for row in rows)


def stage_dependency_results_delivered(db, chat_id, dependency_ids, *, run_id):
  run = db.get(models.ChatRun, run_id)
  if run is None or run.chat_id != chat_id or not run.provider_execution_admitted:
    return 0
  ids = set(dependency_ids) & set(run.admitted_dependency_results_json or ())
  if not ids:
    return 0
  return receivable_dependency_results(db, chat_id, run_id, receipt=True).filter(
    models.GoalDependency.id.in_(ids),
  ).update({models.GoalDependency.result_disposition: "delivered",
    models.GoalDependency.result_disposed_at: now_naive_utc(),
    models.GoalDependency.received_run_id: run_id}, synchronize_session=False)


def stage_retire_goal_dependencies(db, chat_id, goal_id):
  query = undisposed_dependencies(db).filter(
    models.GoalDependency.waiting_chat_id == chat_id,
    models.GoalDependency.waiting_goal_id == goal_id,
  )
  _retain_source_context(db, query.all())
  return query.update({models.GoalDependency.result_disposition: "retired_by_goal_outcome",
    models.GoalDependency.result_disposed_at: now_naive_utc()}, synchronize_session=False)


def stage_retire_chat_dependencies(db, chat_id):
  """Deletion is permanent for pending/owed links, even if the chat is restored."""
  now = now_naive_utc()
  query = undisposed_dependencies(db).filter(
    models.GoalDependency.waiting_chat_id == chat_id,
  )
  _retain_source_context(db, query.all())
  return query.update({models.GoalDependency.result_disposition: "withdrawn",
    models.GoalDependency.result_disposed_at: now,
    models.GoalDependency.withdrawn_at: now}, synchronize_session=False)


def stage_withdraw_dependency(db, chat_id, dependency_id):
  """Withdraw only this edge, including an owed result, under the graph fence.

  This cannot undo evidence already admitted to a running provider. It prevents
  future admission/receipt and never interrupts work that has already entered.
  The caller owns the writer transaction and runtime invalidation.
  """
  chat = db.get(models.Chat, chat_id)
  row = db.get(models.GoalDependency, dependency_id)
  if chat is None or chat.deleted_at is not None or row is None or row.waiting_chat_id != chat_id:
    return "missing"
  if row.result_disposition == "withdrawn":
    return "withdrawn"
  goal = db.get(models.ChatGoal, row.waiting_goal_id)
  if goal is None or goal.chat_id != chat_id or goal.status not in {"open", "stopped"}:
    return "goal_closed"
  if row.result_disposition is not None or row.received_run_id is not None:
    return "already_received"
  _retain_source_context(db, [row])
  now = now_naive_utc()
  changed = undisposed_dependencies(db).filter(
    models.GoalDependency.id == row.id,
    models.GoalDependency.received_run_id.is_(None),
    models.GoalDependency.waiting_goal_id.in_(db.query(models.ChatGoal.id).filter(
      models.ChatGoal.chat_id == chat_id, models.ChatGoal.status.in_(("open", "stopped")),
    )),
  ).update({models.GoalDependency.withdrawn_at: now,
    models.GoalDependency.result_disposed_at: now,
    models.GoalDependency.result_disposition: "withdrawn"}, synchronize_session=False)
  if not changed:
    db.refresh(row)
    return "withdrawn" if row.result_disposition == "withdrawn" else "already_received"
  # The Goal remains open/held. Without another handoff its ordinary Resume
  # presentation (or an active turn's settlement) stays responsible for it.
  db.flush()
  return "withdrawn"


def _retain_source_context(db, rows):
  """Non-waking retirement retains identity, never fabricates a source result.

  Retired pending follows have no source outcome. Save only the safe identity
  before source retention or reacquisition can erase it; an already-stamped
  outcome wins. No kind/resolution/receipt is invented by this snapshot.
  """
  for row in rows:
    if row.outcome_json is not None:
      continue
    if row.target_kind == "claim_acquisition":
      claim = db.get(models.AgentWorkClaim, row.target_claim_id)
      if claim is None or claim.acquisition_id != row.target_acquisition_id:
        continue
    else:
      from app.decision_cards import saved_decision
      if saved_decision(db, row.target_chat_id, row.target_question_id) is None:
        continue
    summary = dependency_summary(db, row)
    db.query(models.GoalDependency).filter(
      models.GoalDependency.id == row.id, models.GoalDependency.resolved_at.is_(None),
      models.GoalDependency.result_disposition.is_(None),
    ).update({models.GoalDependency.outcome_json: {
      "summary": summary["condition"], "source_chat_id": summary["source_chat_id"],
      "source_chat_title": summary["source_chat_title"],
    }}, synchronize_session=False)


def selected_dependency_carrier(db, row):
  identity = "dependency-resume-" + row.id
  carrier = db.get(models.ChatRun, identity)
  if carrier is not None and (carrier.status == "failed" or (
    carrier.status == "interrupted" and carrier.provider_execution_admitted is False
    and carrier.owner_superseded_by_run_id is not None
  )):
    identity += ".retry"
    carrier = db.get(models.ChatRun, identity)
  return identity, carrier


def dependency_resume_blocker(db, row):
  from app.chat import programmatic_start_blocker
  from app.platform_update import late_edits_pending
  blocker = dependency_delivery_blocker(db, row)
  if blocker:
    return blocker
  if late_edits_pending():
    return "restoring_edits"
  blocker = programmatic_start_blocker(db, row.waiting_chat_id)
  if blocker:
    return blocker
  _, carrier = selected_dependency_carrier(db, row)
  if carrier is not None and not (
    carrier.status == "running" and carrier.provider_execution_admitted is False
  ):
    return "manual_resume"
  return None


def goal_dependency_handoffs(db, goal, *, include_foreign_context=True):
  from app.goal_dependency_sources import dependency_source_blocker
  rows = outstanding_dependencies(db).filter(
    models.GoalDependency.waiting_chat_id == goal.chat_id,
    models.GoalDependency.waiting_goal_id == goal.id,
  ).order_by(models.GoalDependency.created_at).all()
  result = []
  for row in rows:
    context = dependency_summary(db, row, include_foreign_context=include_foreign_context)
    blocker = (dependency_resume_blocker(db, row) if row.resolved_at is not None
      else dependency_source_blocker(db, row))
    if goal.status != "open":
      blocker = "goal_held" if goal.status == "stopped" else "goal_closed"
    # Temporary owner-input/provider/restart barriers retain a real result;
    # exhausted carriers and unavailable sources do not promise a future wake.
    automatic = blocker not in {"manual_resume", "source_missing", "owner_not_progressing",
      "goal_held", "goal_closed", "chat_deleted", "disposed",
      "source_held", "source_failed", "owner_deleted"}
    result.append({key: context[key] for key in (
      "id", "target_kind", "condition", "source_chat_id", "source_chat_title",
      "source_question_id", "source_deleted", "outcome")})
    result[-1].update(automatic=automatic, delivery_pending=row.resolved_at is not None,
      resume_blocker=blocker)
  return result


def dependency_summary(db, row, *, include_foreign_context=True):
  """Safe source identity shared by settled history and transcript receipts."""
  outcome = row.outcome_json or {}
  # Exact-chat embeds may display facts already saved on their subscription,
  # never enrich them from another chat or export its navigation identifiers.
  source_id = None
  title = outcome.get("source_chat_title") or "Another chat"
  condition = outcome.get("summary") or "Shared work"
  deleted = False
  if include_foreign_context:
    claim = db.get(models.AgentWorkClaim, row.target_claim_id) if row.target_claim_id else None
    if claim is not None and claim.acquisition_id != row.target_acquisition_id:
      claim = None
    source_id = outcome.get("source_chat_id") or (claim.owner_chat_id if claim else row.target_chat_id)
    source = db.get(models.Chat, source_id) if source_id else None
    from app.decision_cards import saved_decision
    card = saved_decision(db, source_id, row.target_question_id) if row.target_kind == "owner_card" else None
    condition = outcome.get("summary") or (card.summary if card else None) or (claim.summary if claim else "Shared work")
    title = (source.title if source else None) or outcome.get("source_chat_title") or "Deleted chat"
    deleted = source is None or source.deleted_at is not None
  safe_outcome = row.outcome_json
  if not include_foreign_context and safe_outcome is not None:
    safe_outcome = {key: value for key, value in outcome.items()
      if key in {"kind", "summary", "answers", "outcome"}}
  return {"id": row.id, "target_kind": row.target_kind,
    "condition": condition, "source_chat_id": source_id,
    "source_question_id": row.target_question_id if include_foreign_context else None,
    "source_chat_title": title, "source_deleted": deleted,
    "automatic": False, "delivery_pending": row.result_disposition is None,
    "outcome": safe_outcome, "disposition": row.result_disposition,
    "result_disposition": row.result_disposition, "received_run_id": row.received_run_id}


def goal_dependency_history(db, goal, *, include_foreign_context=True):
  rows = db.query(models.GoalDependency).filter(
    models.GoalDependency.waiting_goal_id == goal.id,
    models.GoalDependency.waiting_chat_id == goal.chat_id,
    models.GoalDependency.result_disposition.is_not(None),
  ).order_by(models.GoalDependency.created_at, models.GoalDependency.id).all()
  return [dependency_summary(db, row, include_foreign_context=include_foreign_context) for row in rows]


def terminal_dependency_summaries_by_message_index(db, chat_id, messages, *,
    message_start=0, message_end=None, include_foreign_context=True):
  """A settled subscription stays beside its exact receiving or declaring turn.

  Never guess a recipient from a timestamp or attach another Goal's result to
  the newest answer. Find anchors in the full coordinate index before applying
  the page window; only on-page dependencies hydrate outcomes and source context.
  """
  from app.goal_plans import assistant_message_run_id
  query = db.query(models.GoalDependency).filter(
    models.GoalDependency.waiting_chat_id == chat_id,
    (models.GoalDependency.resolved_at.is_not(None)
     | models.GoalDependency.result_disposition.is_not(None)),
  ).order_by(models.GoalDependency.created_at, models.GoalDependency.id)
  rows = query.with_entities(models.GoalDependency.id,
    models.GoalDependency.received_run_id, models.GoalDependency.created_by_run_id).all()
  if not rows:
    return {}
  by_run = {}
  for index, message in enumerate(messages):
    if isinstance(message, dict) and message.get("role") == "assistant" and not message.get("hidden"):
      run_id = assistant_message_run_id(message.get("id"))
      if run_id:
        by_run.setdefault(run_id, index)
  anchors = {}
  for row in rows:
    index = by_run.get(row.received_run_id) if row.received_run_id else None
    if index is None:
      index = by_run.get(row.created_by_run_id)
    if index is not None and index >= message_start and (message_end is None or index < message_end):
      anchors[row.id] = index
  if not anchors:
    return {}
  result = {}
  for row in query.filter(models.GoalDependency.id.in_(anchors)).all():
    result.setdefault(anchors[row.id], []).append(
      dependency_summary(db, row, include_foreign_context=include_foreign_context))
  return result


def source_dependency_backlinks(db, source_chat_id, *, question_ids=(), goal_id=None):
  """Quiet context for exact source cards or the Goal owning live acquisitions.

  Live subscriptions only; closed/deleted dependents and historical acquisitions
  must not imply a new request. Multiple Goals in one chat share one backlink.
  """
  from sqlalchemy import and_, or_
  targets = []
  if question_ids:
    targets.append(and_(models.GoalDependency.target_kind == "owner_card",
      models.GoalDependency.target_chat_id == source_chat_id,
      models.GoalDependency.target_question_id.in_(question_ids)))
  if goal_id:
    claims = db.query(models.AgentWorkClaim.id, models.AgentWorkClaim.acquisition_id).filter(
      models.AgentWorkClaim.owner_chat_id == source_chat_id,
      models.AgentWorkClaim.owner_goal_id == goal_id,
      models.AgentWorkClaim.completed_at.is_(None), models.AgentWorkClaim.released_at.is_(None),
    ).all()
    targets.extend(and_(models.GoalDependency.target_kind == "claim_acquisition",
      models.GoalDependency.target_claim_id == claim.id,
      models.GoalDependency.target_acquisition_id == claim.acquisition_id) for claim in claims)
  if not targets:
    return {}
  rows = outstanding_dependencies(db).join(models.ChatGoal,
    models.ChatGoal.id == models.GoalDependency.waiting_goal_id).join(models.Chat,
    models.Chat.id == models.GoalDependency.waiting_chat_id).filter(
      or_(*targets), models.ChatGoal.status.in_(("open", "stopped")),
      models.ChatGoal.chat_id == models.Chat.id, models.Chat.deleted_at.is_(None),
  ).with_entities(models.GoalDependency.target_kind, models.GoalDependency.target_question_id,
    models.Chat.id, models.Chat.title).order_by(models.Chat.title, models.Chat.id).all()
  result = {}
  for row in rows:
    key = row.target_question_id if row.target_kind == "owner_card" else goal_id
    result.setdefault(key, {})[row.id] = {"chat_id": row.id, "title": row.title or "Untitled chat"}
  return {key: list(chats.values()) for key, chats in result.items()}


def safe_dependency_startup_orphan(db, chat, physical):
  from app.delegations import retired_delegation_for_chat
  if (retired_delegation_for_chat(db, chat.id) or physical.status != "running"
      or physical.provider_execution_admitted is not False or physical.chat_id != chat.id
      or physical.initiated_by_app_id is not None or not physical.id.startswith("dependency-resume-")):
    return False
  row = db.get(models.GoalDependency,
    physical.id.removeprefix("dependency-resume-").removesuffix(".retry"))
  if (row is None or row.waiting_chat_id != chat.id or row.waiting_goal_id != physical.goal_id
      or dependency_delivery_blocker(db, row)):
    return False
  last = transcript_rows.at(db, chat, -1)
  live = chat.live_assistant or {}
  return bool(isinstance(last, dict) and last.get("role") == "user"
    and last.get("kind") == DEPENDENCY_RESULT_MESSAGE_KIND
    and last.get("cid") == "dependency-result-" + physical.id.removeprefix("dependency-resume-")
    and last.get("content") == dependency_notice(row)
    and last.get("source_work_id") == row.created_by_run_id and last.get("hidden")
    and live.get("id") == physical.id and not live.get("blocks"))


async def deliver_dependency(row_id):
  """Idempotent scheduling is not a receipt; exact provider success owns that."""
  from app import chat_queue
  from app.chat import is_chat_running
  from app.chat_start import start_programmatic_chat_continuation
  from app.chat_writer import AppendPending, await_ack, get_writer
  from app.database import SessionLocal
  from app.platform_update import late_edits_pending
  with SessionLocal() as db:
    row = db.get(models.GoalDependency, row_id)
    if row is None:
      return False
    blocker = dependency_delivery_blocker(db, row)
    if blocker == "chat_deleted":
      stage_retire_chat_dependencies(db, row.waiting_chat_id)
      db.commit()
    elif blocker == "goal_closed":
      stage_retire_goal_dependencies(db, row.waiting_chat_id, row.waiting_goal_id)
      db.commit()
    if blocker or late_edits_pending():
      return False
    run_id, carrier = selected_dependency_carrier(db, row)
    if carrier is not None and not (carrier.status == "running"
        and carrier.provider_execution_admitted is False and not is_chat_running(row.waiting_chat_id)):
      return False
    chat_id, content, source_id = row.waiting_chat_id, dependency_notice(row), row.created_by_run_id
    source = db.get(models.ChatRun, source_id) if source_id else None
    # A preserved former Goal may have no provable subscribing physical run.
    # Queue admission then uses the result run as its root, never invented lineage.
    root_id = ((source.root_run_id or source.id) if source is not None and source.chat_id == chat_id
      else (carrier.root_run_id or carrier.id) if carrier is not None else None)
  cid = "dependency-result-" + run_id.removeprefix("dependency-resume-")
  started = False
  if root_id is not None:
    started = await start_programmatic_chat_continuation(chat_id=chat_id,
      root_run_id=root_id, run_token=run_id, content=content, continuation_id=cid,
      reason="dependency_result", message_kind=DEPENDENCY_RESULT_MESSAGE_KIND,
      source_work_id=source_id, hidden=True)
  if started:
    return True
  with SessionLocal() as db:
    if db.get(models.ChatRun, run_id) is not None:
      return False
  async with chat_queue.get_lock(chat_id):
    await await_ack(get_writer().submit(AppendPending(chat_id=chat_id, run_token="",
      user_msg={"role": "user", "content": content, "ts": int(time.time() * 1000),
        "cid": cid, "hidden": True, "kind": DEPENDENCY_RESULT_MESSAGE_KIND,
        "source_work_id": source_id}, initiated_by_app_id=None)))
  return False


async def withdraw_disposed_dependency_notices(chat_id):
  from app.chat_writer import CancelPending, await_ack, get_writer
  from app.database import SessionLocal
  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    stale = []
    for message in (chat.pending_messages or []) if chat is not None else []:
      if message.get("kind") != DEPENDENCY_RESULT_MESSAGE_KIND:
        continue
      row = dependency_result_row(db, chat_id, message)
      if row is None or row.result_disposition is not None or row.withdrawn_at is not None or row.owner_suppressed_at is not None:
        stale.append(message["cid"])
  for cid in stale:
    await await_ack(get_writer().submit(CancelPending(chat_id=chat_id, run_token="", cid=cid)))
  return len(stale)


def _has_pending_claim_dependencies():
  """Read the reconciliation trigger in a worker-owned session."""
  from app.database import SessionLocal
  with SessionLocal() as db:
    return db.query(models.GoalDependency.id).filter(
      models.GoalDependency.target_kind == "claim_acquisition",
      pending_follow_predicate(),
    ).first() is not None


def _select_dependency_deliveries():
  """Select owed results after source reconciliation, off the event loop."""
  from app.database import SessionLocal
  with SessionLocal() as db:
    rows = outstanding_dependencies(db).filter(
      models.GoalDependency.resolved_at.is_not(None),
    ).with_entities(models.GoalDependency.id, models.GoalDependency.waiting_chat_id).all()
    return [row.id for row in rows], {row.waiting_chat_id for row in rows}


async def deliver_pending_dependencies():
  """Existing supervisor reconciliation, never a polling agent or new runner."""
  from app.chat_writer import ReconcileClaimSources, await_ack, get_writer
  # Only genuinely unresolved claim follows warrant the serialized source
  # reconciliation. The partial index added at cutover makes this a seek.
  try:
    if await asyncio.to_thread(_has_pending_claim_dependencies):
      await await_ack(get_writer().submit(ReconcileClaimSources()))
  except Exception:
    _LOG.warning("claim source reconciliation failed", exc_info=True)
  ids, chats = await asyncio.to_thread(_select_dependency_deliveries)
  started = 0
  for row_id in ids:
    try:
      started += int(await deliver_dependency(row_id))
    except Exception:
      _LOG.warning("dependency continuation failed dependency=%s", row_id, exc_info=True)
  for chat_id in chats:
    try:
      await withdraw_disposed_dependency_notices(chat_id)
    except Exception:
      _LOG.warning("dependency notice cleanup failed chat=%s", chat_id, exc_info=True)
  return started
