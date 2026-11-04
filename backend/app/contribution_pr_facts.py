"""Facts for the PR machine (contribution_pr_machine) from what the server reads on GitHub now.

The app builds the same facts from its own batched GraphQL read (pr-facts.js);
the server builds them from the REST pull request and repository it already
reads and its GraphQL check and merge read, which also carries the writers'
standing opinions (latestOpinionatedReviews, writers only, as in the app) and
the connected account's own latest review.
A read that failed is never a guess: its fact is 'unknown' and readComplete
is False, which the machine never lets enable anything that needs it.

The server leaves openThreads, result and outbox empty on purpose. No server
effect's guard depends on them: those facts only choose what the app
highlights, and every public attempt the server makes keeps its own receipt
fence (contribution_review_runs.require_public_transition_clear).
"""
from __future__ import annotations

import logging
import subprocess

from fastapi import HTTPException

from app import contribution_pr_machine as machine, contribution_review_runs as reviews
from app.contribution_errors import ContributionSubmitError

_log = logging.getLogger(__name__)

READ_FAILURES = (HTTPException, ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError)
ROLLUP_CHECKS = {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail", "PENDING": "pending", "EXPECTED": "pending"}
MERGEABLE = {"MERGEABLE": "yes", "CONFLICTING": "conflict"}
# The strongest permission GitHub reports for the connected account, in GitHub's viewerPermission words.
PERMISSIONS = (("admin", "ADMIN"), ("maintain", "MAINTAIN"), ("push", "WRITE"), ("triage", "TRIAGE"))


def authored_by(pull: dict, actor_id) -> bool:
  """Whether the connected GitHub account (by id, which survives renames) wrote this pull request."""
  actor = str(actor_id or "")
  return bool(actor) and str((pull.get("user") or {}).get("id") or "") == actor


def read_checks(gh, cwd, target: dict) -> dict | None:
  """GitHub's check, merge and review read for this pull request, or None when it could not be read."""
  try:
    return reviews.pull_checks(gh, cwd, target)
  except READ_FAILURES as exc:
    _log.info("PR machine facts: GitHub check read failed for %s#%s (%s)",
              target.get("repo"), target.get("number"), type(exc).__name__)
    return None


def _opinion(node: dict) -> dict:
  # A deleted account is GitHub's "ghost", never skipped; a review with no commit
  # counts as older: it blocks as a change request and never approves.
  author = node.get("author") or {}
  return {"by": author.get("login") or "ghost", "state": node["state"],
          "commit": (node.get("commit") or {}).get("oid") or "", "bot": author.get("__typename") == "Bot"}


def opinions(checks: dict | None) -> list[dict] | None:
  """Each writer's standing opinion, as GitHub keeps it; None when it was not read."""
  nodes = ((checks or {}).get("latestOpinionatedReviews") or {}).get("nodes") if isinstance(checks, dict) else None
  if not isinstance(nodes, list):
    return None
  return [_opinion(node) for node in nodes if isinstance(node, dict) and node.get("state")]


def my_review(checks: dict | None, review_list: list[dict], login: str) -> dict | None:
  """The connected account's latest review (the app's myReviewOf): a later comment or a
  pending draft never hides its standing opinion."""
  opinion = next((r for r in review_list if login and r["by"].lower() == login.lower()), None)
  standing = {"state": opinion["state"], "commit": opinion["commit"]} if opinion else None
  latest = (checks or {}).get("viewerLatestReview") if isinstance(checks, dict) else None
  oid = ((latest or {}).get("commit") or {}).get("oid")
  if not oid or latest.get("state") == "PENDING":
    return standing
  if latest.get("state") == "COMMENTED" and standing:
    return standing
  return {"state": latest.get("state"), "commit": oid}


def _login(user: dict) -> str:
  return str(user.get("login") or "").lower()


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


def pr_facts(*, actor_id, actor_login: str = "", repo: dict, pull: dict, checks: dict | None,
             job: dict | None) -> dict:
  """The machine's facts for one pull request, as the connected account sees it now.

  ``checks`` is GitHub's check, merge and review read, None when it failed;
  ``job`` is the live agent job another review run holds on this PR, if any.
  """
  own = authored_by(pull, actor_id)
  head = (pull.get("head") or {}).get("sha") or ""
  review_list = opinions(checks)
  return {
    "role": "author" if own else "reviewer", "author": _login(pull.get("user") or {}), "head": head,
    "lifecycle": lifecycle_of(pull), "checks": checks_of(checks, head),
    "mergeable": mergeable_of(checks, head), "permission": permission_of(repo),
    "reviews": review_list or [],
    "myReview": None if own else my_review(checks, review_list or [], actor_login),
    "openThreads": 0, "job": job, "result": None, "outbox": None,
    "readComplete": review_list is not None,
  }


def require_allowed(facts: dict, action_id: str) -> None:
  """Refuse an effect the PR machine does not allow, in the machine's own words (the app shows them)."""
  blocker = machine.action_blocker(facts, action_id)
  if blocker:
    raise HTTPException(409, blocker)


def read_facts(gh, cwd, target: dict, repo: dict, pull: dict, *, actor_id, actor_login: str = "",
               job: dict | None, checks: dict | None = None) -> dict:
  """Facts for a pull request the caller just read, adding its checks and reviews.

  ``target`` names it as the review machinery does (repo, number, pr_id).
  Pass ``checks`` when the caller already read them, so GitHub is asked once.
  ``actor_login`` finds the account's own standing opinion; without it only
  its latest review counts.
  """
  checks = checks if checks is not None else read_checks(gh, cwd, target)
  return pr_facts(actor_id=actor_id, actor_login=actor_login, repo=repo, pull=pull, checks=checks, job=job)
