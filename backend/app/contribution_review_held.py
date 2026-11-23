"""A review the platform held back because the pull request moved first.

Before it saves a verdict, the platform re-reads the PR. When the PR already has a newer head it refuses, keeps the
findings private and records why (``live_blocker``). The owner may still send that review, against the commit it covered
and worded with that, never as a verdict on the new head. The owner's "Post it anyway" only saves the exact proposal here;
sending it is the ordinary, confirmed submit of a saved verdict (contribution_review_verdicts.submit), so a formal verdict
is still one saved proposal the owner submits. The PR machine decides whether "Post it anyway" is allowed
(contribution_pr_machine, action ``post_at_reviewed_commit``).
"""
import asyncio
import subprocess

from fastapi import HTTPException

from app import contribution_pr_facts as pr_facts
from app import contribution_review_post_findings as post_findings
from app import contribution_review_public as public
from app import contribution_review_runs as reviews
from app import contribution_review_verdicts as verdicts
from app.contribution_errors import ContributionSubmitError

NOT_HELD = "There is no held-back review to post. Refresh to see the current state."
UNCONFIRMED = "GitHub could not confirm this pull request. Nothing was proposed."
NOT_MOVED = "The pull request has not moved since this review. Nothing was proposed."


def mark(item: dict, proposes_verdict: bool) -> dict:
  """What the run view says of one saved item: ``held_back`` when its finished review was refused for a moved PR and
  could still be sent. Only a run that proposes verdicts has a verdict to send."""
  held = proposes_verdict and bool(item.get("live_blocker")) and post_findings.refusal_code(item) is None
  return {"held_back": True} if held else {}


def not_posted_reply(reason: str, reviewed_head: str, current_head: str | None) -> dict:
  """What the agent that submitted a refused outcome is told: plainly that nothing was proposed or posted, and why."""
  moved = bool(current_head) and current_head != reviewed_head
  why = (f"the pull request moved from {reviewed_head[:7]} to {current_head[:7]} before this review could be saved"
         if moved else reason.strip().rstrip(".") or "the pull request could not be matched")
  return {"reason": reason, "reviewed_head": reviewed_head, "current_head": current_head if moved else None,
          "message": f"Nothing was proposed or posted: {why}. The findings stay private. Tell the owner this plainly "
                     "and end your turn; do not retry. They can send the review from Contribute with Post it anyway."}


async def current_head(gh, cwd, target) -> str | None:
  """The head the selected PR has now, when GitHub says it is that PR; None when it cannot say."""
  pull = await asyncio.to_thread(reviews.selected_pull, gh, cwd, target)  # unsettled: read-only (selected PR head)
  head = ((pull or {}).get("head") or {}).get("sha")
  return head if isinstance(head, str) and head else None


async def propose(db, row, item_key, target, *, proposes_verdict, gh, cwd, authorize, job) -> None:
  """Save the exact verdict proposal for a held-back review, worded with the commit it covered. Sends nothing."""
  item = (row.outcomes_json or {}).get(item_key) or {}
  if not mark(item, proposes_verdict):
    raise HTTPException(409, NOT_HELD)
  try:
    repo, pull = await asyncio.to_thread(reviews.moved_pull, gh, cwd, target)  # unsettled: read-only (same PR, any head)
    now = (pull.get("head") or {}).get("sha") or ""
    if now == target["head_sha"]:
      raise HTTPException(409, NOT_MOVED)
    facts = await asyncio.to_thread(pr_facts.read_facts, gh, cwd, {**target, "head_sha": now}, repo, pull,  # unsettled: read-only (GitHub reviews and checks)
                                    actor_id=row.github_actor_id, job=job)
  except (ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError) as exc:
    raise HTTPException(409, UNCONFIRMED) from exc
  pr_facts.require_allowed({**facts, "heldReview": {"head": target["head_sha"]}}, "post_at_reviewed_commit")
  outcome = {**item, "head_sha": target["head_sha"]}
  proposal = verdicts.propose(outcome, target, moved_to=now)
  authorize()
  saved = {key: value for key, value in item.items() if key != "live_blocker"}
  reviews.save_outcome(db, row, item_key, {**saved, "frozen_evidence": public.evidence(outcome), "verdict_proposal": proposal},
                       expected=item)
