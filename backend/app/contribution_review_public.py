"""Explicit public reviews: immutable private evidence, bounded public copy.

A COMMENT needs the route's frozen opt-in and persisted attempt receipt. A
formal APPROVE or REQUEST_CHANGES additionally needs the owner's own submit of
one exact saved proposal (see contribution_review_verdicts). An uncertain
effect is never retried.
"""
import json
import subprocess
from fastapi import HTTPException
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_effects import DispatchResult, classify_dispatch_error

EVIDENCE_MAX_CHARS = 200_000
EVIDENCE_FIELDS = ("state", "summary", "scope", "tests", "tests_passed", "reviewed_base_sha", "head_sha")


def evidence(outcome):
  return {field: outcome.get(field) for field in EVIDENCE_FIELDS}


def require_same_evidence(previous, proposed):
  """Posting or proposing freezes judgment, not merely the network receipt."""
  receipt = previous.get("public_review") or {}
  if receipt.get("state") not in {"posting", "posted", "unknown"} and not previous.get("verdict_proposal"):
    return False
  # Later merge gates may rewrite state/summary; the published judgment stays.
  frozen = previous.get("frozen_evidence") or evidence(previous)
  if frozen != evidence(proposed):
    raise HTTPException(409, "This public review already froze different evidence. Its verdict cannot be changed.")
  return True


# GitHub rejects a review body above 65,536 characters. Measure in UTF-16 code
# units, which is never less than the code-point count, so the bound holds
# whichever way GitHub counts.
GITHUB_REVIEW_BODY_MAX = 65536


def _body_units(text: str) -> int:
  return len(text.encode("utf-16-le")) // 2


def _clip_evidence(text: str, limit: int) -> str:
  """Keep `text` whole if it fits, else its start plus an explicit omission marker."""
  if _body_units(text) <= limit:
    return text
  def marker(omitted):
    return f"\n\n… {omitted} characters omitted; the full evidence is in Möbius."
  # Code points never exceed UTF-16 units, so shrink by the overflow until it fits.
  keep = max(0, limit - _body_units(marker(len(text))))
  while keep and (excess := _body_units(text[:keep] + marker(len(text) - keep)) - limit) > 0:
    keep = max(0, keep - excess)
  return text[:keep].rstrip() + marker(len(text) - keep)


VERDICT_HEADERS = {"COMMENT": None, "APPROVE": "Approved", "REQUEST_CHANGES": "Changes requested"}


def review_comment_body(outcome: dict, *, marker: str = "", event: str = "COMMENT") -> str:
  """The public text of an owner-requested review: verdict, findings, checks.

  Stored evidence is never cut. Only the posted copy is bounded to GitHub's
  limit, and any omission is stated in the body, never silent. The verdict
  header, the checks and the reviewed-head footer always survive.
  """
  verdict = VERDICT_HEADERS[event] or ("All clear" if outcome.get("state") == "all_clear" else "Changes suggested")
  header = f"**Möbius review: {verdict}**"
  summary = (outcome.get("summary") or "").strip()
  tests = (outcome.get("tests") or "").strip()
  footer = f"_Reviewed at {str(outcome.get('head_sha') or '')[:12]}._"
  if marker:
    footer += f"\n\n{marker}"
  def render(summary_text, tests_text):
    parts = [header, "", summary_text]
    if tests_text:
      parts += ["", f"**Verification:** {tests_text}"]
    return "\n".join(parts + ["", footer])
  body = render(summary, tests)
  if _body_units(body) <= GITHUB_REVIEW_BODY_MAX:
    return body
  available = GITHUB_REVIEW_BODY_MAX - _body_units(render("", ""))
  if tests:
    available -= _body_units("\n\n**Verification:** ")
  # Checks keep at least a quarter of the room, the summary takes the rest.
  tests_room = min(_body_units(tests), max(available - _body_units(summary), available // 4))
  return render(_clip_evidence(summary, available - tests_room), _clip_evidence(tests, tests_room))


def post_review(gh, cwd, target, body, event="COMMENT"):
  """The first transport call is the dispatch boundary; failures after it fence replay."""
  if event not in VERDICT_HEADERS:
    return DispatchResult.not_dispatched("Unknown review event.")
  if _body_units(body) > GITHUB_REVIEW_BODY_MAX:
    return DispatchResult.not_dispatched("The public review exceeds GitHub's body limit.")
  try:
    response = gh(cwd, "api", "--method", "POST",
      f"repos/{target['repo']}/pulls/{target['number']}/reviews", "--input", "-",
      input_text=json.dumps({"commit_id": target["head_sha"], "event": event, "body": body})).stdout
    result = json.loads(response)
    if (not isinstance(result, dict) or not isinstance(result.get("id"), int)
        or isinstance(result["id"], bool) or result["id"] <= 0):
      return DispatchResult.unknown("GitHub did not confirm the posted review.")
    return DispatchResult.confirmed({"id": result["id"], "url": result.get("html_url")})
  except (ContributionSubmitError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
    # Programming errors must surface, not become durable false uncertainty.
    return classify_dispatch_error(exc)


REVIEW_STATES = {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}


def reconcile_review(gh, cwd, target, receipt, actor_id, event="COMMENT"):
  """Find only this armed attempt, without ever treating absence as permission to retry.

  A bounded scan may miss a very old review; that leaves uncertainty intact.
  Legacy receipts without a unique marker cannot be attributed to this grant.
  """
  marker = receipt.get("marker")
  if not marker:
    return {**receipt, "state": "unknown", "summary": "This older receipt lacks a unique review marker; inspect GitHub without repeating the write."}
  try:
    for page in range(1, 11):
      response = gh(cwd, "api", f"repos/{target['repo']}/pulls/{target['number']}/reviews?per_page=100&page={page}")
      rows = json.loads(response.stdout)
      if not isinstance(rows, list):
        break
      for review in rows:
        if (isinstance(review, dict) and isinstance(review.get("user"), dict)
            and str(review["user"].get("id")) == str(actor_id)
            and review.get("commit_id") == target["head_sha"]
            and review.get("state") == REVIEW_STATES[event]
            and isinstance(review.get("body"), str) and marker in review["body"]
            and isinstance(review.get("id"), int) and not isinstance(review["id"], bool)):
          return {**receipt, "state": "posted", "id": review["id"], "url": review.get("html_url"),
                  "summary": "The exact public review was confirmed by a read-only observation."}
      if len(rows) < 100:
        break
  except (ContributionSubmitError, subprocess.SubprocessError, OSError, json.JSONDecodeError):
    pass
  return {**receipt, "state": "unknown", "summary": "The exact public review is not confirmed; no write was repeated."}
