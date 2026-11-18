"""The owner's own Open for review, Back to draft, Close, Request review, Ask for re-review and Ask for approval:
owner-only, asked of the PR machine on GitHub's live answer, and never repeated when unconfirmed."""
import asyncio
import json
import threading

import pytest
from fastapi import HTTPException

from app import contribution_pull_status as subject
from app.contribution_errors import ContributionSubmitError
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.pr_job_helpers import holding
from tests.test_contribution_direct_merge import FakeGitHub, NAME, NUMBER, _flags
from tests.test_contribution_review_runs import APPROVAL, PULL, SHA

ME = {"id": 42, "login": "alex", "type": "User"}
OLDER = "c" * 40
MOVED = "d" * 40
PULL_ENDPOINT = f"repos/{NAME}/pulls/{NUMBER}"
COMMENTS_ENDPOINT = f"repos/{NAME}/issues/{NUMBER}/comments"


class StatusGitHub(FakeGitHub):
  """The merge fake, plus the connected account and the five status writes."""

  def __init__(self, *, mine=True, draft=False):
    super().__init__()
    self.pull = {**PULL, "number": NUMBER, "draft": draft, "auto_merge": None,
                 "user": ME if mine else {"id": 7, "login": "sam"}}
    self.answers = {}  # write name -> what GitHub answers, or an exception
    self.writes_seen = []
    self.after_write = lambda name: None
    self.recent_comments = []  # what GitHub answers to the read of the PR's recent comments, or an exception
    self.comment_reads = []
    self.during_comment_read = lambda: None  # runs while GitHub has not yet answered the comments read
    self.also_reviewed = []  # newest reviews of people without write access, which the writers-only opinions never carry
    self.review_pages = None  # None: one page of every newest review; else pages of nodes, each carrying the next cursor
    self.approver_reads = []  # the `after` cursor of each read of everyone's newest reviews
    self.approvers_answer = None  # what GitHub answers to that read instead of a page, or an exception

  def __call__(self, cwd, *args, check=True, input_text=None, **_ignored):
    self.calls.append(args)
    if args[:3] == ("api", "--method", "PATCH") and args[3] == PULL_ENDPOINT:
      return self._write("close", {**self.pull, "state": "closed"}, _flags(args[4:]))
    if args[:3] == ("api", "--method", "POST") and args[3] == f"{PULL_ENDPOINT}/requested_reviewers":
      asked = json.loads(input_text)["reviewers"]
      return self._write("reviewers", {**self.pull, "requested_reviewers": [{"login": login} for login in asked]}, asked)
    if len(args) == 2 and args[0] == "api" and args[1].startswith(f"{COMMENTS_ENDPOINT}?"):
      self.comment_reads.append(args[1])
      self.during_comment_read()
      return self._answer(self.recent_comments)
    if args[:3] == ("api", "--method", "POST") and args[3] == COMMENTS_ENDPOINT:
      note = json.loads(input_text)["body"]
      return self._write("comment", {"id": 1001, "body": note}, note)
    if args[:2] == ("api", "graphql") and "pageInfo" in _flags(args[2:])["query"]:
      return self._newest_reviews(_flags(args[2:]).get("after"))
    if args[:2] == ("api", "graphql") and "mutation" in _flags(args[2:])["query"]:
      field = "convertPullRequestToDraft" if "convertPullRequestToDraft" in _flags(args[2:])["query"] else "markPullRequestReadyForReview"
      node = {"id": PULL["node_id"], "isDraft": field == "convertPullRequestToDraft", "headRefOid": SHA}
      return self._write(field, {"data": {field: {"pullRequest": node}}}, _flags(args[2:])["id"])
    if args[1] == "user":
      return self._answer(ME)
    return super().__call__(cwd, *args, check=check)

  def _newest_reviews(self, after):
    """Everyone’s newest review, a hundred to a page, as `latestReviews` answers it (the writers’ opinions are among them)."""
    self.approver_reads.append(after)
    if self.approvers_answer is not None:
      return self._answer(self.approvers_answer)
    pages = self.review_pages or [[*self.reviews, *self.comments, *self.also_reviewed]]
    index = 0 if after is None else int(after.removeprefix("cursor-"))
    more = index + 1 < len(pages)
    return self._answer({"data": {"node": {"latestReviews": {
      "pageInfo": {"hasNextPage": more, "endCursor": f"cursor-{index + 1}" if more else None}, "nodes": pages[index]}}}})

  def _write(self, name, confirmed, sent):
    self.writes_seen.append((name, sent))
    self.after_write(name)
    return self._answer(self.answers.get(name, confirmed))


def body(**changes):
  return routes.PullStatusChange(**{"repo": NAME, "number": NUMBER, "head_sha": SHA, **changes})


def asking(reviewers, **changes):
  return routes.ReviewerRequest(**{"repo": NAME, "number": NUMBER, "head_sha": SHA, "reviewers": reviewers, **changes})


def approving(people, note="Could you approve this now?", **changes):
  return routes.ApprovalRequest(**{"repo": NAME, "number": NUMBER, "head_sha": SHA, "people": people,
                                   "note": note, **changes})


def looked_again(login="sam"):
  """Their change request is of an older version, and their newest review is a comment on this one."""
  github = StatusGitHub()
  person = {"login": login, "__typename": "User"}
  github.reviews = [{"state": "CHANGES_REQUESTED", "author": person, "commit": {"oid": OLDER}}]
  github.comments = [{"state": "COMMENTED", "author": person, "commit": {"oid": SHA}}]
  return github


def change(github, name, request=None, job=None):
  return asyncio.run(subject.change_pull_status(github, None, name, request or body(), lambda: None, pr_job=holding(job)))


def refused(github, name, request=None, job=None):
  with pytest.raises(HTTPException) as refusal:
    change(github, name, request, job)
  return refusal.value.status_code, refusal.value.detail


def test_the_owners_draft_opens_for_review_at_the_head_they_saw():
  github = StatusGitHub(draft=True)
  assert change(github, "ready") == {"state": "ready", "observed_head_sha": SHA,
                                      "repo": NAME, "number": NUMBER, "head_sha": SHA}
  assert [name for name, _ in github.writes_seen] == ["markPullRequestReadyForReview"]


def test_open_for_review_is_refused_to_a_reviewer_in_the_machines_words():
  github = StatusGitHub(mine=False, draft=True)
  assert refused(github, "ready") == (409, "You can’t do that as the reviewer of this PR.")
  assert github.writes_seen == []


@pytest.mark.parametrize("change_name, draft", [("ready", False), ("draft", True)])
def test_a_status_change_to_the_state_the_pr_is_already_in_is_confirmed_without_a_write(change_name, draft):
  github = StatusGitHub(draft=draft)
  assert change(github, change_name) == {"state": change_name, "observed_head_sha": SHA,
                                          "repo": NAME, "number": NUMBER, "head_sha": SHA}
  assert github.writes_seen == []


def test_an_already_open_pr_is_not_held_back_by_armed_auto_merge_because_nothing_is_written():
  github = StatusGitHub(draft=False)
  github.pull["auto_merge"] = {"merge_method": "squash"}
  assert change(github, "ready")["state"] == "ready"
  assert github.writes_seen == []


def test_no_status_change_while_an_agent_works_on_the_pr_but_a_stalled_one_holds_nothing():
  github = StatusGitHub(draft=True)
  assert refused(github, "ready", job={"state": "running"}) == (409, "An agent is already working on this PR.")
  assert github.writes_seen == []
  assert change(github, "ready", job={"state": "stalled"})["state"] == "ready"


def test_open_for_review_waits_while_auto_merge_is_armed():
  github = StatusGitHub(draft=True)
  github.pull["auto_merge"] = {"merge_method": "squash"}
  assert refused(github, "ready") == (409, subject.AUTO_MERGE_ON)
  assert github.writes_seen == []


def test_back_to_draft_turns_the_owners_open_pr_into_a_draft():
  github = StatusGitHub()
  assert change(github, "draft")["state"] == "draft"
  assert [name for name, _ in github.writes_seen] == ["convertPullRequestToDraft"]


def test_close_is_tried_for_the_author_or_a_writer_and_a_reader_gets_githubs_refusal():
  assert change(StatusGitHub(), "close")["state"] == "closed"
  theirs = StatusGitHub(mine=False)
  assert change(theirs, "close")["state"] == "closed"
  # Rights are GitHub's to refuse: the write is tried and its refusal is the answer, in a 409 that changed nothing.
  reader = StatusGitHub(mine=False)
  reader.repo = {**reader.repo, "permissions": {"pull": True}}
  reader.answers["close"] = ContributionSubmitError("gh: Forbidden (HTTP 403)")
  assert refused(reader, "close") == (409, "GitHub refused the request (HTTP 403); no effect was applied.")


def test_ask_for_rereview_is_for_anyone_who_reviewed_and_has_not_approved_this_version():
  github = StatusGitHub()
  github.reviews = [{**APPROVAL, "commit": {"oid": OLDER}}]
  assert change(github, "request-reviewers", asking(["Sam"]))["state"] == "requested"
  assert github.writes_seen == [("reviewers", ["Sam"])]
  status, reason = refused(github, "request-reviewers", asking(["bob"]))
  assert status == 409 and "sam" in reason and "have not approved this version" in reason
  again = looked_again()
  assert change(again, "request-reviewers", asking(["sam"]))["state"] == "requested", "even someone who looked at this version"
  current = StatusGitHub()
  assert refused(current, "request-reviewers", asking(["sam"])) == (409, "Everyone who reviewed it has already approved this version.")


def test_request_review_asks_anyone_even_a_person_who_never_reviewed():
  github = StatusGitHub()
  github.reviews = []  # nobody has reviewed this pull request yet: Ask for re-review would have no one to name
  assert change(github, "request-review", asking(["Sam", "bob"]))["state"] == "requested"
  assert github.writes_seen == [("reviewers", ["Sam", "bob"])]
  draft = StatusGitHub(draft=True)
  draft.reviews = []
  assert change(draft, "request-review", asking(["sam"]))["state"] == "requested", "a draft can ask early"


def test_request_review_refuses_the_author_and_whoever_already_approved_this_version():
  github = StatusGitHub()
  assert github.reviews == [APPROVAL], "sam approved this version"
  assert refused(github, "request-review", asking(["Alex"])) == (409, "The author of a pull request cannot be asked to review it.")
  assert refused(github, "request-review", asking(["bob", "sam"])) == (
    409, "Someone who already approved this version cannot be asked again.")
  assert change(github, "request-review", asking(["bob"]))["state"] == "requested", "the others can still be asked"
  assert github.writes_seen == [("reviewers", ["bob"])]


def approval(login, commit=SHA):
  return {"state": "APPROVED", "author": {"login": login, "__typename": "User"}, "commit": {"oid": commit}}


def test_request_review_does_not_ask_a_collaborator_without_write_access_who_approved_this_version():
  github = StatusGitHub()
  github.also_reviewed = [approval("Carol")]  # not a writer: the writers-only opinions never carry her approval
  assert "carol" not in {item["author"]["login"].lower() for item in github.reviews}
  assert refused(github, "request-review", asking(["carol"])) == (409, "Someone who already approved this version cannot be asked again.")
  assert refused(github, "request-review", asking(["bob", "CAROL"]))[0] == 409
  assert github.writes_seen == [], "nobody was asked"
  assert change(github, "request-review", asking(["bob"]))["state"] == "requested"


def test_request_review_asks_someone_whose_approval_was_of_an_older_version():
  github = StatusGitHub()
  github.also_reviewed = [approval("carol", OLDER)]
  assert change(github, "request-review", asking(["carol"]))["state"] == "requested"


def test_request_review_finds_an_approver_past_the_first_hundred_reviews():
  github = StatusGitHub()
  others = [{"state": "COMMENTED", "author": {"login": f"person{index}", "__typename": "User"}, "commit": {"oid": SHA}} for index in range(100)]
  github.review_pages = [others, [approval("carol")]]
  assert refused(github, "request-review", asking(["carol"])) == (409, "Someone who already approved this version cannot be asked again.")
  assert github.approver_reads == [None, "cursor-1"], "the second page is read with the cursor the first one gave"
  assert github.writes_seen == []


def test_request_review_asks_nobody_when_who_approved_cannot_be_read_in_full():
  unreadable = "GitHub could not show who approved this pull request."
  for broken, reason in ((ContributionSubmitError("gh: Bad gateway (HTTP 502)"), subject.UNREADABLE), ({"errors": [{"message": "boom"}]}, unreadable),
                         ({"data": {"node": None}}, unreadable), (["not", "an", "object"], unreadable)):
    github = StatusGitHub()
    github.approvers_answer = broken
    assert refused(github, "request-review", asking(["bob"])) == (409, reason), broken
    assert github.writes_seen == [], f"asked anyway after {broken!r}"
  github = StatusGitHub()
  github.review_pages = [[approval("someone")] for _ in range(subject.pr_facts.APPROVER_PAGES + 1)]
  assert refused(github, "request-review", asking(["bob"])) == (409, "GitHub showed too many reviews to tell who approved this pull request.")
  assert len(github.approver_reads) == subject.pr_facts.APPROVER_PAGES and github.writes_seen == []


def test_only_request_review_reads_everyone_who_approved():
  github = looked_again()
  assert change(github, "request-reviewers", asking(["sam"]))["state"] == "requested"
  assert github.approver_reads == [], "Ask for re-review keeps the machine's own rule"


def test_request_review_is_refused_on_a_pull_request_that_is_no_longer_open():
  github = StatusGitHub()
  github.pull = {**github.pull, "state": "closed"}
  status, reason = refused(github, "request-review", asking(["bob"]))
  assert status == 409 and reason == "This PR is no longer open." and github.writes_seen == []


def without_write_access(state, commit=OLDER, login="pat"):
  """Someone with no write access reviewed: GitHub's writers-only opinions leave them out, its newest-review list does not."""
  github = StatusGitHub()
  github.reviews = []
  github.comments = [{"state": state, "author": {"login": login, "__typename": "User"}, "commit": {"oid": commit}}]
  return github


@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED"])
def test_both_ask_actions_reach_a_reviewer_without_write_access_whose_opinion_is_of_an_older_version(state):
  rereview = without_write_access(state)
  assert change(rereview, "request-reviewers", asking(["Pat"]))["state"] == "requested"
  assert rereview.writes_seen == [("reviewers", ["Pat"])]
  approval = without_write_access(state)
  assert change(approval, "ask-approval", approving(["pat"], "@pat could you approve this version?"))["state"] == "asked"
  assert approval.writes_seen == [("comment", "@pat could you approve this version?")]


def test_a_reviewer_without_write_access_who_approved_this_version_is_not_asked_and_the_refusal_says_why():
  github = without_write_access("APPROVED", SHA)
  words = (409, "Everyone who reviewed it has already approved this version.")
  assert refused(github, "request-reviewers", asking(["pat"])) == words
  assert refused(github, "ask-approval", approving(["pat"])) == words
  assert github.writes_seen == []


def test_ask_for_approval_posts_one_note_to_people_who_looked_again_without_approving():
  github = looked_again()
  note = "@Sam thank you for coming back. Could you approve this now?"
  assert change(github, "ask-approval", approving(["Sam"], f"  {note}\n")) == {
    "state": "asked", "observed_head_sha": SHA, "repo": NAME, "number": NUMBER, "head_sha": SHA}
  assert github.writes_seen == [("comment", note)]


def test_ask_for_approval_posts_a_note_that_names_no_one():
  github = looked_again()
  assert change(github, "ask-approval", approving(["sam"], "Ready for another look."))["state"] == "asked"
  assert github.writes_seen == [("comment", "Ready for another look.")]


def test_ask_for_approval_is_refused_for_a_person_who_never_reviewed():
  github = looked_again()
  assert refused(github, "ask-approval", approving(["sam", "bob"])) == (
    409, "Ask only people who reviewed this pull request and have not approved this version: sam.")
  assert github.writes_seen == []


def test_ask_for_approval_reaches_someone_whose_change_request_is_of_an_older_version():
  github = looked_again()
  github.comments = []
  assert change(github, "ask-approval", approving(["sam"], "@sam could you approve?"))["state"] == "asked"
  assert github.writes_seen == [("comment", "@sam could you approve?")]


def test_ask_for_approval_posts_to_someone_who_only_commented_and_holds_no_change_request():
  github = looked_again()
  github.reviews = []
  assert change(github, "ask-approval", approving(["sam"], "@sam could you approve?"))["state"] == "asked"
  assert github.writes_seen == [("comment", "@sam could you approve?")]


@pytest.mark.parametrize("note", ["@bob please approve", "Thanks @sam and @bob", "cc @sam-smith", "mail a@example.org"])
def test_ask_for_approval_is_refused_when_the_note_names_anyone_but_the_people_asked(note):
  github = looked_again()
  assert refused(github, "ask-approval", approving(["sam"], note)) == (409, "The note may only name the people asked.")
  assert github.writes_seen == []


def test_ask_for_approval_is_refused_in_the_machines_words_when_nobody_reviewed():
  waiting = StatusGitHub()
  waiting.reviews = []
  assert refused(waiting, "ask-approval", approving(["sam"])) == (409, "Nobody has reviewed this PR yet.")
  theirs = looked_again()
  theirs.pull["user"] = {"id": 7, "login": "kim"}
  assert refused(theirs, "ask-approval", approving(["sam"])) == (409, "You can’t do that as the reviewer of this PR.")
  closed = looked_again()
  closed.pull["state"] = "closed"
  assert refused(closed, "ask-approval", approving(["sam"]))[0] == 409
  assert waiting.writes_seen == theirs.writes_seen == closed.writes_seen == []


@pytest.mark.parametrize("answer", ["not json", {"body": "Could you approve this now?"},
                                    {"id": "1001", "body": "Could you approve this now?"},
                                    {"id": True, "body": "Could you approve this now?"}, []],
                         ids=["not json", "no id", "text id", "boolean id", "not an object"])
def test_ask_for_approval_is_unconfirmed_and_never_repeated_when_githubs_answer_is_unusable(answer):
  github = looked_again()
  github.answers["comment"] = answer
  status, detail = refused(github, "ask-approval", approving(["sam"]))
  assert status == 502 and detail["code"] == "approval_unconfirmed" and "posted the note" in detail["message"]
  assert len(github.writes_seen) == 1


NOTE = "Could you approve this now?"


def test_the_same_note_is_never_posted_twice_but_another_one_is():
  github = looked_again()
  github.recent_comments = [{"id": 5, "body": "Thanks for the look"}, {"id": 6, "body": NOTE}]
  assert refused(github, "ask-approval", approving(["sam"], NOTE)) == (409, subject.NOTE_ALREADY_THERE)
  assert github.writes_seen == []
  github.recent_comments = [{"id": 5, "body": NOTE + " Please."}, {"id": 6, "body": "other"}]
  assert change(github, "ask-approval", approving(["sam"], NOTE))["state"] == "asked"
  assert github.writes_seen == [("comment", NOTE)]


def test_the_repeat_check_reads_the_last_seven_days_of_comments_once():
  from datetime import datetime, timedelta, timezone
  github = looked_again()
  change(github, "ask-approval", approving(["sam"], NOTE))
  assert len(github.comment_reads) == 1
  query = github.comment_reads[0]
  assert query.startswith(f"{COMMENTS_ENDPOINT}?per_page=100&since=") and query.endswith("Z")
  since = datetime.strptime(query.rsplit("since=", 1)[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
  assert timedelta(days=7) - timedelta(minutes=1) < datetime.now(timezone.utc) - since < timedelta(days=7, minutes=1)


@pytest.mark.parametrize("failure", [ContributionSubmitError("gh: Internal Server Error (HTTP 500)"),
                                     ContributionSubmitError("gh: Forbidden (HTTP 403)"), OSError("no network"),
                                     "not json", {"message": "not a list"}],
                         ids=["server error", "forbidden", "no network", "not json", "not a list"])
def test_a_repeat_check_that_cannot_read_comments_never_blocks_the_post_which_still_happens_once(failure):
  github = looked_again()
  github.recent_comments = failure
  assert change(github, "ask-approval", approving(["sam"], NOTE))["state"] == "asked"
  assert github.writes_seen == [("comment", NOTE)]


def test_a_caller_cancelled_during_the_repeat_check_never_posts_the_note():
  github = looked_again()
  reading, release = threading.Event(), threading.Event()
  github.during_comment_read = lambda: (reading.set(), release.wait(5))

  async def cancel_while_reading():
    task = asyncio.ensure_future(subject.change_pull_status(github, None, "ask-approval", approving(["sam"]),
                                                            lambda: None, pr_job=holding()))
    await asyncio.to_thread(reading.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
      await task

  asyncio.run(cancel_while_reading())
  assert github.writes_seen == []


def test_permission_revoked_during_the_repeat_check_never_posts_the_note():
  github = looked_again()
  revoked = []
  github.during_comment_read = lambda: revoked.append(True)

  def authorize():
    if revoked:
      raise HTTPException(403, "App access was turned off.")

  with pytest.raises(HTTPException) as refusal:
    asyncio.run(subject.change_pull_status(github, None, "ask-approval", approving(["sam"]), authorize, pr_job=holding()))
  assert (refusal.value.status_code, refusal.value.detail) == (409, "App access was turned off.")
  assert len(github.comment_reads) == 1 and github.writes_seen == []


def test_a_pull_request_githubs_comment_read_says_is_not_there_is_refused_and_nothing_is_posted():
  github = looked_again()
  github.recent_comments = ContributionSubmitError("gh: Not Found (HTTP 404)")
  assert refused(github, "ask-approval", approving(["sam"], NOTE)) == (409, subject.UNREADABLE)
  assert github.writes_seen == []


def test_a_note_that_posted_is_never_reported_as_refused_when_the_head_cannot_be_read_afterwards():
  github = looked_again()
  github.after_write = lambda name: github.unreadable.add("pull")
  status, detail = refused(github, "ask-approval", approving(["sam"]))
  assert status == 502 and detail["code"] == "approval_unconfirmed"
  assert len(github.writes_seen) == 1


@pytest.mark.parametrize("asked", ["Alice", "alice", "ALICE"])
def test_ask_for_rereview_matches_a_reviewer_whatever_the_case_of_their_login(asked):
  github = StatusGitHub()
  github.reviews = [{**APPROVAL, "author": {"login": "Alice", "__typename": "User"}, "commit": {"oid": OLDER}}]
  assert change(github, "request-reviewers", asking([asked]))["state"] == "requested"
  assert github.writes_seen == [("reviewers", [asked])]
  status, reason = refused(github, "request-reviewers", asking(["bob"]))
  assert status == 409 and "Alice" in reason  # the refusal still names her as GitHub spells her


def test_a_head_that_moved_since_the_owner_looked_is_refused_before_any_write():
  github = StatusGitHub(draft=True)
  github.pull["head"] = {"sha": MOVED}
  assert refused(github, "ready")[0] == 409
  assert github.writes_seen == []


def test_an_unreadable_review_list_refuses_a_status_change_in_the_machines_words():
  github = StatusGitHub(draft=True)
  github.unreadable.add("reviews")
  assert refused(github, "ready") == (409, "This PR could not be read. Try again.")
  assert github.writes_seen == []


@pytest.mark.parametrize("name, write, code", [
  ("ready", "markPullRequestReadyForReview", "ready_unconfirmed"),
  ("draft", "convertPullRequestToDraft", "draft_unconfirmed"),
  ("close", "close", "close_unconfirmed"),
  ("request-reviewers", "reviewers", "reviewers_unconfirmed"),
  ("request-review", "reviewers", "reviewers_unconfirmed"),
])
def test_an_unconfirmed_write_is_a_502_with_its_code_and_is_never_retried(name, write, code):
  github = StatusGitHub(draft=name == "ready")
  github.reviews = [{**APPROVAL, "commit": {"oid": OLDER}}]
  github.answers[write] = "not json"
  request = asking(["sam"]) if name.startswith("request-review") else None
  status, detail = refused(github, name, request)
  assert status == 502 and detail["code"] == code and "Check the pull request" in detail["message"]
  assert len(github.writes_seen) == 1


@pytest.mark.parametrize("name, person", [("request-review", "bob"), ("request-reviewers", "sam")])
def test_githubs_own_reason_for_a_422_reaches_the_owner_and_nothing_is_repeated(name, person):
  reason = "Reviews may only be requested from collaborators. One or more of the users you specified is not a collaborator of the repository."
  start = looked_again if name == "request-reviewers" else StatusGitHub
  github = start()
  github.answers["reviewers"] = ContributionSubmitError(f"gh: {reason} (HTTP 422)")
  assert refused(github, name, asking([person])) == (409, f"GitHub refused: {reason}")
  assert len(github.writes_seen) == 1, "one attempt, never repeated"
  # GitHub saying only "Validation Failed" has no reason to pass on: the generic words stay.
  vague = start()
  vague.answers["reviewers"] = ContributionSubmitError("gh: Validation Failed (HTTP 422)")
  assert refused(vague, name, asking([person])) == (409, "GitHub refused the request (HTTP 422); no effect was applied.")


def test_a_refusal_github_proves_is_a_409_that_changed_nothing():
  github = StatusGitHub()
  github.answers["close"] = ContributionSubmitError("gh: Validation Failed (HTTP 422)")
  assert refused(github, "close") == (409, "GitHub refused the request (HTTP 422); no effect was applied.")


def test_only_the_owner_can_change_a_pull_requests_status(monkeypatch):
  agent = Principal(owner=None, app_id=None, chat_id="review-chat", run_id="run")
  with pytest.raises(HTTPException) as refusal:
    asyncio.run(routes.close_pull_now(1, body(), None, agent))
  assert refusal.value.status_code == 403


def test_status_routes_are_not_taken_by_a_contribution_record_named_pulls():
  from starlette.routing import Match
  from app.main import app
  endpoints = {"ready": routes.mark_pull_ready_now, "draft": routes.back_to_draft_now,
               "close": routes.close_pull_now, "request-review": routes.request_review_now,
               "request-reviewers": routes.request_rereview_now,
               "ask-approval": routes.ask_approval_now}
  for name, endpoint in endpoints.items():
    scope = {"type": "http", "method": "POST", "path": f"/api/github/contributions/1/pulls/{name}", "root_path": ""}
    first = next(route for route in app.router.routes if route.matches(scope)[0] == Match.FULL)
    # FastAPI may keep an included router whole; then it must be this one, not github's records.
    assert getattr(first, "endpoint", None) is endpoint or getattr(first, "original_router", None) is routes.router


def test_reviewers_are_github_logins():
  from pydantic import ValidationError
  with pytest.raises(ValidationError):
    asking(["not a login"])
  assert asking(["sam", "sam"]).reviewers == ["sam"]


def test_people_asked_for_approval_are_github_logins_and_the_note_is_one_to_a_thousand_characters_once_trimmed():
  from pydantic import ValidationError
  with pytest.raises(ValidationError):
    approving(["not a login"])
  with pytest.raises(ValidationError):
    approving([])
  with pytest.raises(ValidationError):
    approving([f"user{index}" for index in range(16)])
  assert approving(["sam", "sam"]).people == ["sam"]
  assert approving(["sam"], "  \n note \n ").note == "note"
  assert len(approving(["sam"], " " + "x" * 1000 + " ").note) == 1000
  for note in ("", "   \n\t", "x" * 1001):
    with pytest.raises(ValidationError):
      approving(["sam"], note)
  with pytest.raises(ValidationError):
    approving(["sam"], reviewers=["sam"])  # a misspelled field fails loudly, never ignored
