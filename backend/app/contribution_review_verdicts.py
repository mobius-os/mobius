"""Formal review verdicts: the agent proposes, the owner submits.

A run with the frozen ``propose_verdict`` opt-in never posts APPROVE or
REQUEST_CHANGES by itself. Its ``publish:true`` outcome saves one immutable
proposal (event, exact wording, pinned head and base), plus the same findings
worded as a plain COMMENT. The owner's own submit of that exact proposal, as
proposed or downgraded to the COMMENT, is the authority to post it, once, at
the pinned head; an uncertain dispatch is never retried. The one exception is
the owner's own choice when starting the review (``agent_submits_verdict``):
the run then submits its proposal under that frozen grant. A proposal is not
consent to merge: a merge grant still needs the posted APPROVE and its gates;
a COMMENT is never an approval.
"""
import asyncio
import hashlib
import subprocess
import uuid

from fastapi import HTTPException

from app import chat_queue, models, settled, contribution_review_runs as reviews
from app import contribution_pr_facts as pr_facts, contribution_review_public as public
from app import contribution_review_lifecycle as review_lifecycle
from app.contribution_errors import ContributionSubmitError
from app.decision_cards import fence_owner
from app.github_connection import _github_connection_transaction

EVENTS = {"all_clear": "APPROVE", "needs_you": "REQUEST_CHANGES"}


def own_pull(row, pull) -> bool:
  """GitHub refuses approve and request-changes from the PR's own author."""
  return pr_facts.authored_by(pull, row.github_actor_id)


def _sha256(text: str) -> str:
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def proposed_event(outcome) -> str:
  """Findings the reviewer does not hold the PR for are a plain comment, never a change request."""
  if outcome["state"] == "needs_you" and outcome.get("blocking") is False:
    return "COMMENT"
  return EVENTS[outcome["state"]]


def propose(outcome, target, moved_to="") -> dict:
  """One immutable proposal: the verdict's exact wording and the same findings as a plain comment.

  ``moved_to`` names the head the PR has since moved to, for a review of an older version the owner chose to send
  anyway: the wording says so, and submitting accepts that PR at any head (see submit)."""
  event = proposed_event(outcome)
  proposal_id = uuid.uuid4().hex
  # The marker is part of the frozen wording: GitHub's body limit is applied to
  # exactly what will be posted, so submitting can never overflow it.
  marker = f"<!-- mobius-review:{proposal_id} -->"
  body = public.review_comment_body(outcome, marker=marker, event=event, moved_to=moved_to)
  comment_body = public.review_comment_body(outcome, marker=marker, event="COMMENT", moved_to=moved_to)
  return {"id": proposal_id, "state": "proposed", "event": event, "marker": marker, "body": body,
          "body_sha256": _sha256(body), "comment_body": comment_body, "comment_body_sha256": _sha256(comment_body),
          "head_sha": target["head_sha"], "reviewed_base_sha": outcome.get("reviewed_base_sha"),
          "proposed_at": reviews.utc_now_iso(), **({"moved_to": moved_to} if moved_to else {})}


def _sent_as(saved: dict, event: str | None) -> dict:
  """The fields that change when the owner sends the proposal as a plain comment instead."""
  if event is None or event == saved.get("event"):
    return {}
  if event != "COMMENT" or not saved.get("comment_body"):
    raise HTTPException(409, "This verdict can only be sent as proposed. Nothing was posted.")
  return {"event": "COMMENT", "proposed_event": saved["event"]}


def _settle(db, row, item_key, proposal_id, fields):
  """Move only this exact proposal forward, on the refreshed current item."""
  def apply(current):
    saved = current.get("verdict_proposal") or {}
    if saved.get("id") != proposal_id or saved.get("state") in reviews.VERDICT_SETTLED:
      return None
    return {**current, "verdict_proposal": {**saved, **fields}}
  reviews.save_effect_outcome(db, row, item_key, apply)


def _arm(db, row, item_key, proposal_id, authorize, event=None):
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
  try:
    sent_as = _sent_as(saved, event)
  except HTTPException:
    db.rollback()
    raise
  current = row.outcomes_json[item_key]
  armed = {**current, "verdict_proposal": {**saved, **sent_as, "state": "posting",
                                           "submitted_at": reviews.utc_now_iso()}}
  if not reviews._cas_outcome(db, row, item_key, armed):
    raise HTTPException(409, "This review changed. Refresh before submitting.")
  return armed["verdict_proposal"]


def _preflight(gh, cwd, row, target, read_pull):
  actor = reviews.read(gh, cwd, "user")
  if str(actor.get("id") or "") != row.github_actor_id:
    raise HTTPException(409, "The connected GitHub account changed. Nothing was posted.")
  _, pull = read_pull(gh, cwd, target)
  if pull.get("state") != "open" or pull.get("merged"):
    raise HTTPException(409, "The pull request is no longer open. Nothing was posted.")
  if own_pull(row, pull):
    raise HTTPException(409, "GitHub does not allow a verdict on your own pull request. Nothing was posted.")


async def _prove_target(gh, cwd, row, target, read_pull):
  """Read-only proof of actor, open PR, head (per ``read_pull``) and authorship; a failed read proves non-dispatch."""
  try:
    await asyncio.to_thread(_preflight, gh, cwd, row, target, read_pull)  # unsettled: read-only (actor, PR head and author proof)
  except (ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError) as exc:
    raise HTTPException(409, "GitHub could not confirm this pull request. Nothing was posted.") from exc


def _persist_result(db, row, item_key, proposal_id, result):
  """Record what the dispatch returned: the proposal leaves `posting` only here."""
  fields = {"state": {"confirmed": "posted", "unknown": "unknown", "not_dispatched": "not_dispatched"}[result.state]}
  receipt = result.receipt or {}
  # The proposal id is its identity; GitHub's review id is a separate fact.
  fields.update({k: v for k, v in (("review_id", receipt.get("id")), ("url", receipt.get("url"))) if v is not None})
  if result.reason:
    fields["summary"] = result.reason
  if result.state == "unknown":
    _settle(db, row, item_key, proposal_id, fields)
  else:
    # The clock may have marked this same proposal `unknown` while the request was still in flight:
    # what its own dispatcher then learns for certain (a review id, a refusal) is the better fact.
    _settle_unconfirmed(db, row, item_key, proposal_id, fields)


async def submit(db, row, item_key, target, proposal_id, *, gh, cwd, authorize, event=None):
  """Post the owner-submitted proposal once, as proposed or (event COMMENT) as its plain comment.

  Returns its settled receipt; its ``event`` is what was actually sent.
  """
  # A proposal made for a review of an older version (moved_to) is sent against the commit it covered, so the PR may be at
  # any head; every other proposal still needs the PR at exactly the head it was proposed for.
  saved = ((row.outcomes_json or {}).get(item_key) or {}).get("verdict_proposal") or {}
  read_pull = reviews.moved_pull if saved.get("id") == proposal_id and saved.get("moved_to") else reviews.current_pull
  async with _github_connection_transaction():
    await _prove_target(gh, cwd, row, target, read_pull)
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      armed = _arm(db, row, item_key, proposal_id, authorize, event)
      # Admission and the reservation's database fence can both wait, so the
      # proof above may be stale. Repeat it now that the dispatch is reserved;
      # anything that ends before the post is a definite non-dispatch.
      try:
        await _prove_target(gh, cwd, row, target, read_pull)
      except BaseException as exc:
        summary = str(exc.detail) if isinstance(exc, HTTPException) else "The request ended before anything was posted."
        _settle(db, row, item_key, proposal_id, {"state": "not_dispatched", "summary": summary})
        raise
    try:
      authorize()
    except HTTPException as exc:
      _settle(db, row, item_key, proposal_id, {"state": "not_dispatched", "summary": str(exc.detail)})
      raise
    # Both wordings carry the proposal's marker, frozen when it was proposed.
    wording = armed["comment_body"] if armed["event"] == "COMMENT" else armed["body"]
    # A caller cancelled during the post still records what GitHub answered.
    result = await settled.settle(
      asyncio.to_thread(public.post_review, gh, cwd, target, wording, armed["event"]),
      what="Verdict dispatch",
      abandoned=lambda task: _persist_result(db, row, item_key, proposal_id, task.result()))
  _persist_result(db, row, item_key, proposal_id, result)
  db.refresh(row)
  return row.outcomes_json[item_key]["verdict_proposal"]


NEVER_SENT = "This verdict was never sent to GitHub, so nothing is waiting on you."
NO_ANSWER = ("GitHub did not answer within 5 minutes. This verdict is not sent again; "
             "it is only read back from GitHub.")
DISMISSED = "You dismissed this verdict. GitHub never confirmed it and nothing more will be sent."


def _settle_unconfirmed(db, row, item_key, proposal_id, fields):
  """Move one unconfirmed attempt to a final state on the refreshed item."""
  def apply(current):
    saved = current.get("verdict_proposal") or {}
    if saved.get("id") != proposal_id or saved.get("state") not in {"posting", "unknown"}:
      return None
    # The clock's "no answer" line stops being true once an answer arrives.
    kept = {k: v for k, v in saved.items() if not (k == "summary" and v == NO_ANSWER and "summary" not in fields)}
    return {**current, "verdict_proposal": {**kept, **fields}}
  reviews.save_effect_outcome(db, row, item_key, apply)


def settle_observed(db, row, item_key, proposal, review, *, complete_scan, read_at):
  """Settle an unconfirmed verdict from one read-only look at GitHub's reviews.

  ``review`` is the exact review carrying this attempt's marker, if GitHub shows
  it: the verdict is then posted. With no such review the verdict is released as
  never sent only once the read covered GitHub's whole list and began after the
  one-hour dispatch window that bounds how long a request can still land. Both
  settle this one record; nothing here ever submits again.
  """
  if review is not None:
    fields = {"state": "posted", "review_id": review["id"], "url": review.get("html_url"),
              "summary": "The exact public review was confirmed by a read-only observation."}
    if review.get("state") == public.DISMISSED_STATE:
      # It was sent, so it is not "never sent"; but it is no longer a live verdict.
      fields.update(dismissed_on_github=True, summary="The review was sent, then dismissed on GitHub.")
  elif complete_scan and reviews.dispatch_window_closed(proposal.get("submitted_at"), read_at):
    fields = {"state": "not_dispatched", "summary": NEVER_SENT}
  else:
    return
  _settle_unconfirmed(db, row, item_key, proposal["id"], fields)


def answer_overdue(proposal: dict | None, now: int) -> dict | None:
  """A posting verdict that never got an answer, as the unconfirmed verdict it now is; None while it may still land.

  The request that armed it ends within its own timeouts, so a proposal still `posting` after the machine's
  5 minutes lost its answer (the process died, say). It becomes `unknown`: read back from GitHub, never
  resent, and the owner can discard it. The marker search that can settle it is `settle_observed`.

  The 5 minutes start when the proposal is armed (`submitted_at`), which is before the second proof read
  and the dispatch. Under a very slow GitHub the flip can therefore come first: the request that is still
  in flight records its own definitive result over it (a review id, or a refusal after the post), and a post
  that never reports back is read back by `settle_observed`. A proof that fails before the post can no longer
  settle `not_dispatched` (an `unknown` verdict is refused that), so the owner discards it. Safe: nothing is
  ever resent.
  """
  vector = review_lifecycle.verdict_timeout(proposal, now) if proposal else None
  if vector is None:
    return None
  return {**proposal, "state": "unknown", "summary": NO_ANSWER, review_lifecycle.KEY: vector}


def dismiss(db, row, item_key, proposal_id):
  """The owner's word that an unconfirmed verdict is not worth waiting on.

  Only a settled uncertainty qualifies: a verdict still posting has a live
  request whose own result must land. This records the decision and sends nothing.
  """
  saved = ((row.outcomes_json or {}).get(item_key) or {}).get("verdict_proposal") or {}
  if saved.get("id") != proposal_id or saved.get("state") != "unknown":
    raise HTTPException(409, "Only a verdict GitHub never confirmed can be discarded. Refresh to see its result.")
  _settle_unconfirmed(db, row, item_key, proposal_id,
                      {"state": "not_dispatched", "summary": DISMISSED, "dismissed_at": reviews.utc_now_iso()})


def discard(db, row, item_key, proposal_id):
  saved = ((row.outcomes_json or {}).get(item_key) or {}).get("verdict_proposal") or {}
  if saved.get("id") != proposal_id or saved.get("state") != "proposed":
    raise HTTPException(409, "This verdict was already submitted or discarded. Refresh to see its result.")
  _settle(db, row, item_key, proposal_id, {"state": "discarded", "discarded_at": reviews.utc_now_iso()})
