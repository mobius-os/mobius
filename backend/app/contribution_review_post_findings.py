"""The owner posts a finished private review, once, as a plain COMMENT review.

A review that reported privately keeps its findings in Möbius. The owner may
later send exactly those words to GitHub: a read-only preview returns the text
and its hash, and the post names that hash, so what the owner read is what is
posted. Dispatch, its receipt and the one-review-per-head fence are
post_public_review's; nothing here retries an uncertain post.
"""
import asyncio
import hashlib
import subprocess

from fastapi import HTTPException

from app import contribution_review_public as public
from app import contribution_review_runs as reviews
from app import contribution_review_verdicts as verdicts
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_public_actions import post_public_review

# Unlike a refusal, an unconfirmed post may have reached GitHub: say so, never retry it.
UNCONFIRMED = {"code": "post_unconfirmed",
               "message": "GitHub may have posted it. Check the pull request before trying again."}
CHANGED = "This review changed since you read it. Nothing was posted. Open it again to read the current text."
HELD_RECEIPTS = {"posting", "posted", "unknown"}


def _refusal(item: dict) -> str | None:
  """Why this saved item has no private result to post, in the owner's words."""
  if item.get("blocked"):
    return "This review did not finish, so there is nothing to post. Review again first."
  if item.get("state") not in {"all_clear", "needs_you"} or not (item.get("summary") or "").strip():
    return "This review has no finished result to post."
  if item.get("verdict_proposal"):
    return "This review already has a verdict. Submit or discard that one."
  if (item.get("public_review") or {}).get("state") in HELD_RECEIPTS:
    return "This review was already sent to GitHub. Check the pull request."
  return None


def preview(item: dict, target: dict) -> dict:
  """The exact words a post would send for this saved item, and the hash that binds a post to them."""
  refusal = _refusal(item)
  if refusal:
    raise HTTPException(409, refusal)
  body = public.review_comment_body({**item, "head_sha": target["head_sha"]})
  return {"body": body, "preview_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
          "head_sha": target["head_sha"]}


def _require_someone_elses_open_pull(gh, cwd, row, target) -> None:
  _, pull = reviews.current_pull(gh, cwd, target)
  if pull.get("state") != "open" or pull.get("merged"):
    raise HTTPException(409, "The pull request is no longer open. Nothing was posted.")
  if verdicts.own_pull(row, pull):
    raise HTTPException(409, "A review of your own pull request stays private. Nothing was posted.")


async def post(db, row, item_key, target, preview_sha256, principal, cwd, *, gh, authorize) -> dict:
  """Post the previewed words once. Returns the confirmed receipt; refuses or reports uncertainty otherwise."""
  item = (row.outcomes_json or {}).get(item_key) or {}
  if preview(item, target)["preview_sha256"] != preview_sha256:
    raise HTTPException(409, CHANGED)
  try:
    await asyncio.to_thread(_require_someone_elses_open_pull, gh, cwd, row, target)  # unsettled: read-only (PR head and author proof)
  except (ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError) as exc:
    raise HTTPException(409, "GitHub could not confirm this pull request. Nothing was posted.") from exc
  outcome = await post_public_review(db, row, item_key, target, {**item, "head_sha": target["head_sha"]},
                                     principal, cwd, gh=gh, authorize=authorize)
  receipt = outcome.get("public_review") or {}
  # The receipt is saved whatever it says: a refusal before dispatch stays postable, an uncertain one does not.
  reviews.save_effect_outcome(db, row, item_key, outcome)
  if receipt.get("state") == "not_dispatched":
    raise HTTPException(409, receipt.get("summary") or "Nothing was posted.")
  if receipt.get("state") != "posted":
    raise HTTPException(502, UNCONFIRMED)
  return {"state": "posted", "url": receipt.get("url") or "", "review_id": receipt.get("id")}
