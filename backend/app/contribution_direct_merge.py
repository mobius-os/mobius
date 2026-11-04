"""The owner's own "merge this exact pull-request version now".

There is no review run, agent or stored consent here: the owner's tap is the
authority, so GitHub is read again right before the one write and the write is
pinned to the head the owner saw. GitHub's own rules decide, through the review
machinery's merge_blocker, and so does the PR machine's merge guard on the
facts just read: someone other than the author approved this exact version, no
change request still stands, and no agent is working on the PR. Never an admin
bypass or a force. Nothing here approves, edits or comments, and one call
touches one pull request. A write whose outcome cannot be read is reported as
unconfirmed and is never repeated.
"""
import asyncio
import logging
import subprocess

from fastapi import HTTPException

from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine
from app import contribution_review_merge_actions as merge_actions
from app import contribution_review_runs as reviews
from app.contribution_errors import ContributionSubmitError
from app.github_connection import _github_connection_transaction

_log = logging.getLogger(__name__)

# A read that fails before any write proves that nothing was done.
_READ_FAILURES = (ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError)
UNREADABLE = ("This pull request could not be read from GitHub. Check that GitHub is "
              "connected and that the pull request still exists.")
CHANGED = "The pull request changed since you looked. Refresh and check it again."
QUEUE_CHOOSES_METHOD = ("This repository merges through a merge queue, which chooses the method "
                        "itself. Merge without choosing one.")
# Unlike a refusal, an unconfirmed write may have been applied: say so, never retry it.
UNCONFIRMED = {"code": "merge_unconfirmed",
               "message": "GitHub may have merged it. Check the pull request before trying again."}


def _read_object(gh, cwd, endpoint: str) -> dict:
  value = reviews.read(gh, cwd, endpoint)
  if not isinstance(value, dict):
    raise ValueError(f"GitHub answered {endpoint} with something other than an object")
  return value


def _require_same_open_pull(pull: dict, head_sha: str) -> None:
  """Refuse, naming why, unless this is still the open, ready version the owner saw."""
  if pull.get("merged"):
    raise HTTPException(409, "This pull request is already merged.")
  if pull.get("state") != "open":
    raise HTTPException(409, "This pull request is closed.")
  if (pull.get("head") or {}).get("sha") != head_sha:
    raise HTTPException(409, CHANGED)
  if pull.get("draft"):
    raise HTTPException(409, "This pull request is still a draft.")


def _merge_target(gh, cwd, body, repo: dict, pull: dict) -> dict:
  """The pull request as the merge machinery names it, with the base tip seen now."""
  pr_id, base_ref = pull.get("node_id"), (pull.get("base") or {}).get("ref")
  if not isinstance(pr_id, str) or not isinstance(base_ref, str):
    raise ValueError("GitHub named no id or base branch for this pull request")
  # GitHub's own spelling: a renamed repository's old name cannot take a write.
  name = repo.get("full_name") or body.repo
  return {"repo": name, "number": body.number, "head_sha": body.head_sha, "pr_id": pr_id,
          "base_ref": base_ref, "base_sha": reviews.base_head(gh, cwd, name, base_ref)}


def _require_clear(target: dict, repo: dict, pull: dict, checks: dict, facts: dict) -> None:
  if reviews.queue_entry(checks, target):
    raise HTTPException(409, "This pull request is already in the merge queue.")
  blocker = reviews.merge_blocker(target, repo, pull, checks) or machine.action_blocker(facts, "merge")
  if blocker:
    raise HTTPException(409, blocker)


def _read_mergeable_pull(gh, cwd, body, job) -> tuple[dict, dict, dict]:
  """Read GitHub now and refuse, in plain words, unless the exact head named can be merged.

  Returns what dispatch_merge takes: the merge target, the repository and the
  pull request's checks. This only reads. ``job`` is the agent job a review
  run holds on this PR, if any.
  """
  try:
    repo = _read_object(gh, cwd, f"repos/{body.repo}")
    pull = _read_object(gh, cwd, f"repos/{body.repo}/pulls/{body.number}")
    # Refusals are 409s and pass through this try; only an unreadable GitHub is a 404.
    _require_same_open_pull(pull, body.head_sha)
    target = _merge_target(gh, cwd, body, repo, pull)
    checks = reviews.pull_checks(gh, cwd, target)
  except _READ_FAILURES as exc:
    raise HTTPException(404, UNREADABLE) from exc
  # The merge guard does not depend on who presses Merge, so no account read is needed.
  facts = pr_facts.read_facts(gh, cwd, target, repo, pull, actor_id=None, job=job, checks=checks)
  _require_clear(target, repo, pull, checks, facts)
  return target, repo, checks


def _method_to_send(requested: str | None, repo: dict, checks: dict) -> str | None:
  """The method for the merge call, or None when the repository's merge queue chooses it."""
  if checks["isMergeQueueEnabled"]:
    if requested:
      raise HTTPException(409, QUEUE_CHOOSES_METHOD)
    return None
  if requested is None:
    return reviews.merge_method(repo)
  allowed = reviews.allowed_merge_methods(repo)
  if requested not in allowed:
    raise HTTPException(409, f"This repository does not allow {requested} merges. "
                             f"It allows: {', '.join(allowed) or 'none'}.")
  return requested


def _answer(result, body, method: str | None) -> dict:
  """The owner's answer for a settled dispatch; only a confirmed one claims success."""
  if result.state == "not_dispatched":
    raise HTTPException(409, result.reason)
  if result.state != "confirmed":
    raise HTTPException(502, UNCONFIRMED)
  return {"state": result.receipt["state"], "merge_sha": result.receipt.get("merge_sha"),
          "method": method, "repo": body.repo, "number": body.number, "head_sha": body.head_sha}


async def _merge(gh, cwd, body, authorize, job) -> dict:
  authorize()
  async with _github_connection_transaction():
    target, repo, checks = await asyncio.to_thread(_read_mergeable_pull, gh, cwd, body, job)  # unsettled: read-only (GitHub pull request, reviews, checks and base tip)
    method = _method_to_send(body.method, repo, checks)
    result = await merge_actions.dispatch_merge(gh, cwd, target, repo, checks, authorize, method)
  # dispatch_merge turns a cancellation before the write into a refusal. The
  # caller's cancellation still wins, as in every review merge.
  if result.state == "not_dispatched" and asyncio.current_task().cancelling():
    raise asyncio.CancelledError()
  return _answer(result, body, method)


def _refusal_summary(exc: HTTPException) -> str:
  detail = exc.detail["code"] if isinstance(exc.detail, dict) else exc.detail
  return f"HTTP {exc.status_code}, {detail}"


async def merge_exact_pull(gh, cwd, body, authorize, job: dict | None = None) -> dict:
  """Merge or enqueue the exact head the owner named, once, and report what GitHub confirmed.

  `body` carries repo, number, head_sha and an optional method. `authorize`
  raises an HTTPException once the caller's authority no longer stands; it runs
  before any GitHub call and again right before the write. `job` is the agent
  job a review run holds on this PR (the PR machine's job fact). Raises
  HTTPException: 404 when GitHub cannot be read, 409 for a refusal that changed
  nothing and 502 when the write's outcome is unconfirmed. Logs one line per attempt.
  """
  outcome = "ended without an answer; GitHub may or may not have applied it"
  try:
    answer = await _merge(gh, cwd, body, authorize, job)
    outcome = answer["state"]
    return answer
  except HTTPException as exc:
    outcome = _refusal_summary(exc)
    raise
  finally:
    _log.info("Direct merge of %s#%s at %s: %s", body.repo, body.number, body.head_sha, outcome)
