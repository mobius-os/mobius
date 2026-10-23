"""Repair publication and observation while the route owns admission locks.

The route supplies one live-authority callback. This executor owns the persisted
attempt and exact non-dispatch release; confirmed facts survive revoked authority.
"""
import asyncio
import logging
import subprocess

from fastapi import HTTPException
from sqlalchemy import text

from app.database import SessionLocal
from app import agent_work_claims, chat_queue, contribution_review_runs as reviews, models, settled
from app import contribution_review_repairs as repairs
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_effects import DispatchResult, NotDispatched


_log = logging.getLogger(__name__)
_unsupported_gc_warned = False


def checkout_gc_scope(db) -> dict[str, set[str]]:
  """Classify durable checkout receipts before a filesystem-only sweep.

  An active physical parent/child turn or uncertain public effect always wins
  over age. Dormant edits get a longer grace than unreceipted crash orphans.
  """
  active_chats = {chat_id for (chat_id,) in db.query(models.ChatRun.chat_id).filter(
    models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES)).all()}
  active_parents = {parent for (parent,) in db.query(models.Delegation.parent_chat_id).filter(
    models.Delegation.child_chat_id.in_(active_chats)).all()} if active_chats else set()
  protected: set[str] = set()
  terminal: set[str] = set()
  dormant: set[str] = set()
  for row in db.query(models.ContributionReviewRun).filter_by(
      mode="review_fix_merge").populate_existing().all():
    active_editor = row.chat_id in active_chats or row.chat_id in active_parents
    stopped = (row.outcomes_json or {}).get("_review_stop", {}).get("state") == "requested"
    for original in row.targets_json or []:
      item_key = reviews.key(original)
      outcome = (row.outcomes_json or {}).get(item_key, {})
      if not isinstance(outcome, dict):
        continue
      attempts = outcome.get("repair_attempts") or []
      for attempt in attempts:
        path = attempt.get("checkout") if isinstance(attempt, dict) else None
        if not isinstance(path, str):
          continue
        state = attempt.get("state")
        if state == "pushed":
          terminal.add(path)
        elif state != "rejected":
          protected.add(path)
      checkout = outcome.get("checkout") or {}
      path = checkout.get("checkout") if isinstance(checkout, dict) else None
      if not isinstance(path, str):
        continue
      try:
        effective_head = reviews.effective_target(row, original)["head_sha"]
      except (HTTPException, KeyError, ValueError):
        effective_head = None  # Ambiguous history cannot authorize deletion.
      initial_head = checkout.get("initial_head_sha")
      superseded = (isinstance(initial_head, str) and isinstance(effective_head, str)
                    and initial_head != effective_head)
      if active_editor:
        protected.add(path)
      elif stopped or outcome.get("state") == "merged" or superseded:
        terminal.add(path)
      else:
        dormant.add(path)
  return {"protected_paths": protected, "terminal_paths": terminal,
          "dormant_paths": dormant}


def sweep_checkout_receipts_once() -> None:
  """Fence writers through classification and rename, not slow deletion.

  Admission/ChatRun/Delegation writes cannot change checkout ownership while
  a candidate is selected and atomically quarantined. After commit, any new
  admission sees a missing old path and must reprepare it.
  """
  global _unsupported_gc_warned
  with SessionLocal() as db:
    dialect = db.get_bind().dialect.name
    if dialect != "sqlite":
      if not _unsupported_gc_warned:
        _log.warning("repair checkout GC skipped on %s: no cross-writer fence", dialect)
        _unsupported_gc_warned = True
      return
    # The first statement is a harmless owner-row write. SQLite holds its
    # single-writer reservation until commit, including classification and
    # same-filesystem rename. No recursive delete runs in that transaction.
    db.execute(text("UPDATE contribution_review_runs SET revision=revision "
                    "WHERE id=(SELECT id FROM contribution_review_runs LIMIT 1)"))
    repairs.quarantine_stale_checkouts(**checkout_gc_scope(db))
    db.commit()
  repairs.delete_quarantined_checkouts()


async def dispatch_repair(row, target, validation, *, gh, cwd, authorize):
  try:
    authorize()
  except HTTPException as exc:
    return DispatchResult.not_dispatched(str(exc.detail))
  loop = asyncio.get_running_loop()
  async def guard():
    try:
      authorize()
      previous = dict((row.outcomes_json or {}).get(reviews.key(target), {}))
      await asyncio.to_thread(reviews.require_repairable_merge, gh, cwd, target, previous)  # unsettled: read-only (saved queue removal proof)
      authorize()
    except HTTPException as exc:
      raise NotDispatched(str(exc.detail)) from exc
  def before_push():
    asyncio.run_coroutine_threadsafe(guard(), loop).result()
  return await settled.run_settled(repairs.push_repair, gh, cwd, row, target, validation,
                                  before_push=before_push)


async def publish_attempt(db, row, target, validation, receipt, principal, *, gh, cwd, authorize):
  """Reserve, dispatch once, then preserve the exact public fact or uncertainty."""
  async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
    authorize()
    previous = row.outcomes_json.get(reviews.key(target), {})
    owned = reviews.arm_repair(db, row, target, previous, receipt, principal)
    if owned:
      return owned
  db.refresh(row)
  previous = row.outcomes_json[reviews.key(target)]
  attempts = list(previous["repair_attempts"])
  pushed = [*attempts[:-1], {**attempts[-1], "state": "pushed"}]
  try:
    # Successor invariants belong before the network effect. In particular,
    # queue-removal history in a later layer must not fail after a real push.
    successor = reviews.confirmed_successor(previous, pushed, validation)
  except (HTTPException, KeyError, ValueError):
    result = DispatchResult.not_dispatched("The successor receipt cannot be recorded safely.")
  else:
    result = await dispatch_repair(row, target, validation, gh=gh, cwd=cwd, authorize=authorize)
  if result.state == "not_dispatched":
    remote_rejection = (isinstance(result.receipt, dict)
      and result.receipt.get("kind") == "remote_ref_rejection"
      and result.receipt.get("head_sha") == attempts[-1]["head_sha"])
    saved_attempts = ([*attempts[:-1], {**attempts[-1], "state": "rejected"}]
      if remote_rejection else attempts[:-1])
    reviews.save_effect_outcome(db, row, reviews.key(target), {**previous,
      "repair_attempts": saved_attempts, "state": "needs_you",
      "summary": f"No branch update was applied: {result.reason}"})
    agent_work_claims.finish_work(db, owner_id=row.owner_id, chat_id=row.chat_id,
      work_key=attempts[-1]["work_key"], outcome="Repair branch update was not applied.", release=True)
  elif result.state == "unknown":
    attempts[-1] = {**attempts[-1], "state": "push_unknown"}
    reviews.save_effect_outcome(db, row, reviews.key(target), {**previous,
      "repair_attempts": attempts, "state": "needs_you", "summary": result.reason})
  else:
    attempts[-1] = {**attempts[-1], "state": "pushed"}
    reviews.save_effect_outcome(db, row, reviews.key(target), successor)
    await settled.run_settled(repairs.discard_checkout, validation["checkout"])
  return None


async def reconcile_push(db, row, original, previous, pending, *, gh, cwd):
  """Confirm the existing push from remote identity; never send another push."""
  if pending["state"] == "pushed":
    return True
  candidate = {**original, "head_sha": pending["head_sha"], "base_sha": pending["base_sha"]}
  try:
    await asyncio.to_thread(reviews.current_pull, gh, cwd, candidate)
  except (HTTPException, ContributionSubmitError, OSError, ValueError, subprocess.SubprocessError):
    return False
  attempts = [{**r, "state": "pushed"} if r is pending else r
              for r in previous.get("repair_attempts", [])]
  try:
    outcome = reviews.confirmed_successor(previous, attempts, pending)
  except (HTTPException, KeyError, ValueError):
    # GitHub already proves the push. Do not strand it as 'pushing' merely
    # because a later local projection invariant needs manual repair.
    outcome = {**previous, "repair_attempts": attempts, "state": "needs_you",
      "summary": "The repair push is confirmed, but successor history needs manual reconciliation."}
  reviews.save_effect_outcome(db, row, reviews.key(original), outcome)
  await settled.run_settled(repairs.discard_checkout, pending.get("checkout"))
  return True


def escalate_unconfirmed_push(db, row, original, previous, pending):
  """An old armed receipt is uncertain, never permission for another push."""
  if pending.get("state") != "pushing":
    return
  attempts = [{**attempt, "state": "push_unknown"} if attempt.get("id") == pending.get("id") else attempt
              for attempt in previous.get("repair_attempts", [])]
  reviews.save_effect_outcome(db, row, reviews.key(original), {**previous,
    "repair_attempts": attempts, "state": "needs_you",
    "summary": "The earlier repair push is not confirmed; observe it without repeating the write."})
