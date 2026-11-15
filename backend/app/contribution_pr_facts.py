"""Facts for the PR machine (contribution_pr_machine) from what the server reads on GitHub now.

The app builds the same facts from its own batched GraphQL read (pr-facts.js);
the server builds them from the REST pull request and repository it already
reads and its GraphQL check and merge read, which also carries the writers'
standing opinions (latestOpinionatedReviews, writers only, as in the app), each
person's newest review (latestReviews, which GitHub cannot limit to writers)
of which only the plain comments are kept: evidence that they looked, never an
opinion, and harmless for a non-writer because the machine uses a comment only
for someone who holds a writers-only change request. The read also carries the
connected account's own latest review.
An approval of an older commit is asked about once more, in read_facts: whether
the PR's own change is the same at the head (contribution_same_work).
A read that failed is never a guess: its fact is 'unknown' and readComplete
is False, which the machine never lets enable anything that needs it.

The server leaves openThreads, result and outbox empty on purpose. No server
effect's guard depends on them: those facts only choose what the app
highlights, and every public attempt the server makes keeps its own receipt
fence (contribution_review_runs.require_public_transition_clear).
"""
from __future__ import annotations

import json
import logging
import subprocess

from fastapi import HTTPException

from app import contribution_pr_machine as machine, contribution_review_runs as reviews
from app import contribution_same_work as same_work
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


def _review_nodes(checks: dict | None, field: str) -> list | None:
  connection = checks.get(field) if isinstance(checks, dict) else None
  nodes = connection.get("nodes") if isinstance(connection, dict) else None
  return nodes if isinstance(nodes, list) else None


def opinions(checks: dict | None) -> list[dict] | None:
  """Each writer's standing opinion, as GitHub keeps it; None when it was not read."""
  nodes = _review_nodes(checks, "latestOpinionatedReviews")
  if nodes is None:
    return None
  return [_opinion(node) for node in nodes if isinstance(node, dict) and node.get("state")]


def comments(checks: dict | None) -> list[dict]:
  """Each person's newest review when it was a plain comment: proof they looked at that version.

  GitHub's opinions skip comments, so a reviewer who comments after asking for
  changes keeps their request, and this is the only place that says they came
  back. Not reading them only leaves that unsaid, so a missing answer is no
  comments, never a reason to call the PR unread.
  """
  return [_opinion(node) for node in _review_nodes(checks, "latestReviews") or []
          if isinstance(node, dict) and node.get("state") == "COMMENTED"]


# A hundred reviewers a page; past this many pages the read is refused rather than guessed.
APPROVER_PAGES = 10
_APPROVERS = ('query($id:ID!,$after:String){node(id:$id){... on PullRequest {latestReviews(first:100, after:$after)'
              '{pageInfo{hasNextPage endCursor} nodes{state author{login} commit{oid}}}}}}')


def approvers_at(gh, cwd, target: dict) -> set[str]:
  """Lowercased logins whose newest review is an approval of exactly target["head_sha"], read to the last page.

  Anyone: a collaborator with read access approves too, which the writers-only opinions of read_facts never show, and
  those stop at a hundred. Only this read may say who can safely be asked to review again. Raises READ_FAILURES when
  GitHub cannot answer in full, because a missing approver would be asked a second time.
  """
  found, cursor = set(), None
  for _ in range(APPROVER_PAGES):
    flags = ["-f", f"query={_APPROVERS}", "-f", f"id={target['pr_id']}"] + (["-f", f"after={cursor}"] if cursor else [])
    result = json.loads(gh(cwd, "api", "graphql", *flags).stdout)
    reviewed = ((result.get("data") or {}).get("node") or {}).get("latestReviews") if isinstance(result, dict) else None
    nodes = reviewed.get("nodes") if isinstance(reviewed, dict) else None
    paging = reviewed.get("pageInfo") if isinstance(reviewed, dict) else None
    if not isinstance(result, dict) or result.get("errors") or not isinstance(nodes, list) or not isinstance(paging, dict):
      raise HTTPException(409, "GitHub could not show who approved this pull request.")
    found |= {str((node.get("author") or {}).get("login") or "").lower() for node in nodes
              if isinstance(node, dict) and node.get("state") == "APPROVED"
              and (node.get("commit") or {}).get("oid") == target["head_sha"]}
    cursor = paging.get("endCursor")
    if paging.get("hasNextPage") is not True:
      return found - {""}
    if not cursor:
      break
  raise HTTPException(409, "GitHub showed too many reviews to tell who approved this pull request.")


def everyone_who_reviewed(checks: dict | None) -> list[dict]:
  """Each person's newest review, whoever they are and whatever it said: who can be asked, never who may merge.

  Merge standing is the writers' opinions alone (`opinions`). A person without write access still reviewed
  the PR, and GitHub lets anyone be asked, so the two Ask actions read this list instead. A draft they have
  not submitted (PENDING) is nobody's review. A missing answer is nobody, never a reason to call the PR unread.
  """
  return [_opinion(node) for node in _review_nodes(checks, "latestReviews") or []
          if isinstance(node, dict) and node.get("state") and node["state"] != "PENDING"]


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
    "reviews": [*(review_list or []), *comments(checks)],
    "reviewers": everyone_who_reviewed(checks),
    "myReview": None if own else my_review(checks, review_list or [], actor_login),
    "openThreads": 0, "job": job, "result": None, "outbox": None,
    "readComplete": review_list is not None,
  }


def require_allowed(facts: dict, action_id: str) -> None:
  """Refuse an effect the PR machine does not allow, in the machine's own words (the app shows them)."""
  blocker = machine.action_blocker(facts, action_id)
  if blocker:
    raise HTTPException(409, blocker)


def _base_tip(gh, cwd, target: dict, pull: dict) -> str | None:
  """The base branch's current tip: the caller's own read when it holds one, else GitHub's answer now."""
  if target.get("base_sha"):
    return target["base_sha"]
  base_ref = (pull.get("base") or {}).get("ref")
  if not isinstance(base_ref, str):
    return None
  try:
    return reviews.base_head(gh, cwd, target["repo"], base_ref)
  except READ_FAILURES as exc:
    _log.info("PR machine facts: base branch read failed for %s#%s (%s)",
              target.get("repo"), target.get("number"), type(exc).__name__)
    return None


def _mark_carried_approvals(gh, cwd, target: dict, pull: dict, facts: dict) -> None:
  """Prove, for an approval of an older commit, whether the PR's own change is the same (sameWork).

  Costs nothing unless such an approval could decide a merge: an open PR that the
  connected account can merge and nobody has approved at the head. A read that fails
  leaves the approval unproved, which the machine treats as an approval of an older version.
  """
  review_list, head, author = facts["reviews"], facts["head"], facts["author"]
  if facts["lifecycle"] != "open" or not machine.may_write(facts["permission"]):
    return
  if not same_work.carried_candidates(review_list, head, author):
    return
  base_sha = _base_tip(gh, cwd, target, pull)
  if base_sha:
    same_work.mark_carried_approvals(gh, cwd, target.get("repo"), base_sha, head, review_list, author=author)


def read_facts(gh, cwd, target: dict, repo: dict, pull: dict, *, actor_id, actor_login: str = "",
               job: dict | None, checks: dict | None = None) -> dict:
  """Facts for a pull request the caller just read, adding its checks and reviews.

  ``target`` names it as the review machinery does (repo, number, pr_id).
  Pass ``checks`` when the caller already read them, so GitHub is asked once.
  ``actor_login`` finds the account's own standing opinion; without it only
  its latest review counts. An approval of an older commit comes back marked
  with ``sameWork`` when the PR's own change is the same at the head.
  """
  checks = checks if checks is not None else read_checks(gh, cwd, target)
  facts = pr_facts(actor_id=actor_id, actor_login=actor_login, repo=repo, pull=pull, checks=checks, job=job)
  _mark_carried_approvals(gh, cwd, target, pull, facts)
  return facts
