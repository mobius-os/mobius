"""The owner's one-tap "merge this exact version now": owner-only, pinned to the
head they saw, bound by GitHub's own rules, and never repeated when unconfirmed."""
import asyncio
import json
import logging
import subprocess
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth as auth_mod, contribution_direct_merge as subject, contribution_review_runs as reviews, models
from app.chat_writer import create_chat
from app.contribution_errors import ContributionSubmitError
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import APPROVAL, BASE, CHECKS, ITEM, PULL, REPO, SHA, TARGET

NAME, NUMBER = ITEM["repo"], ITEM["number"]
MERGED_SHA = "c" * 40
MOVED = "d" * 40
RESPONSE_BODY = "RESPONSE-BODY-THAT-MUST-NOT-BE-LOGGED"
QUEUE_ENTRY = {"id": "QUEUE_1", "enqueuedAt": "2026-11-04T09:00:00Z"}
MERGE_ENDPOINT = f"repos/{NAME}/pulls/{NUMBER}/merge"
ALLOWED_ENDPOINTS = {f"repos/{NAME}", f"repos/{NAME}/pulls/{NUMBER}", f"repos/{NAME}/git/ref/heads/main",
                     MERGE_ENDPOINT, "graphql"}

GATE = "GitHub has not cleared this pull request for a normal merge; check its rules or merge queue."
REVIEW = "GitHub still requires a review or has requested changes."
CHECKS_FAILED = "The current checks have not all passed."
NO_PERMISSION = "Your repository merge permission is no longer available."


def _flags(args):
  """The `-f key=value` pairs of a gh call."""
  return dict(pair.split("=", 1) for flag, pair in zip(args[::2], args[1::2]) if flag == "-f")


def _rollup(state):
  return {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]}}


class FakeGitHub:
  """A stand-in for `gh`: answers the reads a merge needs, records every write,
  and fails the test on any call outside the one pull request."""

  def __init__(self):
    self.repo = {**REPO, "allow_merge_commit": True}
    self.pull = {**PULL}
    self.checks = {key: value for key, value in CHECKS.items() if key != "latestOpinionatedReviews"}
    self.reviews = [APPROVAL]  # the writers' standing opinions the check read carries
    self.base_tips = [BASE]  # each base-branch read takes the next; the last repeats
    self.merge_answer = {"merged": True, "sha": MERGED_SHA, "message": RESPONSE_BODY}
    self.enqueue_answer = {"data": {"enqueuePullRequest": {"mergeQueueEntry": QUEUE_ENTRY}}}
    self.unreadable = set()  # any of "repo", "pull", "base", "checks", "reviews"
    self.on_base_read = lambda count: None
    self.base_reads = 0
    self.calls, self.merges, self.enqueues = [], [], []

  @property
  def writes(self):
    return len(self.merges) + len(self.enqueues)

  def __call__(self, cwd, *args, check=True, **_ignored):
    self.calls.append(args)
    if args[:3] == ("api", "--method", "PUT") and args[3] == MERGE_ENDPOINT:
      return self._merge(_flags(args[4:]))
    if args[:2] == ("api", "graphql"):
      return self._graphql(_flags(args[2:]))
    return self._read(args[1])

  def _read(self, endpoint):
    reads = {f"repos/{NAME}": ("repo", self.repo), f"repos/{NAME}/pulls/{NUMBER}": ("pull", self.pull),
             f"repos/{NAME}/git/ref/heads/main": ("base", None)}
    if endpoint not in reads:
      pytest.fail(f"unexpected GitHub read: {endpoint}")
    kind, payload = reads[endpoint]
    self._must_be_readable(kind)
    if kind == "base":
      self.base_reads += 1
      self.on_base_read(self.base_reads)
      payload = {"object": {"sha": self.base_tips[min(self.base_reads, len(self.base_tips)) - 1]}}
    return self._answer(payload)

  def _graphql(self, flags):
    if flags["id"] != TARGET["pr_id"]:
      pytest.fail(f"unexpected pull request: {flags['id']}")
    if flags["query"].lstrip().startswith("mutation"):
      self.enqueues.append({"id": flags["id"], "head": flags["head"]})
      return self._answer(self.enqueue_answer)
    self._must_be_readable("checks")
    reviews = {} if "reviews" in self.unreadable else {"latestOpinionatedReviews": {"nodes": self.reviews}}
    return self._answer({"data": {"node": {**self.checks, **reviews}}})

  def _merge(self, flags):
    self.merges.append(flags)
    answer = self._answer(self.merge_answer)
    if isinstance(self.merge_answer, dict) and self.merge_answer.get("merged") is True:
      self.pull.update(merged=True, state="closed")
    return answer

  def _must_be_readable(self, kind):
    if kind in self.unreadable:
      raise ContributionSubmitError("gh: Not Found (HTTP 404)")

  @staticmethod
  def _answer(value):
    if isinstance(value, BaseException):
      raise value
    text = value if isinstance(value, str) else json.dumps(value)
    return SimpleNamespace(stdout=text, stderr="", returncode=0)


def body(**changes):
  return routes.DirectMerge(**{"repo": NAME, "number": NUMBER, "head_sha": SHA, **changes})


def ledger_for(db):
  """The owner's session: every press saves its attempt here, as the route does."""
  owner = db.query(models.Owner).first() or models.Owner(username="merge-owner", hashed_password="unused")
  db.add(owner)
  db.commit()
  LEDGER.update(db=db, owner_id=owner.id)


@pytest.fixture(autouse=True)
def ledger(request):
  # HTTP tests bring their own owner (the desk); every other test makes one here.
  if "desk" not in request.fixturenames:
    ledger_for(request.getfixturevalue("db"))


LEDGER = {}


def press(github, authorize=lambda: None, **changes):
  return asyncio.run(subject.merge_exact_pull(github, None, body(**changes), authorize, LEDGER["db"], LEDGER["owner_id"], job=None))


def refused(github, authorize=lambda: None, **changes):
  with pytest.raises(HTTPException) as refusal:
    press(github, authorize, **changes)
  return refusal.value


# Merging the exact version

def test_the_exact_head_is_merged_once_with_the_repositorys_preferred_method():
  github = FakeGitHub()
  assert press(github) == {"state": "merged", "merge_sha": MERGED_SHA, "method": "squash",
                           "repo": NAME, "number": NUMBER, "head_sha": SHA}
  assert github.merges == [{"sha": SHA, "merge_method": "squash"}]
  assert github.enqueues == []


@pytest.mark.parametrize("allowed, preferred", [
  ({"allow_squash_merge": True, "allow_merge_commit": True}, "squash"),
  ({"allow_squash_merge": False, "allow_merge_commit": True}, "merge"),
  ({"allow_squash_merge": False, "allow_merge_commit": False, "allow_rebase_merge": True}, "rebase"),
])
def test_without_a_chosen_method_the_repositorys_preferred_one_is_sent(allowed, preferred):
  github = FakeGitHub()
  github.repo.update(allowed)
  assert press(github)["method"] == preferred
  assert github.merges == [{"sha": SHA, "merge_method": preferred}]


def test_the_preferred_merge_method_is_squash_then_merge_then_rebase():
  everything = {"allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True}
  assert reviews.allowed_merge_methods(everything) == ["squash", "merge", "rebase"]
  assert reviews.merge_method({**everything, "allow_squash_merge": False}) == "merge"
  with pytest.raises(HTTPException) as none_allowed:
    reviews.merge_method({})
  assert (none_allowed.value.status_code, none_allowed.value.detail) == (
    409, "GitHub did not provide an allowed merge method.")


def test_an_allowed_method_the_owner_chose_is_the_one_sent():
  github = FakeGitHub()
  assert press(github, method="merge")["method"] == "merge"
  assert github.merges == [{"sha": SHA, "merge_method": "merge"}]


def test_a_merge_only_reads_and_writes_the_one_pull_request_it_names():
  github = FakeGitHub()
  press(github)
  endpoints = {call[3] if call[1] == "--method" else call[1] for call in github.calls}
  assert endpoints <= ALLOWED_ENDPOINTS
  assert [call[2] for call in github.calls if call[1] == "--method"] == ["PUT"]
  assert github.enqueues == []


def test_a_second_press_is_told_it_is_already_merged_and_writes_nothing():
  github = FakeGitHub()
  press(github)
  again = refused(github)
  assert (again.status_code, again.detail) == (409, "This pull request is already merged.")
  assert len(github.merges) == 1


# A merge queue

@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_a_merge_queue_repository_enqueues_the_exact_head_and_says_queued(state):
  github = FakeGitHub()
  github.checks.update(isMergeQueueEnabled=True, mergeStateStatus=state)
  assert press(github) == {"state": "queued", "merge_sha": None, "method": None,
                           "repo": NAME, "number": NUMBER, "head_sha": SHA}
  assert github.enqueues == [{"id": "PR_7", "head": SHA}]
  assert github.merges == []


def test_a_merge_queue_chooses_the_method_so_an_explicit_one_is_refused():
  github = FakeGitHub()
  github.checks.update(isMergeQueueEnabled=True)
  refusal = refused(github, method="squash")
  assert (refusal.status_code, refusal.detail) == (409, subject.QUEUE_CHOOSES_METHOD)
  assert github.writes == 0


def test_a_pull_request_already_in_the_queue_is_not_enqueued_again():
  github = FakeGitHub()
  github.checks.update(isMergeQueueEnabled=True, mergeQueueEntry=QUEUE_ENTRY)
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, "This pull request is already in the merge queue.")
  assert github.writes == 0


# Refusals: GitHub's live answer says no, so nothing is written

REFUSALS = {
  "an already merged pull request": (lambda g: g.pull.update(merged=True, state="closed"),
                                     "This pull request is already merged."),
  "a closed pull request": (lambda g: g.pull.update(state="closed"), "This pull request is closed."),
  "a head that moved since the owner looked": (lambda g: g.pull.update(head={"sha": MOVED}), subject.CHANGED),
  "a draft": (lambda g: g.pull.update(draft=True), "This pull request is still a draft."),
  "a head that moved between GitHub's two answers": (lambda g: g.checks.update(headRefOid=MOVED),
                                                      "The pull request changed during the merge check."),
  "nobody but the author approved": (lambda g: setattr(g, "reviews", []), "Nobody but the author has approved it yet."),
  "an approval only of an older version": (
    lambda g: setattr(g, "reviews", [{**APPROVAL, "commit": {"oid": MOVED}}]), "The approval is for an older version."),
  "only a bot approved": (lambda g: setattr(g, "reviews", [{**APPROVAL, "author": {"login": "ci", "__typename": "Bot"}}]),
                          "Nobody but the author has approved it yet."),
  "the author's own approval": (lambda g: (g.pull.update(user={"id": 7, "login": "sam"})),
                                "Nobody but the author has approved it yet."),
  "an older change request still standing": (
    lambda g: g.reviews.append({"state": "CHANGES_REQUESTED", "author": {"login": "bob", "__typename": "User"},
                                "commit": {"oid": MOVED}}), "bob asked for changes and hasn’t approved since."),
  "a change request whose commit GitHub no longer has": (
    lambda g: g.reviews.append({"state": "CHANGES_REQUESTED", "author": None, "commit": None}),
    "ghost asked for changes and hasn’t approved since."),
  "reviews that cannot be read": (lambda g: g.unreadable.add("reviews"), "This PR could not be read. Try again."),
  "failing checks": (lambda g: g.checks.update(_rollup("FAILURE")), CHECKS_FAILED),
  "pending checks": (lambda g: g.checks.update(_rollup("PENDING")), CHECKS_FAILED),
  "no check result": (lambda g: g.checks.update(commits={"nodes": []}), "GitHub did not provide the current check result."),
  "changes requested": (lambda g: g.checks.update(reviewDecision="CHANGES_REQUESTED"), REVIEW),
  "a review still required": (lambda g: g.checks.update(reviewDecision="REVIEW_REQUIRED"), REVIEW),
  "merge conflicts": (lambda g: g.checks.update(mergeable="CONFLICTING", mergeStateStatus="DIRTY"), GATE),
  "a branch that is behind": (lambda g: g.checks.update(mergeStateStatus="BEHIND"), GATE),
  "a pull request GitHub's rules still block": (lambda g: g.checks.update(mergeStateStatus="BLOCKED"), GATE),
  "an admin whose pull request is still blocked": (
    lambda g: (g.repo.update(permissions={"admin": True}), g.checks.update(mergeStateStatus="BLOCKED")), GATE),
  "no merge permission": (lambda g: g.repo.update(permissions={"pull": True}), NO_PERMISSION),
  "an archived repository": (lambda g: g.repo.update(archived=True), NO_PERMISSION),
  "an unconfirmed queue policy": (lambda g: g.checks.update(isMergeQueueEnabled=None),
                                  "GitHub did not confirm the merge queue policy."),
}


@pytest.mark.parametrize("what", REFUSALS)
def test_a_pull_request_that_cannot_be_merged_is_refused_in_plain_words_and_nothing_is_written(what):
  change, sentence = REFUSALS[what]
  github = FakeGitHub()
  change(github)
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, sentence)
  assert github.writes == 0


def test_a_merge_waits_while_an_agent_works_on_the_pull_request_but_not_for_a_stalled_one():
  github = FakeGitHub()
  with pytest.raises(HTTPException) as refusal:
    asyncio.run(subject.merge_exact_pull(github, None, body(), lambda: None, LEDGER["db"], LEDGER["owner_id"], job={"state": "running"}))
  assert (refusal.value.status_code, refusal.value.detail) == (409, "An agent is already working on this PR.")
  assert github.writes == 0
  answer = asyncio.run(subject.merge_exact_pull(github, None, body(), lambda: None, LEDGER["db"], LEDGER["owner_id"], job={"state": "stalled"}))
  assert answer["state"] == "merged"


def test_a_method_the_repository_does_not_allow_is_refused_with_the_ones_it_does():
  github = FakeGitHub()
  refusal = refused(github, method="rebase")
  assert (refusal.status_code, refusal.detail) == (
    409, "This repository does not allow rebase merges. It allows: squash, merge.")
  assert github.writes == 0


def test_a_merged_pull_request_is_reported_as_merged_even_when_its_head_moved_first():
  github = FakeGitHub()
  github.pull.update(merged=True, state="closed", head={"sha": MOVED})
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, "This pull request is already merged.")
  assert github.writes == 0


def test_a_target_branch_that_moves_before_the_write_is_refused_and_nothing_is_written():
  github = FakeGitHub()
  github.base_tips = [BASE, MOVED]
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (
    409, "The target branch changed. Review the new combined result first.")
  assert github.writes == 0


@pytest.mark.parametrize("missing", ["repo", "pull", "base", "checks"])
def test_a_pull_request_github_cannot_be_read_for_is_a_404_and_nothing_is_written(missing):
  github = FakeGitHub()
  github.unreadable.add(missing)
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (404, subject.UNREADABLE)
  assert github.writes == 0


def test_an_answer_that_is_not_a_pull_request_is_unreadable_not_a_crash():
  github = FakeGitHub()
  github.pull = ["not", "a", "pull", "request"]
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (404, subject.UNREADABLE)


# The write's outcome: refused by GitHub, or not known

def test_a_refusal_from_github_on_the_write_is_a_plain_409_that_changed_nothing():
  github = FakeGitHub()
  github.merge_answer = ContributionSubmitError("gh: Pull Request is not mergeable (HTTP 405)")
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, "GitHub refused the request (HTTP 405); no effect was applied.")
  assert len(github.merges) == 1


def test_a_merge_github_says_it_did_not_apply_is_a_409():
  github = FakeGitHub()
  github.merge_answer = {"merged": False, "message": "Head branch was modified."}
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, "GitHub refused the merge request without applying it.")


UNCONFIRMED_ANSWERS = {
  "an unreadable answer": "<html>gateway timeout</html>",
  "an answer of the wrong shape": json.dumps(["merged"]),
  "an answer without a merged flag": {"message": "ok"},
  "a timeout": subprocess.TimeoutExpired("gh", 90),
  "a server error": ContributionSubmitError("gh: Bad Gateway (HTTP 502)"),
  "a throttled request": ContributionSubmitError("gh: Too Many Requests (HTTP 429)"),
}


@pytest.mark.parametrize("what", UNCONFIRMED_ANSWERS)
def test_a_write_whose_outcome_is_unknown_is_unconfirmed_and_never_retried(what):
  github = FakeGitHub()
  github.merge_answer = UNCONFIRMED_ANSWERS[what]
  refusal = refused(github)
  assert refusal.status_code == 502
  assert refusal.detail == {"code": "merge_unconfirmed",
                            "message": "GitHub may have merged it. Check the pull request before trying again."}
  assert len(github.merges) == 1


def test_an_unreadable_queue_answer_is_unconfirmed_and_never_retried():
  github = FakeGitHub()
  github.checks.update(isMergeQueueEnabled=True)
  github.enqueue_answer = "<html>gateway timeout</html>"
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (502, subject.UNCONFIRMED)
  assert len(github.enqueues) == 1


def attempt_row():
  return LEDGER["db"].query(models.DirectMergeAttempt).one()


def test_an_unknown_merge_is_not_sent_again_by_the_next_identical_request():
  github = FakeGitHub()
  github.merge_answer = subprocess.TimeoutExpired("gh", 90)
  assert refused(github).status_code == 502
  assert attempt_row().state == "unknown"
  again = refused(github)
  assert (again.status_code, again.detail) == (409, subject.UNFINISHED)
  assert len(github.merges) == 1


def test_an_unknown_enqueue_is_not_sent_again_either():
  github = FakeGitHub()
  github.checks.update(isMergeQueueEnabled=True)
  github.enqueue_answer = "<html>gateway timeout</html>"
  assert refused(github).status_code == 502
  assert refused(github).detail == subject.UNFINISHED
  assert len(github.enqueues) == 1


def test_an_unknown_merge_is_released_once_its_dispatch_window_has_passed():
  github = FakeGitHub()
  github.merge_answer = subprocess.TimeoutExpired("gh", 90)
  refused(github)
  attempt_row().armed_at = "2020-01-01T00:00:00Z"
  LEDGER["db"].commit()
  github.merge_answer = {"merged": True, "sha": MERGED_SHA}
  github.pull.update(merged=False, state="open")
  assert press(github)["state"] == "merged"
  assert len(github.merges) == 2 and attempt_row().state == "merged"


def test_a_known_refusal_frees_the_attempt_so_the_owner_can_press_again():
  github = FakeGitHub()
  github.merge_answer = ContributionSubmitError("gh: Pull Request is not mergeable (HTTP 405)")
  assert refused(github).status_code == 409
  assert LEDGER["db"].query(models.DirectMergeAttempt).count() == 0
  github.merge_answer = {"merged": True, "sha": MERGED_SHA}
  assert press(github)["state"] == "merged"


def test_a_caller_that_goes_away_during_the_write_leaves_the_attempt_saved():
  github = FakeGitHub()
  entered, release = threading.Event(), threading.Event()
  def hold_the_write(flags):
    entered.set()
    assert release.wait(5)
    return FakeGitHub._answer(github.merge_answer)
  github._merge = hold_the_write
  async def scenario():
    task = asyncio.create_task(subject.merge_exact_pull(github, None, body(), lambda: None, LEDGER["db"], LEDGER["owner_id"], job=None))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
      await task
  asyncio.run(scenario())
  assert attempt_row().state == "attempting"
  assert refused(github).detail == subject.UNFINISHED


def test_an_attempt_a_review_run_already_armed_for_this_head_is_not_sent_again():
  github = FakeGitHub()
  armed = {"head_sha": SHA, "state": "merging", "merge_attempted": True}
  db = LEDGER["db"]
  chat = create_chat(id="review-chat", title="Review")
  db.add(chat)
  db.commit()
  db.add(models.ContributionReviewRun(id="batch", app_id=None, owner_id=LEDGER["owner_id"], request_id="r1", mode="review_merge",
    targets_json=[TARGET], outcomes_json={reviews.key(TARGET): armed}, chat_id=chat.id, github_actor_id="42"))
  db.commit()
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, subject.ALREADY_ATTEMPTED)
  assert github.writes == 0


def test_a_pull_retargeted_after_it_was_read_is_refused_before_the_write():
  github = FakeGitHub()
  def retarget_on_the_first_base_read(count):
    if count == 1:
      github.pull.update(base={**github.pull["base"], "ref": "release"})
  github.on_base_read = retarget_on_the_first_base_read
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, "The pull request changed since it was checked. Refresh and check it again.")
  assert github.writes == 0


# Authority that stops standing, and a caller that goes away

REVOKED = "Contribute's permission changed. No new review or merge was started."


def revoked_on_call(number):
  """An authorize hook that starts refusing on its `number`th call."""
  calls = []
  def authorize():
    calls.append(1)
    if len(calls) == number:
      raise HTTPException(409, REVOKED)
  return authorize


def test_authority_lost_before_any_github_call_stops_the_merge_at_once():
  github = FakeGitHub()
  refusal = refused(github, revoked_on_call(1))
  assert (refusal.status_code, refusal.detail) == (409, REVOKED)
  assert github.calls == []


@pytest.mark.parametrize("call", [2, 3], ids=["before the base check", "right before the write"])
def test_authority_lost_after_the_reads_still_stops_the_write(call):
  github = FakeGitHub()
  refusal = refused(github, revoked_on_call(call))
  assert (refusal.status_code, refusal.detail) == (409, REVOKED)
  assert github.calls and github.writes == 0


def test_a_cancelled_request_never_starts_the_write_and_its_cancellation_is_not_swallowed():
  github = FakeGitHub()
  entered, release = threading.Event(), threading.Event()
  def hold_the_final_base_check(count):
    if count == 2:
      entered.set()
      assert release.wait(5)
  github.on_base_read = hold_the_final_base_check
  async def scenario():
    task = asyncio.create_task(subject.merge_exact_pull(github, None, body(), lambda: None, LEDGER["db"], LEDGER["owner_id"], job=None))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
      await task
  asyncio.run(scenario())
  assert github.writes == 0


# One log line per attempt

def test_each_attempt_logs_one_line_with_repo_number_head_and_outcome_and_no_response_body(caplog):
  caplog.set_level(logging.INFO, logger=subject.__name__)
  press(FakeGitHub())
  drafted = FakeGitHub()
  drafted.pull.update(draft=True)
  refused(drafted)
  lines = [record.getMessage() for record in caplog.records if record.name == subject.__name__]
  assert lines == [f"Direct merge of {NAME}#{NUMBER} at {SHA}: merged",
                   f"Direct merge of {NAME}#{NUMBER} at {SHA}: HTTP 409, This pull request is still a draft."]
  assert RESPONSE_BODY not in caplog.text


def test_an_unconfirmed_attempt_is_logged_as_such(caplog):
  caplog.set_level(logging.INFO, logger=subject.__name__)
  github = FakeGitHub()
  github.merge_answer = subprocess.TimeoutExpired("gh", 90)
  refused(github)
  assert [r.getMessage() for r in caplog.records if r.name == subject.__name__] == [
    f"Direct merge of {NAME}#{NUMBER} at {SHA}: HTTP 502, merge_unconfirmed"]


# Over HTTP, with real bearers: who may press it

@pytest.fixture
def desk(client, owner_token, db, monkeypatch):
  """The real app with Contribute installed and GitHub faked, and a bearer of every kind."""
  owner = db.query(models.Owner).first()
  contribute = models.App(name="Contribute", slug="contribute", source_dir="test-contribute",
                          github_access=True, token_nonce="contribute-nonce")
  other = models.App(name="Other", slug="other", source_dir="test-other",
                     github_access=True, token_nonce="other-nonce")
  db.add_all([contribute, other, create_chat(id="desk-chat", title="Desk"),
              models.ChatRun(id="desk-run", chat_id="desk-chat", status="running")])
  db.commit()
  def app_token(app, **lineage):
    return auth_mod.create_app_token(app.id, owner.username, owner.token_epoch,
                                     app_nonce=app.token_nonce, **lineage)
  delegated = {"delegation_id": "child", "delegation_chat": "desk-chat"}
  tokens = {
    "owner": owner_token,
    "contribute": app_token(contribute),
    "other_app": app_token(other),
    "agent": auth_mod.create_agent_token("desk-chat", owner.username, owner.token_epoch, run_id="desk-run"),
    "delegated_agent": auth_mod.create_agent_token("desk-chat", owner.username, owner.token_epoch, **delegated),
    "delegated_app": app_token(contribute, **delegated),
  }
  github = FakeGitHub()
  monkeypatch.setattr(routes, "_gh", github)

  def post(who, payload=None, **headers):
    return client.post(f"/api/github/contributions/{contribute.id}/pulls/merge",
                       json={"repo": NAME, "number": NUMBER, "head_sha": SHA} if payload is None else payload,
                       headers={"Authorization": f"Bearer {tokens[who]}", **headers})
  return SimpleNamespace(app_id=contribute.id, github=github, post=post, client=client,
                         owner={"Authorization": f"Bearer {owner_token}"})


MERGED_ANSWER = {"state": "merged", "merge_sha": MERGED_SHA, "method": "squash",
                 "repo": NAME, "number": NUMBER, "head_sha": SHA}


@pytest.mark.parametrize("who", ["agent", "delegated_agent", "delegated_app"])
def test_an_agent_can_never_press_merge(desk, who):
  response = desk.post(who)
  assert response.status_code == 403, response.text
  assert desk.github.calls == []


@pytest.mark.parametrize("who", ["owner", "contribute"])
def test_the_owner_and_the_contribute_app_can_press_merge(desk, who):
  response = desk.post(who)
  assert response.status_code == 200, response.text
  assert response.json() == MERGED_ANSWER
  assert desk.github.merges == [{"sha": SHA, "merge_method": "squash"}]


def test_another_app_cannot_press_merge(desk):
  response = desk.post("other_app")
  assert response.status_code == 403, response.text
  assert desk.github.calls == []


def test_an_app_without_github_access_cannot_press_merge(desk, db):
  db.query(models.App).filter_by(slug="contribute").update({"github_access": False})
  db.commit()
  assert desk.post("contribute").status_code == 403
  assert desk.post("owner").status_code == 409
  assert desk.github.calls == []


def test_a_request_without_a_bearer_cannot_press_merge(desk):
  response = desk.client.post(f"/api/github/contributions/{desk.app_id}/pulls/merge",
                              json={"repo": NAME, "number": NUMBER, "head_sha": SHA})
  assert response.status_code == 401
  assert desk.github.calls == []


def test_a_cross_site_request_cannot_press_merge(desk):
  response = desk.post("owner", **{"Sec-Fetch-Site": "cross-site"})
  assert response.status_code == 403, response.text
  assert desk.github.calls == []


def test_the_contribute_apps_own_sandboxed_frame_can_press_merge(desk):
  # The shell's opaque frame sends Origin: null and a cross-site fetch label with its app bearer.
  response = desk.post("contribute", **{"Sec-Fetch-Site": "cross-site", "Origin": "null"})
  assert response.status_code == 200, response.text


# Over HTTP: the exact shapes the Contribute app reads

def test_a_refusal_is_a_409_whose_detail_is_the_sentence_to_show(desk):
  desk.github.pull.update(draft=True)
  response = desk.post("contribute")
  assert (response.status_code, response.json()) == (409, {"detail": "This pull request is still a draft."})
  assert desk.github.writes == 0


def test_an_unreadable_pull_request_is_a_404_with_a_plain_sentence(desk):
  desk.github.unreadable.add("pull")
  response = desk.post("contribute")
  assert (response.status_code, response.json()) == (404, {"detail": subject.UNREADABLE})


def test_an_unconfirmed_write_is_a_502_merge_unconfirmed(desk):
  desk.github.merge_answer = "<html>gateway timeout</html>"
  response = desk.post("contribute")
  assert (response.status_code, response.json()) == (502, {"detail": {
    "code": "merge_unconfirmed",
    "message": "GitHub may have merged it. Check the pull request before trying again."}})
  assert len(desk.github.merges) == 1


def test_a_queued_pull_request_is_a_200_with_no_merge_sha_and_no_method(desk):
  desk.github.checks.update(isMergeQueueEnabled=True)
  response = desk.post("contribute", {"repo": NAME, "number": NUMBER, "head_sha": SHA, "method": None})
  assert (response.status_code, response.json()) == (200, {
    "state": "queued", "merge_sha": None, "method": None, "repo": NAME, "number": NUMBER, "head_sha": SHA})


@pytest.mark.parametrize("payload", [
  {"number": NUMBER, "head_sha": SHA},
  {"repo": NAME, "head_sha": SHA},
  {"repo": NAME, "number": NUMBER},
  {"repo": "not a repository", "number": NUMBER, "head_sha": SHA},
  {"repo": "../../etc/passwd", "number": NUMBER, "head_sha": SHA},
  {"repo": NAME, "number": 0, "head_sha": SHA},
  {"repo": NAME, "number": "seven", "head_sha": SHA},
  {"repo": NAME, "number": NUMBER, "head_sha": "abc123"},
  {"repo": NAME, "number": NUMBER, "head_sha": SHA.upper()},
  {"repo": NAME, "number": NUMBER, "head_sha": SHA, "method": "fast-forward"},
  {"repo": NAME, "number": NUMBER, "head_sha": SHA, "merge_method": "squash"},
])
def test_a_malformed_body_is_a_422_and_nothing_reaches_github(desk, payload):
  response = desk.post("contribute", payload)
  assert response.status_code == 422, response.text
  assert desk.github.calls == []


def test_the_capability_is_advertised_in_both_presets_views(desk):
  app_view = desk.client.get(f"/api/github/contributions/{desk.app_id}/review-presets", headers=desk.owner)
  core_view = desk.client.get("/api/github/review-presets", headers=desk.owner)
  assert app_view.status_code == core_view.status_code == 200
  assert app_view.json()["capabilities"]["merge_now"] is True
  assert core_view.json()["capabilities"]["merge_now"] is True
