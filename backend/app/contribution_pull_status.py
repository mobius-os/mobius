"""The owner's own status changes on one pull request, at the version they saw.

Open for review, back to draft, close, request review, ask for re-review, and ask
for approval: each is a tap in Contribute behind its confirm sheet. As with Merge now
(contribution_direct_merge) there is no run, agent or stored consent: the
owner's tap is the authority. GitHub is read again right before the one
write, and the PR machine is asked on those facts, so the refusal is the
reason the app showed. These writes cannot name a head, so a push landing
between the read and the write changes the version they apply to: the head
GitHub reports afterwards is returned as observed_head_sha. A write whose
outcome cannot be read is reported as unconfirmed and is never repeated.
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import HTTPException

from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine, settled
from app import contribution_review_public as public, contribution_review_runs as reviews
from app.contribution_direct_merge import CHANGED, UNREADABLE, _READ_FAILURES, _read_object, _refusal_summary
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_effects import (DispatchResult, NotDispatched, _gh_http_status,
                                             classify_dispatch_error, graphql_mutation_result)
from app.github_connection import _github_connection_transaction

_log = logging.getLogger(__name__)

# What GitHub reads as a mention in a comment; the ask-approval note may name only the people asked.
MENTION = re.compile(r"@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
# The note carries the head and names the person, so the same words on the PR are the same ask.
NOTE_ALREADY_THERE = "This exact note is already on the pull request. Wait for their reply."
REPEAT_WINDOW = timedelta(days=7)

# Opening a draft can satisfy the last condition of an armed auto-merge: that would be a merge.
AUTO_MERGE_ON = ("Auto-merge is switched on for this pull request, so opening it for review could merge it "
                 "straight away. Turn auto-merge off on GitHub first.")
_READY = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id isDraft headRefOid}}}"
_DRAFT = "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{id isDraft headRefOid}}}"


def _status_mutation(gh, cwd, query: str, field: str, pr_id: str, *, draft: bool) -> str | None:
  """One draft-state mutation; the head GitHub reports once it confirms the new state, else None."""
  response = gh(cwd, "api", "graphql", "-f", f"query={query}", "-f", f"id={pr_id}", check=False)
  result = graphql_mutation_result(response)
  data = result.get("data")
  mutation = data.get(field) if isinstance(data, dict) else None
  pull = mutation.get("pullRequest") if isinstance(mutation, dict) else None
  if (not isinstance(pull, dict) or pull.get("id") != pr_id or pull.get("isDraft") is not draft
      or not isinstance(pull.get("headRefOid"), str)):
    return None
  return pull["headRefOid"]


def mark_ready(gh, cwd, target, _body) -> str | None:
  return _status_mutation(gh, cwd, _READY, "markPullRequestReadyForReview", target["pr_id"], draft=False)


def convert_to_draft(gh, cwd, target, _body) -> str | None:
  return _status_mutation(gh, cwd, _DRAFT, "convertPullRequestToDraft", target["pr_id"], draft=True)


def _answer_object(stdout: str) -> dict:
  answer = json.loads(stdout)
  return answer if isinstance(answer, dict) else {}


def close_pull(gh, cwd, target, _body) -> str | None:
  pull = _answer_object(gh(cwd, "api", "--method", "PATCH", f"repos/{target['repo']}/pulls/{target['number']}",
                           "-f", "state=closed").stdout)
  return (pull.get("head") or {}).get("sha") if pull.get("state") == "closed" else None


# gh prints GitHub's own sentence, then the status: "gh: Reviews may only be requested from collaborators. (HTTP 422)".
_GITHUB_422 = re.compile(r"(?im)^gh:\s*(.+?)\s*\(HTTP\s+422\)\s*$")


def _github_422_reason(exc: ContributionSubmitError) -> str | None:
  """GitHub's own sentence for a 422 (a bot, or someone who is not a collaborator), unless it said only "Validation Failed"."""
  match = _GITHUB_422.search(str(exc))
  reason = match.group(1).strip() if match else ""
  return f"GitHub refused: {reason}" if reason and reason.lower() != "validation failed" else None


def request_reviewers(gh, cwd, target, body) -> str | None:
  try:
    response = gh(cwd, "api", "--method", "POST",
                  f"repos/{target['repo']}/pulls/{target['number']}/requested_reviewers", "--input", "-",
                  input_text=json.dumps({"reviewers": body.reviewers}))
  except ContributionSubmitError as exc:
    # A 422 is proof that nothing was asked, and GitHub's sentence says why: say it instead of the bare status.
    reason = _github_422_reason(exc)
    if reason:
      raise NotDispatched(reason) from exc
    raise
  pull = _answer_object(response.stdout)
  requested = {str(user.get("login") or "").lower() for user in pull.get("requested_reviewers") or []
               if isinstance(user, dict)}
  confirmed = {login.lower() for login in body.reviewers} <= requested
  return (pull.get("head") or {}).get("sha") if confirmed else None


def _head_after_posting(gh, cwd, target) -> str | None:
  """The head GitHub reports once the note is posted; None when it cannot be read.

  The note is already on the PR, so this read failing must never read as "refused,
  nothing happened": every failure here is an unconfirmed write, which is never repeated.
  """
  try:
    pull = _answer_object(gh(cwd, "api", f"repos/{target['repo']}/pulls/{target['number']}").stdout)
  except _READ_FAILURES:
    return None
  sha = (pull.get("head") or {}).get("sha")
  return sha if isinstance(sha, str) else None


def _refuse_a_repeated_note(gh, cwd, target, body) -> None:
  """Raise NotDispatched when this exact note is already among the PR's recent comments.

  A read, so it runs before the settled write (see StatusChange.prepare), never inside it.
  Best effort: a comment read that fails never blocks the post, except GitHub
  saying the pull request itself is not there, which nothing could post to.
  """
  since = (datetime.now(timezone.utc) - REPEAT_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
  try:
    found = reviews.read(gh, cwd,
                         f"repos/{target['repo']}/issues/{target['number']}/comments?per_page=100&since={since}")
  except _READ_FAILURES as exc:
    if _gh_http_status(str(exc)) == 404:
      raise NotDispatched(UNREADABLE) from exc
    return
  if isinstance(found, list) and any(isinstance(item, dict) and item.get("body") == body.note for item in found):
    raise NotDispatched(NOTE_ALREADY_THERE)


def post_note(gh, cwd, target, body) -> str | None:
  """Post the owner's note as a comment on the PR, once; the head GitHub reports once it confirms the comment."""
  posted = public.post_issue_comment(gh, cwd, target, body.note)
  if posted.state == "not_dispatched":
    raise NotDispatched(posted.reason)
  return _head_after_posting(gh, cwd, target) if posted.state == "confirmed" else None


def _ask_only_askable(people: list[str], facts: dict) -> None:
  """Anyone who reviewed or commented may be asked, except who already approved this version (and never a bot or the author)."""
  askable = [item["by"] for item in machine.decide(facts)["askable"]]
  allowed = {name.lower() for name in askable}
  if any(login.lower() not in allowed for login in people):
    raise HTTPException(409, "Ask only people who reviewed this pull request and have not approved this version: "
                             f"{', '.join(askable) or 'nobody'}.")


def _rereview_only(facts: dict, pull: dict, body) -> None:
  _ask_only_askable(body.reviewers, facts)


APPROVED_ALREADY = "Someone who already approved this version cannot be asked again."


def _refuse_current_approvers(gh, cwd, target, body) -> None:
  """Request review's own read: nobody who approved this exact version is asked again, whatever their access.

  The machine's approvals are the writers' (the merge rule), so a collaborator with read access who approved
  is missing from them. A read that cannot answer in full refuses, because an approver would be asked a second time.
  """
  try:
    approved = pr_facts.approvers_at(gh, cwd, target)
  except pr_facts.READ_FAILURES as exc:
    raise NotDispatched(str(exc.detail) if isinstance(exc, HTTPException) else UNREADABLE) from exc
  if any(login.lower() in approved for login in body.reviewers):
    raise NotDispatched(APPROVED_ALREADY)


def _request_check(facts: dict, pull: dict, body) -> None:
  """Anyone may be asked to review, except the author and anyone who already approved this version.

  Who approved is read twice: here from the machine, which also knows approvals carried over from an older commit,
  and in _refuse_current_approvers, which sees every approver of this commit. Whether a login is a person who can
  review is GitHub's to answer: it refuses a bot or a non-collaborator with a 422, in its own words.
  """
  author = str(facts.get("author") or "").lower()
  approved = {name.lower() for name in machine.decide(facts)["approved_by"]}
  if any(login.lower() == author for login in body.reviewers):
    raise HTTPException(409, "The author of a pull request cannot be asked to review it.")
  if any(login.lower() in approved for login in body.reviewers):
    raise HTTPException(409, APPROVED_ALREADY)


def _approval_note_check(facts: dict, pull: dict, body) -> None:
  _ask_only_askable(body.people, facts)
  asked = {login.lower() for login in body.people}
  if any(mention.lower() not in asked for mention in MENTION.findall(body.note)):
    raise HTTPException(409, "The note may only name the people asked.")


def _no_auto_merge(facts: dict, pull: dict, body) -> None:
  if "auto_merge" not in pull:
    raise HTTPException(404, UNREADABLE)
  if pull["auto_merge"] is not None:
    raise HTTPException(409, AUTO_MERGE_ON)


def _nothing_more(facts: dict, pull: dict, body) -> None:
  return None


def _nothing_to_read(gh, cwd, target, body) -> None:
  return None


def _not_yet(pull: dict) -> bool:
  return False


def _is_ready(pull: dict) -> bool:
  return pull.get("draft") is False


def _is_draft(pull: dict) -> bool:
  return pull.get("draft") is True


@dataclass(frozen=True)
class StatusChange:
  machine_action: str  # the PR machine action this tap is
  state: str  # what the pull request is once GitHub confirms it
  write: Callable  # (gh, cwd, target, body) -> the head GitHub reports, or None when unconfirmed
  unconfirmed: dict  # the 502 detail when the write's outcome cannot be read
  check: Callable = _nothing_more  # a rule beyond the machine, on the facts and the pull just read
  # A GitHub read the write needs first, as (gh, cwd, target, body); raises NotDispatched to refuse. It runs
  # unsettled, before the authority check that precedes the write, so a cancellation or a revoked permission
  # during the read still stops the write.
  prepare: Callable = _nothing_to_read
  already: Callable = _not_yet  # the pull just read is already what this tap makes it: confirmed with no write


def _unconfirmed(code: str, maybe: str) -> dict:
  return {"code": code, "message": f"GitHub may have {maybe}. Check the pull request before trying again."}


CHANGES = {
  "ready": StatusChange("open_for_review", "ready", mark_ready,
                        _unconfirmed("ready_unconfirmed", "opened it for review"), check=_no_auto_merge, already=_is_ready),
  "draft": StatusChange("back_to_draft", "draft", convert_to_draft,
                        _unconfirmed("draft_unconfirmed", "turned it back into a draft"), already=_is_draft),
  "close": StatusChange("close", "closed", close_pull, _unconfirmed("close_unconfirmed", "closed it")),
  "request-review": StatusChange("request_review", "requested", request_reviewers,
                                 _unconfirmed("reviewers_unconfirmed", "asked for the reviews"), _request_check,
                                 _refuse_current_approvers),
  "request-reviewers": StatusChange("ask_rereview", "requested", request_reviewers,
                                    _unconfirmed("reviewers_unconfirmed", "asked for the reviews"), _rereview_only),
  "ask-approval": StatusChange("ask_approval", "asked", post_note,
                               _unconfirmed("approval_unconfirmed", "posted the note"), _approval_note_check,
                               _refuse_a_repeated_note),
}


def _read_pull_facts(gh, cwd, body) -> tuple[dict, dict, dict]:
  """Read GitHub now: the connected account, the exact pull request the owner saw, and its machine facts.

  The agent job is not among them: it is a local fact, asked for later, right at the check."""
  try:
    actor = _read_object(gh, cwd, "user")
    repo = _read_object(gh, cwd, f"repos/{body.repo}")
    pull = _read_object(gh, cwd, f"repos/{body.repo}/pulls/{body.number}")
    pr_id = pull.get("node_id")
    if not isinstance(pr_id, str) or not actor.get("id"):
      raise ValueError("GitHub named no pull request or account id")
  except _READ_FAILURES as exc:
    raise HTTPException(404, UNREADABLE) from exc
  if (pull.get("head") or {}).get("sha") != body.head_sha:
    raise HTTPException(409, CHANGED)
  # GitHub's own spelling: a renamed repository's old name cannot take a write.
  target = {"repo": repo.get("full_name") or body.repo, "number": body.number,
            "head_sha": body.head_sha, "pr_id": pr_id}
  facts = pr_facts.read_facts(gh, cwd, target, repo, pull, actor_id=actor["id"],
                              actor_login=str(actor.get("login") or ""), job=None)
  return target, pull, facts


async def _prepare(change: StatusChange, gh, cwd, target, body) -> None:
  """The write's own GitHub read, cancellable and unsettled: a refusal here changed nothing."""
  try:
    await asyncio.to_thread(change.prepare, gh, cwd, target, body)
  except NotDispatched as exc:
    raise HTTPException(409, str(exc)) from exc


async def _dispatch(change: StatusChange, gh, cwd, target, body, authorize) -> DispatchResult:
  """The one write, with the caller's authority checked right before it."""
  try:
    authorize()
  except HTTPException as exc:
    return DispatchResult.not_dispatched(str(exc.detail))
  try:
    observed = await settled.run_settled(change.write, gh, cwd, target, body)
  except Exception as exc:
    return classify_dispatch_error(exc)
  if not isinstance(observed, str):
    return DispatchResult.unknown("GitHub returned no usable confirmation. Check without repeating it.")
  return DispatchResult.confirmed({"observed_head_sha": observed})


def _answer(result: DispatchResult, change: StatusChange, body) -> dict:
  """The owner's answer for a settled write; only a confirmed one claims success."""
  if result.state == "not_dispatched":
    raise HTTPException(409, result.reason)
  if result.state != "confirmed":
    raise HTTPException(502, change.unconfirmed)
  return {"state": change.state, "observed_head_sha": result.receipt["observed_head_sha"],
          "repo": body.repo, "number": body.number, "head_sha": body.head_sha}


async def _change(gh, cwd, change: StatusChange, body, authorize, pr_job) -> dict:
  authorize()
  async with _github_connection_transaction():
    target, pull, facts = await asyncio.to_thread(_read_pull_facts, gh, cwd, body)  # unsettled: read-only (GitHub account, pull request, reviews and checks)
    # The write's own GitHub read waits too, so it comes before the job is asked for.
    await _prepare(change, gh, cwd, target, body)
    # The agent job is local, so GitHub's reads cannot show it change: ask for it now, after every wait,
    # and keep review admission out until the write is sent.
    async with pr_job() as job_now:
      facts = {**facts, "job": job_now()}
      pr_facts.require_allowed(facts, change.machine_action)
      if change.already(pull):
        # Nothing to write, so nothing can be unconfirmed: the head the owner saw is the one GitHub holds.
        result = DispatchResult.confirmed({"observed_head_sha": body.head_sha})
      else:
        change.check(facts, pull, body)
        result = await _dispatch(change, gh, cwd, target, body, authorize)
  # A cancellation before the write is a refusal here; the caller's cancellation still wins.
  if result.state == "not_dispatched" and asyncio.current_task().cancelling():
    raise asyncio.CancelledError()
  return _answer(result, change, body)


async def change_pull_status(gh, cwd, name: str, body, authorize, pr_job) -> dict:
  """Apply one owner status change (a key of CHANGES) to the exact pull request named, once.

  `body` carries repo, number and head_sha (and reviewers for
  request-review and request-reviewers, people and note for ask-approval). `authorize` raises an HTTPException once the caller's
  authority no longer stands; it runs before any GitHub call and again right
  before the write. `pr_job` opens the PR's agent job as it is at the final
  check (a review run's or a Respond chat's), holding review admission until
  the write is sent.
  Raises HTTPException: 404 when GitHub cannot be read, 409 for a refusal
  that changed nothing (the PR machine's own words when it refuses) and 502
  with CHANGES[name].unconfirmed when the write's outcome is unknown. Logs
  one line per attempt.
  """
  outcome = "ended without an answer; GitHub may or may not have applied it"
  try:
    answer = await _change(gh, cwd, CHANGES[name], body, authorize, pr_job)
    moved = "" if answer["observed_head_sha"] == body.head_sha else f" at head {answer['observed_head_sha']}"
    outcome = answer["state"] + moved
    return answer
  except HTTPException as exc:
    outcome = _refusal_summary(exc)
    raise
  finally:
    _log.info("Owner %s of %s#%s at %s: %s", name, body.repo, body.number, body.head_sha, outcome)
