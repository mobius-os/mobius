"""The confirm sheets and refusals use the PR machine's words: the sheet is the confirmation, a verdict is discarded."""
import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import contribution_pr_machine as machine
from app.routes import contribution_owner_grants as grants
from app.routes import contribution_reviews as routes
from tests.test_contribution_direct_merge import NAME, NUMBER, SHA, desk  # noqa: F401  (fixture and constants)
from tests.test_contribution_pull_status import StatusGitHub
from tests.test_contribution_review_verdicts import KEY, _decide, _grant, _record, setup  # noqa: F401  (fixture)
from tests.test_contribution_review_runs import report


def preview(desk, monkeypatch, action, **github):
  monkeypatch.setattr(routes, "_gh", StatusGitHub(**github))
  sheet = desk.client.post("/api/owner-grants/preview", headers=desk.owner, json={
    "app_id": desk.app_id, "action": action, "scope": {},
    "body": {"repo": NAME, "number": NUMBER, "head_sha": SHA}})
  assert sheet.status_code == 200, sheet.text
  return sheet.json()


def test_the_close_sheet_does_not_promise_a_confirmation_it_already_is(desk, monkeypatch):
  sheet = preview(desk, monkeypatch, "close")
  assert sheet["headline"] == f"Close pull request: {NAME}#{NUMBER}" and sheet["verb"] == "Close pull request"
  assert [fact["label"] for fact in sheet["facts"]] == ["Pull request", "Author", "Merges into", "Version", "Where it stands"]


def test_the_machine_keeps_its_own_words_for_close(desk, monkeypatch):
  """Only the sheet drops the row: the action list the app shows still carries the detail."""
  facts = {"role": "author", "lifecycle": "open", "draft": False, "permission": "admin"}
  decided = machine.decide(facts)
  close = next(action for action in decided["actions"] if action["id"] == "close")
  assert close["title"] == "Close pull request" and close["detail"] == "Asks you to confirm first."


@pytest.mark.parametrize("action, github", [("open_for_review", {"draft": True}), ("back_to_draft", {})])
def test_the_other_status_sheets_are_unchanged(desk, monkeypatch, action, github):
  sheet = preview(desk, monkeypatch, action, **github)
  assert [fact["label"] for fact in sheet["facts"]][-1] == "Where it stands"


def test_the_sheet_for_an_unconfirmed_verdict_says_discard_like_the_machine():
  body = routes.VerdictDecision(repo=NAME, number=NUMBER, head_sha=SHA, proposal_id="a" * 32, decision="dismiss")
  sheet = asyncio.run(grants._describe_verdict(SimpleNamespace(body=body)))
  assert (sheet["headline"], sheet["verb"]) == (f"Discard the unconfirmed verdict on {NAME}#{NUMBER}", "Discard")
  assert "Dismiss" not in str(sheet)


def test_a_verdict_that_is_not_unconfirmed_is_refused_in_the_words_of_discard(setup, monkeypatch):
  _grant(setup, propose_verdict=True)
  _record(monkeypatch, [])
  proposal = report(setup, publish=True, state="needs_you", summary="Blocking bug")["run"]["items"][0]["verdict_proposal"]
  with pytest.raises(HTTPException) as refused:
    _decide(setup, "dismiss", proposal["id"])
  assert refused.value.detail == "Only a verdict GitHub never confirmed can be discarded. Refresh to see its result."
