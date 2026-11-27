"""The server says, per review item, whether its findings can be posted, with a stable code for why not.

The Contribute app offered "Post findings" in cases the server always refuses (a blocked review, a blank summary,
any verdict, a receipt already held). The refusal words did not change; the run view now carries the same decision as
data, so the button is offered exactly when the server would accept the saved item.
"""
import pytest

from app import contribution_review_post_findings as post_findings
from app import contribution_review_runs as domain
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, SHA, setup  # noqa: F401  (the fixture)

KEY = domain.key(ITEM)
FINDINGS = {"state": "needs_you", "summary": "P1: a real bug", "head_sha": SHA}


@pytest.mark.parametrize("changes, code", [
  ({}, None),
  ({"state": "all_clear"}, None),
  ({"public_review": {"state": "not_dispatched"}}, None),  # refused before it reached GitHub: postable again
  ({"blocked": "The diff could not be read"}, "blocked"),
  ({"blocked": True}, "blocked"),
  ({"summary": ""}, "no_result"),
  ({"summary": "   "}, "no_result"),
  ({"state": "reviewing"}, "no_result"),
  ({"state": "merged"}, "no_result"),
  ({"state": "failed"}, "no_result"),
  ({"verdict_proposal": {"state": "discarded"}}, "has_verdict"),
  ({"verdict_proposal": {"state": "not_dispatched"}}, "has_verdict"),
  ({"verdict_proposal": {"state": "proposed"}}, "has_verdict"),
  ({"public_review": {"state": "posting"}}, "already_sent"),
  ({"public_review": {"state": "posted"}}, "already_sent"),
  ({"public_review": {"state": "unknown"}}, "already_sent"),
])
def test_the_code_is_the_first_reason_the_saved_item_cannot_be_posted(changes, code):
  item = {**FINDINGS, **changes}
  assert post_findings.refusal_code(item) == code
  assert post_findings.offer(item) == ({"allowed": True} if code is None else {"allowed": False, "code": code})


@pytest.mark.parametrize("changes", [{"blocked": True}, {"summary": ""}, {"verdict_proposal": {"state": "discarded"}},
                                     {"public_review": {"state": "posted"}}])
def test_the_server_refuses_exactly_what_the_code_names_in_the_words_it_always_used(changes):
  item = {**FINDINGS, **changes}
  code = post_findings.refusal_code(item)
  with pytest.raises(Exception) as refused:
    post_findings.preview(item, {"head_sha": SHA})
  assert refused.value.status_code == 409 and refused.value.detail == post_findings.REFUSALS[code]


def test_the_words_are_the_ones_the_owner_already_read():
  assert post_findings.REFUSALS == {
    "blocked": "This review did not finish, so there is nothing to post. Review again first.",
    "no_result": "This review has no finished result to post.",
    "has_verdict": "This review already has a verdict. Submit or discard that one.",
    "already_sent": "This review was already sent to GitHub. Check the pull request."}


def test_every_item_of_the_run_view_and_the_summary_list_carries_the_decision(setup):
  db, row, principal = setup
  full = routes.get_review(1, row.id, db, principal)["run"]["items"][0]
  assert full["post_findings"] == {"allowed": False, "code": "no_result"}  # still reviewing: nothing to post
  domain.save_outcome(db, row, KEY, FINDINGS)
  assert routes.get_review(1, row.id, db, principal)["run"]["items"][0]["post_findings"] == {"allowed": True}
  listed = routes.list_reviews(1, db, principal, view="summary")["runs"][0]["items"][0]
  assert listed["post_findings"] == {"allowed": True}
  domain.save_outcome(db, row, KEY, {**FINDINGS, "verdict_proposal": {"id": "v1", "state": "discarded"}})
  listed = routes.list_reviews(1, db, principal, view="summary")["runs"][0]["items"][0]
  assert listed["post_findings"] == {"allowed": False, "code": "has_verdict"}


def test_the_decision_comes_from_the_saved_item_not_the_state_the_view_shows(setup):
  """A verdict still waiting shows the item as `verdict_pending`; what the server checks is the saved review."""
  db, row, principal = setup
  domain.save_outcome(db, row, KEY, {**FINDINGS, "verdict_proposal": {"id": "v1", "state": "proposed", "event": "REQUEST_CHANGES"}})
  item = routes.get_review(1, row.id, db, principal)["run"]["items"][0]
  assert item["state"] == "verdict_pending" and item["post_findings"] == {"allowed": False, "code": "has_verdict"}
