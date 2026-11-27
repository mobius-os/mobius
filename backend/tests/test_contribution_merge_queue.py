"""A pull request the merge queue already holds is refused before any confirmation exists, not after it is spent.

The merge itself already refused a queued PR ("already in the merge queue"), but only at the end of the tap: the
owner confirmed a sheet, the grant was spent, and the refusal came after. The confirm sheet is built from the first
read of the PR, so that read now refuses it, and the PR machine's facts carry `queued` so Merge is off wherever the
machine is asked.
"""
import pytest

from app import contribution_pr_facts as pr_facts, contribution_pr_machine as machine, models
from tests.test_contribution_direct_merge import (QUEUE_ENTRY, SHA, FakeGitHub, desk, ledger,  # noqa: F401  (fixtures)
                                                  press, refused)
from tests.test_owner_grants import MERGE, confirm, declare_owner_confirm  # noqa: F401  (fixture and constants)

IN_QUEUE = "This pull request is already in the merge queue."


def queued(github, **changes):
  github.checks.update(isMergeQueueEnabled=True, mergeQueueEntry=QUEUE_ENTRY, **changes)
  return github


# --- characterization: what the merge did before, and must keep doing ---------------------------------

def test_the_merge_of_a_queued_pull_request_is_refused_in_these_words_and_writes_nothing():
  github = queued(FakeGitHub())
  refusal = refused(github)
  assert (refusal.status_code, refusal.detail) == (409, IN_QUEUE)
  assert github.writes == 0


def test_a_pull_request_in_a_queue_repository_that_is_not_queued_yet_is_enqueued_as_before():
  github = FakeGitHub()
  github.checks.update(isMergeQueueEnabled=True, mergeQueueEntry=None)
  assert press(github)["state"] == "queued"
  assert github.enqueues == [{"id": "PR_7", "head": SHA}]


def test_an_entry_the_queue_holds_for_another_head_is_not_this_pull_requests():
  github = queued(FakeGitHub(), headRefOid="e" * 40)
  assert refused(github).detail != IN_QUEUE  # the read of another head fails its own guards, not the queue's


# --- a queued pull request is refused at the sheet, so no confirmation is minted ----------------------

def test_no_confirm_sheet_is_shown_for_a_queued_pull_request(desk):
  queued(desk.github)
  sheet = desk.client.post("/api/owner-grants/preview",
                           json={"app_id": desk.app_id, "action": "merge", "scope": {}, "body": MERGE}, headers=desk.owner)
  assert (sheet.status_code, sheet.json()["detail"]) == (409, IN_QUEUE)


def test_no_confirmation_is_minted_for_a_queued_pull_request_so_none_is_spent(desk, db):
  declare_owner_confirm(db, "contribute")
  clear = desk.client.post("/api/owner-grants/preview",
                           json={"app_id": desk.app_id, "action": "merge", "scope": {}, "body": MERGE}, headers=desk.owner)
  assert clear.status_code == 200  # not queued yet: the sheet is shown as always
  queued(desk.github)
  minted = desk.client.post("/api/owner-grants", json={
    "app_id": desk.app_id, "action": "merge", "scope": {}, "body": MERGE, "digest": clear.json()["digest"]}, headers=desk.owner)
  assert (minted.status_code, minted.json()["detail"]) == (409, IN_QUEUE)
  assert db.query(models.OwnerGrant).count() == 0
  assert desk.post("contribute").status_code == 403  # nothing to spend: the press needs a confirmation it cannot get
  assert desk.github.writes == 0


def test_the_sheet_of_a_pull_request_that_is_not_queued_is_unchanged(desk):
  confirmed = confirm(desk, "merge", MERGE)
  assert confirmed.status_code == 200, confirmed.text


def test_a_press_by_the_owners_own_session_is_refused_in_the_same_words(desk):
  queued(desk.github)
  response = desk.post("owner")
  assert (response.status_code, response.json()) == (409, {"detail": IN_QUEUE})
  assert desk.github.writes == 0


# --- the machine's own fact ---------------------------------------------------------------------------

HEAD = SHA


@pytest.mark.parametrize("checks, expected", [
  ({"headRefOid": HEAD, "mergeQueueEntry": QUEUE_ENTRY}, True),
  ({"headRefOid": HEAD, "mergeQueueEntry": None}, False),
  ({"headRefOid": HEAD, "mergeQueueEntry": {}}, False),
  ({"headRefOid": "e" * 40, "mergeQueueEntry": QUEUE_ENTRY}, None),  # the queue's test commit is not this head
  ({"headRefOid": HEAD}, None),  # the read did not ask the queue
  (None, None),
])
def test_the_queue_fact_says_only_what_the_read_proved(checks, expected):
  assert pr_facts.queued_of(checks, HEAD) is expected


def base_facts():
  return {"role": "author", "author": "alex", "head": "h2", "lifecycle": "open", "checks": "pass", "mergeable": "yes",
          "permission": "WRITE", "readComplete": True, "openThreads": 0,
          "reviews": [{"by": "sam", "state": "APPROVED", "commit": "h2"}]}


def test_merge_is_off_in_the_machine_while_the_queue_holds_the_pull_request():
  facts = base_facts()
  assert machine.action_blocker(facts, "merge") == ""
  for unqueued in ({}, {"queued": None}, {"queued": False}):
    assert machine.action_blocker({**facts, **unqueued}, "merge") == ""
  assert machine.action_blocker({**facts, "queued": True}, "merge") == machine.IN_MERGE_QUEUE == IN_QUEUE
  decided = machine.decide({**facts, "queued": True})
  merge = next(action for action in decided["actions"] if action["id"] == "merge")
  assert (merge["enabled"], merge["reason"]) == (False, IN_QUEUE)


def test_the_queue_stops_only_merge_and_never_outranks_a_closed_or_unreadable_pull_request():
  facts = {**base_facts(), "queued": True}
  others = {action["id"] for action in machine.decide(facts)["actions"] if action["enabled"]}
  assert "merge" not in others and "close" in others
  assert machine.action_blocker({**facts, "lifecycle": "merged"}, "merge") == "This PR is no longer open."
  assert machine.action_blocker({**facts, "readComplete": False}, "merge") == "This PR could not be read. Try again."
