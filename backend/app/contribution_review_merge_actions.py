"""Exact merge reservation and dispatch, separate from review judgment."""
import asyncio

from fastapi import HTTPException

from app import chat_queue, contribution_review_runs as reviews, settled
from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine
from app.contribution_review_effects import DispatchResult, NotDispatched, classify_dispatch_error
from app.github_connection import _github_connection_transaction


async def dispatch_merge(gh, cwd, target, repo, checks, authorize, method=None, still_clear=None):
  """Authority refusal precedes dispatch; a received remote fact survives Stop.

  ``still_clear`` is for a caller that decided before waiting for the connection and the run's lock:
  it returns why the merge is refused on what GitHub says now, or None.
  """
  try:
    authorize()
    # The earlier gate ran before reservation and claim persistence. A moved
    # target tip is a known refusal, not an uncertain merge/queue attempt.
    await settled.run_settled(reviews.assert_current_base, gh, cwd, target)
    refusal = await still_clear() if still_clear else None
    if refusal:
      return DispatchResult.not_dispatched(refusal)
    authorize()
  except HTTPException as exc:
    return DispatchResult.not_dispatched(str(exc.detail))
  except asyncio.CancelledError:
    return DispatchResult.not_dispatched("The review was cancelled before any merge request began.")
  except Exception:
    return DispatchResult.not_dispatched("The final check failed before merge dispatch.")
  try:
    if checks.get("isMergeQueueEnabled") is True:
      receipt = await settled.run_settled(reviews.enqueue, gh, cwd, target)
      return DispatchResult.confirmed({"state": "queued", **reviews.queue_receipt(receipt)})
    receipt = await settled.run_settled(reviews.perform_merge, gh, cwd, target, repo, method)
    if receipt.get("merged") is False:
      raise NotDispatched("GitHub refused the merge request without applying it.")
    if receipt.get("merged") is not True:
      return DispatchResult.unknown("GitHub returned no usable merge confirmation. Reconcile without retry.")
    return DispatchResult.confirmed({"state": "merged", "merge_sha": receipt.get("sha")})
  except Exception as exc:
    return classify_dispatch_error(exc)


async def _reserve_and_dispatch(db, row, target, outcome, repo, checks, principal, *, gh, cwd, authorize,
                                method=None, still_clear=None):
  async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
    authorize()
    try:
      owned = reviews.arm_merge(db, row, target, outcome, principal)
    except ValueError as exc:
      db.rollback()
      raise HTTPException(409, f"Merge claim conflict: {exc} No merge attempt was started; no new Goal follow was created.") from exc
    if owned:
      if owned.get("review_selection_id") != row.id:
        reviews.save_outcome(db, row, reviews.key(target), owned)
      else:
        db.refresh(row)
      return None
  db.refresh(row)
  armed = dict(row.outcomes_json[reviews.key(target)])
  result = await dispatch_merge(gh, cwd, target, repo, checks, authorize, method, still_clear)
  if result.state == "not_dispatched":
    reviews.disarm_merge(db, row, target, result.reason)
    if asyncio.current_task().cancelling():
      raise asyncio.CancelledError()
    return None
  if result.state == "confirmed":
    return {**armed, **result.receipt}
  return {**armed, "state": "merge_unknown", "summary": result.reason}


async def _merge_refusal(gh, cwd, row, target, repo, pull, checks, job) -> str | None:
  """GitHub's own merge rules, then the PR machine's merge guard on facts read now.

  ``job`` excludes this run itself: its own merge is not a second agent on the PR.
  """
  blocker = reviews.merge_blocker(target, repo, pull, checks)
  if blocker:
    return blocker
  facts = await asyncio.to_thread(pr_facts.read_facts, gh, cwd, target, repo, pull,  # unsettled: read-only (GitHub reviews)
                                  actor_id=row.github_actor_id, job=job, checks=checks)
  return machine.action_blocker(facts, "merge") or None


async def merge_reviewed_target(db, row, target, outcome, repo, pull, principal, *, gh, cwd, authorize,
                                job: dict | None):
  """Persist the current gate result or one typed public effect, never replay."""
  if pull.get("merged"):
    reviews.save_effect_outcome(db, row, reviews.key(target), {
      **outcome, "state": "merged", "merge_sha": pull.get("merge_commit_sha")})
    return
  checks = await asyncio.to_thread(reviews.pull_checks, gh, cwd, target)  # unsettled: read-only (GitHub PR checks)
  entry = reviews.queue_entry(checks, target)
  if entry:
    await asyncio.to_thread(reviews.assert_current_base, gh, cwd, target)  # unsettled: read-only (GitHub target base)
    authorize()
    reviews.save_outcome(db, row, reviews.key(target), {**outcome, "state": "queued", **reviews.queue_receipt(entry)})
    return
  blocker = await _merge_refusal(gh, cwd, row, target, repo, pull, checks, job)
  if blocker:
    authorize()
    reviews.save_outcome(db, row, reviews.key(target), {**outcome, "state": "needs_you", "summary": blocker})
    return
  method = reviews.merge_method(repo) if not checks.get("isMergeQueueEnabled") else None

  async def still_clear() -> str | None:
    """The gates above on what GitHub says now: they were answered before the waits for the connection
    and this run's lock, and an approval can be dismissed meanwhile."""
    repo_now, pull_now = await asyncio.to_thread(reviews.current_pull, gh, cwd, target)  # unsettled: read-only (GitHub pull request)
    checks_now = await asyncio.to_thread(reviews.pull_checks, gh, cwd, target)  # unsettled: read-only (GitHub PR checks)
    return await _merge_refusal(gh, cwd, row, target, repo_now, pull_now, checks_now, job)

  async with _github_connection_transaction():
    actor = await asyncio.to_thread(reviews.read, gh, cwd, "user")  # unsettled: read-only (GitHub actor identity)
    if str(actor.get("id") or "") != row.github_actor_id:
      authorize()
      reviews.save_outcome(db, row, reviews.key(target), {**outcome, "state": "needs_you",
        "summary": "The connected GitHub account changed. Approve a new review selection."})
      return
    await asyncio.to_thread(reviews.assert_current_base, gh, cwd, target)  # unsettled: read-only (GitHub target base)
    result = await _reserve_and_dispatch(db, row, target, outcome, repo, checks, principal,
      gh=gh, cwd=cwd, authorize=authorize, method=method, still_clear=still_clear)
    if result is not None:
      reviews.save_effect_outcome(db, row, reviews.key(target), result)
