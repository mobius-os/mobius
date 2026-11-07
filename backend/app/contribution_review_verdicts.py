"""Formal review verdicts: the agent proposes, only the owner submits.

A run with the frozen ``propose_verdict`` opt-in never posts APPROVE or
REQUEST_CHANGES by itself. Its ``publish:true`` outcome saves one immutable
proposal (event, exact wording, pinned head and base). The owner's own submit
of that exact proposal is the only authority to post it, once, at the pinned
head; an uncertain dispatch is never retried. A proposal is not consent to
merge: a merge grant still needs the posted APPROVE and its usual gates.
"""
import asyncio
import hashlib
import subprocess
import uuid

from fastapi import HTTPException

from app import chat_queue, models, settled, contribution_review_runs as reviews
from app import contribution_review_public as public
from app.contribution_errors import ContributionSubmitError
from app.decision_cards import fence_owner
from app.github_connection import _github_connection_transaction

EVENTS = {"all_clear": "APPROVE", "needs_you": "REQUEST_CHANGES"}


def own_pull(row, pull) -> bool:
  """GitHub refuses approve and request-changes from the PR's own author."""
  return str((pull.get("user") or {}).get("id") or "") == str(row.github_actor_id)


def propose(outcome, target) -> dict:
  event = EVENTS[outcome["state"]]
  body = public.review_comment_body(outcome, event=event)
  return {"id": uuid.uuid4().hex, "state": "proposed", "event": event, "body": body,
          "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
          "head_sha": target["head_sha"], "reviewed_base_sha": outcome.get("reviewed_base_sha"),
          "proposed_at": reviews.utc_now_iso()}


def _settle(db, row, item_key, proposal_id, fields):
  """Move only this exact proposal forward, on the refreshed current item."""
  def apply(current):
    saved = current.get("verdict_proposal") or {}
    if saved.get("id") != proposal_id or saved.get("state") in reviews.VERDICT_SETTLED:
      return None
    return {**current, "verdict_proposal": {**saved, **fields}}
  reviews.save_effect_outcome(db, row, item_key, apply)


def _arm(db, row, item_key, proposal_id, marker, authorize):
  """Reserve the one dispatch for this proposal, fenced across grants."""
  fence_owner(db, row.owner_id)
  try:
    authorize()
  except Exception:
    db.rollback()
    raise
  saved = ((row.outcomes_json or {}).get(item_key) or {}).get("verdict_proposal") or {}
  if saved.get("id") != proposal_id or saved.get("state") != "proposed":
    db.rollback()
    raise HTTPException(409, "This verdict was already submitted or discarded. Refresh to see its result.")
  holder = db.query(models.ContributionReviewRun.id).filter(
    models.ContributionReviewRun.owner_id == row.owner_id,
    models.ContributionReviewRun.id != row.id,
    models.ContributionReviewRun.outcomes_json[item_key]["verdict_proposal"]["head_sha"].as_string() == saved["head_sha"],
    models.ContributionReviewRun.outcomes_json[item_key]["verdict_proposal"]["state"].as_string().in_(
      ("posting", "posted", "unknown")),
  ).first()
  if holder:
    db.rollback()
    raise HTTPException(409, f"Another review run ({holder.id}) already submitted a verdict for this exact version.")
  current = row.outcomes_json[item_key]
  armed = {**current, "verdict_proposal": {**saved, "state": "posting", "marker": marker,
                                           "submitted_at": reviews.utc_now_iso()}}
  if not reviews._cas_outcome(db, row, item_key, armed):
    raise HTTPException(409, "This review changed. Refresh before submitting.")
  return armed["verdict_proposal"]


def _preflight(gh, cwd, row, target):
  actor = reviews.read(gh, cwd, "user")
  if str(actor.get("id") or "") != row.github_actor_id:
    raise HTTPException(409, "The connected GitHub account changed. Nothing was posted.")
  _, pull = reviews.current_pull(gh, cwd, target)
  if pull.get("state") != "open" or pull.get("merged"):
    raise HTTPException(409, "The pull request is no longer open. Nothing was posted.")
  if own_pull(row, pull):
    raise HTTPException(409, "GitHub does not allow a verdict on your own pull request. Nothing was posted.")


async def submit(db, row, item_key, target, proposal_id, *, gh, cwd, authorize):
  """Post the owner-submitted proposal once; return its settled receipt."""
  async with _github_connection_transaction():
    try:
      await asyncio.to_thread(_preflight, gh, cwd, row, target)  # unsettled: read-only (actor, PR head and author proof)
    except (ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError) as exc:
      raise HTTPException(409, "GitHub could not confirm this pull request. Nothing was posted.") from exc
    marker = f"<!-- mobius-review:{uuid.uuid4().hex} -->"
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      armed = _arm(db, row, item_key, proposal_id, marker, authorize)
    try:
      authorize()
    except HTTPException as exc:
      _settle(db, row, item_key, proposal_id, {"state": "not_dispatched", "summary": str(exc.detail)})
      raise
    body = f"{armed['body']}\n\n{marker}"
    result = await settled.run_settled(public.post_review, gh, cwd, target, body, armed["event"])
  fields = {"state": {"confirmed": "posted", "unknown": "unknown", "not_dispatched": "not_dispatched"}[result.state]}
  receipt = result.receipt or {}
  # The proposal id is its identity; GitHub's review id is a separate fact.
  fields.update({k: v for k, v in (("review_id", receipt.get("id")), ("url", receipt.get("url"))) if v is not None})
  if result.reason:
    fields["summary"] = result.reason
  _settle(db, row, item_key, proposal_id, fields)
  db.refresh(row)
  return row.outcomes_json[item_key]["verdict_proposal"]


def discard(db, row, item_key, proposal_id):
  saved = ((row.outcomes_json or {}).get(item_key) or {}).get("verdict_proposal") or {}
  if saved.get("id") != proposal_id or saved.get("state") != "proposed":
    raise HTTPException(409, "This verdict was already submitted or discarded. Refresh to see its result.")
  _settle(db, row, item_key, proposal_id, {"state": "discarded", "discarded_at": reviews.utc_now_iso()})
