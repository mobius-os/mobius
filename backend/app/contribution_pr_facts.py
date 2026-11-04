"""Facts for the PR machine (contribution_pr_machine) from what the server reads on GitHub now.

The app builds the same facts from its own batched GraphQL read (pr-facts.js);
the server builds them from the REST pull request and repository it already
reads, its GraphQL check and merge read, and the pull request's review list.
A read that failed is never a guess: its fact is 'unknown' and readComplete
is False, which the machine never lets enable anything that needs it.

The server leaves openThreads, result and outbox empty on purpose. No server
effect's guard depends on them: those facts only choose what the app
highlights, and every public attempt the server makes keeps its own receipt
fence (contribution_review_runs.require_public_transition_clear).
"""
from __future__ import annotations

import subprocess

from fastapi import HTTPException

from app import contribution_pr_machine as machine, contribution_review_runs as reviews
from app.contribution_errors import ContributionSubmitError

READ_FAILURES = (HTTPException, ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError)
REVIEW_PAGE_SIZE = 100
REVIEW_PAGES = 10
OPINIONS = ("APPROVED", "CHANGES_REQUESTED")
ROLLUP_CHECKS = {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail", "PENDING": "pending", "EXPECTED": "pending"}
MERGEABLE = {"MERGEABLE": "yes", "CONFLICTING": "conflict"}
# The strongest permission GitHub reports for the connected account, in GitHub's viewerPermission words.
PERMISSIONS = (("admin", "ADMIN"), ("maintain", "MAINTAIN"), ("push", "WRITE"), ("triage", "TRIAGE"))


def authored_by(pull: dict, actor_id) -> bool:
  """Whether the connected GitHub account (by id, which survives renames) wrote this pull request."""
  actor = str(actor_id or "")
  return bool(actor) and str((pull.get("user") or {}).get("id") or "") == actor


def _review_page(gh, cwd, repo: str, number: int, page: int) -> list | None:
  try:
    rows = reviews.read(gh, cwd, f"repos/{repo}/pulls/{number}/reviews?per_page={REVIEW_PAGE_SIZE}&page={page}")
  except READ_FAILURES:
    return None
  return rows if isinstance(rows, list) else None


def read_reviews(gh, cwd, repo: str, number: int) -> list[dict] | None:
  """Every review on the pull request, oldest first; None unless GitHub's whole list was read."""
  found: list[dict] = []
  for page in range(1, REVIEW_PAGES + 1):
    rows = _review_page(gh, cwd, repo, number, page)
    if rows is None:
      return None
    found.extend(row for row in rows if isinstance(row, dict))
    if len(rows) < REVIEW_PAGE_SIZE:
      return found
  return None  # Past the page cap: an incomplete list could hide a change request.


def read_checks(gh, cwd, target: dict) -> dict | None:
  """GitHub's check and merge read for this pull request, or None when it could not be read."""
  try:
    return reviews.pull_checks(gh, cwd, target)
  except READ_FAILURES:
    return None


def _login(user: dict) -> str:
  return str(user.get("login") or "").lower()


def opinions(review_list: list[dict], author_id) -> list[dict]:
  """Each non-author's standing opinion: their latest approval or change request.

  A later dismissal clears it, and a comment never replaces an opinion. Bots
  are tagged; the machine never counts them as approvals or blockers.
  """
  standing: dict[str, dict] = {}
  for review in review_list:
    user = review.get("user") or {}
    if not _login(user) or str(user.get("id")) == str(author_id):
      continue
    if review.get("state") == "DISMISSED":
      standing.pop(_login(user), None)
    elif review.get("state") in OPINIONS:
      standing[_login(user)] = {"by": _login(user), "state": review["state"],
                                "commit": review.get("commit_id"), "bot": user.get("type") == "Bot"}
  return list(standing.values())


def my_review(review_list: list[dict], actor_id) -> dict | None:
  """The connected account's latest submitted review of any state (a pending draft is not one)."""
  mine = [review for review in review_list
          if str((review.get("user") or {}).get("id")) == str(actor_id) and review.get("state") != "PENDING"]
  return {"state": mine[-1].get("state"), "commit": mine[-1].get("commit_id")} if mine else None


def lifecycle_of(pull: dict) -> str:
  if pull.get("merged") is True:
    return "merged"
  if pull.get("state") == "closed":
    return "closed"
  if pull.get("state") != "open":
    return "unknown"
  return "draft" if pull.get("draft") is True else "open"


def checks_of(checks: dict | None, head: str) -> str:
  """pass, fail or pending from the head commit's rollup; 'none' only when GitHub says no checks exist."""
  if not isinstance(checks, dict) or checks.get("headRefOid") != head:
    return "unknown"
  commits = (checks.get("commits") or {}).get("nodes") or []
  commit = (commits[0].get("commit") or {}) if len(commits) == 1 else {}
  if "statusCheckRollup" not in commit:
    return "unknown"
  rollup = commit["statusCheckRollup"]
  if rollup is None:
    return "none"
  return ROLLUP_CHECKS.get(rollup.get("state"), "unknown")


def mergeable_of(checks: dict | None, head: str) -> str:
  if not isinstance(checks, dict) or checks.get("headRefOid") != head:
    return "unknown"
  return MERGEABLE.get(checks.get("mergeable"), "unknown")


def permission_of(repo: dict) -> str:
  """The connected account's rights here; an archived or disabled repository takes no writes."""
  if repo.get("archived") or repo.get("disabled"):
    return "READ"
  granted = repo.get("permissions") or {}
  return next((level for name, level in PERMISSIONS if granted.get(name) is True), "READ")


def pr_facts(*, actor_id, repo: dict, pull: dict, checks: dict | None,
             review_list: list[dict] | None, job: dict | None) -> dict:
  """The machine's facts for one pull request, as the connected account sees it now.

  ``checks`` and ``review_list`` are None when their read failed; ``job`` is
  the live agent job another review run holds on this PR, if any.
  """
  own = authored_by(pull, actor_id)
  author = pull.get("user") or {}
  head = (pull.get("head") or {}).get("sha") or ""
  return {
    "role": "author" if own else "reviewer", "author": _login(author), "head": head,
    "lifecycle": lifecycle_of(pull), "checks": checks_of(checks, head),
    "mergeable": mergeable_of(checks, head), "permission": permission_of(repo),
    "reviews": opinions(review_list or [], author.get("id")),
    "myReview": None if own else my_review(review_list or [], actor_id),
    "openThreads": 0, "job": job, "result": None, "outbox": None,
    "readComplete": checks is not None and review_list is not None,
  }


def require_allowed(facts: dict, action_id: str) -> None:
  """Refuse an effect the PR machine does not allow, in the machine's own words (the app shows them)."""
  blocker = machine.action_blocker(facts, action_id)
  if blocker:
    raise HTTPException(409, blocker)


def read_facts(gh, cwd, target: dict, repo: dict, pull: dict, *, actor_id, job: dict | None,
               checks: dict | None = None) -> dict:
  """Facts for a pull request the caller just read, adding its review list and checks.

  ``target`` names it as the review machinery does (repo, number, pr_id).
  Pass ``checks`` when the caller already read them, so GitHub is asked once.
  """
  checks = checks if checks is not None else read_checks(gh, cwd, target)
  review_list = read_reviews(gh, cwd, target["repo"], target["number"])
  return pr_facts(actor_id=actor_id, repo=repo, pull=pull, checks=checks,
                  review_list=review_list, job=job)
