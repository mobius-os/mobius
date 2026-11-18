"""A review ends finished or blocked, and the owner can post a finished private one."""
import asyncio
import re

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import contribution_review_list as list_view
from app import contribution_review_public as public
from app import contribution_review_runs as domain
from app.contribution_review_effects import DispatchResult
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, PULL, REPO, report, setup  # noqa: F401

IDENTITY = {k: ITEM[k] for k in ("repo", "number", "head_sha")}
FINDINGS = {"state": "needs_you", "summary": "The retry sends the message twice."}


def _review(setup, **options):
  db, row, _ = setup
  row.mode = "review"
  row.options_json = options
  db.commit()
  return db, row


def _posts(monkeypatch, *, state="confirmed"):
  sent = []
  def post(_gh, _cwd, target, body, event="COMMENT"):
    sent.append((event, target["head_sha"], body))
    if state == "confirmed":
      return DispatchResult.confirmed({"id": 9, "url": "https://github.com/x#review-9"})
    return DispatchResult.unknown("lost response")
  monkeypatch.setattr(public, "post_review", post)
  return sent


def _owner(setup):
  return Principal(owner=setup[2].owner, app_id=None)


def _preview(setup):
  db, row, _ = setup
  return routes.preview_posted_findings(1, row.id, routes.PullIdentity(**IDENTITY), db, _owner(setup))


def _post(setup, preview_sha256, principal=None):
  db, row, _ = setup
  body = routes.PostFindings(**IDENTITY, preview_sha256=preview_sha256)
  return asyncio.run(routes.post_findings_now(1, row.id, body, db, principal or _owner(setup)))


# A review ends finished or blocked

def test_a_publishing_review_cannot_save_unfinished_findings_privately(setup):
  _review(setup, post_review=True, propose_verdict=True)
  with pytest.raises(HTTPException) as refused:
    report(setup, state="needs_you", summary="Review incomplete, not a verdict.")
  assert refused.value.status_code == 422 and "finish it first" in refused.value.detail
  assert setup[1].outcomes_json == {}


def test_a_named_obstacle_is_saved_privately_as_a_blocked_review(setup):
  _review(setup, post_review=True, propose_verdict=True)
  item = report(setup, state="needs_you", summary="Could not read the PR.",
                blocked="GitHub refuses the diff: 404")["run"]["items"][0]
  assert item["state"] == "needs_you" and item["blocked"] == "GitHub refuses the diff: 404"
  assert "verdict_proposal" not in item and "public_review" not in item


def test_a_later_finished_result_clears_the_blocked_mark(setup):
  _review(setup)
  report(setup, state="needs_you", summary="Could not read the PR.", blocked="No access")
  assert report(setup, **FINDINGS)["run"]["items"][0]["blocked"] is None


@pytest.mark.parametrize("changes", [{"state": "all_clear"}, {"state": "needs_you", "publish": True}])
def test_a_blocked_review_is_never_a_verdict(changes):
  with pytest.raises(ValidationError):
    routes.ReviewOutcome(**{**ITEM, "summary": "x", "blocked": "No access", **changes})


def test_a_private_review_still_reports_its_findings_privately(setup):
  _review(setup)
  assert report(setup, **FINDINGS)["run"]["items"][0]["state"] == "needs_you"


def test_every_review_brief_says_to_finish_or_name_the_obstacle(setup):
  _db, row = _review(setup, post_review=True, propose_verdict=True)
  assert domain.FINISH_RULE in domain.brief(row)
  row.options_json = {}
  assert domain.FINISH_RULE in domain.brief(row)


def test_a_list_row_says_a_review_is_blocked_without_its_reason():
  assert list_view.summary_item({**ITEM, "state": "needs_you", "blocked": "No access"})["blocked"] is True
  assert "blocked" not in list_view.summary_item({**ITEM, "state": "needs_you", "blocked": None})


# Post to GitHub for a finished private review

def test_the_owner_posts_exactly_the_previewed_words_once_as_a_comment(setup, monkeypatch):
  _review(setup)
  sent = _posts(monkeypatch)
  report(setup, **FINDINGS)
  words = _preview(setup)
  assert FINDINGS["summary"] in words["body"] and words["head_sha"] == ITEM["head_sha"]
  assert sent == []
  receipt = _post(setup, words["preview_sha256"])
  assert receipt["state"] == "posted" and receipt["url"]
  (event, head, body), = sent
  assert (event, head) == ("COMMENT", ITEM["head_sha"])
  assert body.startswith(words["body"]) and "mobius-review:" in body
  with pytest.raises(HTTPException) as again:
    _post(setup, words["preview_sha256"])
  assert again.value.status_code == 409 and len(sent) == 1


def _visible(body):
  """The posted body with its hidden per-dispatch marker removed: what a reader sees."""
  return re.sub(r"\n\n<!-- mobius-review:[0-9a-f]+ -->\Z", "", body)


@pytest.mark.parametrize("summary,tests", [
  ("The retry sends the message twice.", "Focused tests passed"),  # short: never clipped
  ("x" * 70_000, "Focused tests passed"),                          # the summary must be clipped
  ("The retry sends the message twice.", "y" * 70_000),            # the verification must be clipped
  ("\U0001f600" * 40_000, "\U0001f642" * 40_000),                  # astral chars: two UTF-16 units each
])
def test_the_posted_words_match_the_preview_even_when_evidence_is_clipped(setup, monkeypatch, summary, tests):
  # The preview the owner confirms is marker-free; the post adds a hidden marker
  # that shrinks the body budget. Reserving that room keeps the visible words and
  # the omission count identical, so "Posted exactly as" stays true past the limit.
  _review(setup)
  sent = _posts(monkeypatch)
  report(setup, state="needs_you", summary=summary, tests=tests)
  words = _preview(setup)
  receipt = _post(setup, words["preview_sha256"])
  assert receipt["state"] == "posted"
  (_event, _head, body), = sent
  assert "mobius-review:" in body and _visible(body) == words["body"]
  assert public._body_units(body) <= public.GITHUB_REVIEW_BODY_MAX


def test_the_review_agent_cannot_post_its_own_findings(setup, monkeypatch):
  _review(setup)
  sent = _posts(monkeypatch)
  report(setup, **FINDINGS)
  with pytest.raises(HTTPException) as refused:
    _post(setup, _preview(setup)["preview_sha256"], principal=setup[2])
  assert refused.value.status_code == 403 and sent == []


def test_words_that_changed_since_the_preview_are_not_posted(setup, monkeypatch):
  _review(setup)
  sent = _posts(monkeypatch)
  report(setup, **FINDINGS)
  read = _preview(setup)["preview_sha256"]
  report(setup, state="needs_you", summary="A different finding.")
  with pytest.raises(HTTPException) as refused:
    _post(setup, read)
  assert refused.value.status_code == 409 and sent == []


def test_a_blocked_review_has_nothing_to_post(setup):
  _review(setup)
  report(setup, state="needs_you", summary="Could not read the PR.", blocked="No access")
  with pytest.raises(HTTPException) as refused:
    _preview(setup)
  assert refused.value.status_code == 409 and "did not finish" in refused.value.detail


def test_a_review_with_a_verdict_is_not_posted_a_second_way(setup, monkeypatch):
  _review(setup, post_review=True, propose_verdict=True)
  _posts(monkeypatch)
  report(setup, publish=True, **FINDINGS)
  with pytest.raises(HTTPException) as refused:
    _preview(setup)
  assert refused.value.status_code == 409 and "verdict" in refused.value.detail


def test_a_review_of_your_own_pull_request_stays_private(setup, monkeypatch):
  _review(setup)
  sent = _posts(monkeypatch)
  report(setup, **FINDINGS)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "user": {"id": 42}}))
  with pytest.raises(HTTPException) as refused:
    _post(setup, _preview(setup)["preview_sha256"])
  assert refused.value.status_code == 409 and sent == []


def test_an_unconfirmed_post_is_reported_and_never_replayed(setup, monkeypatch):
  _db, row = _review(setup)
  sent = _posts(monkeypatch, state="unknown")
  report(setup, **FINDINGS)
  read = _preview(setup)["preview_sha256"]
  with pytest.raises(HTTPException) as unconfirmed:
    _post(setup, read)
  assert unconfirmed.value.status_code == 502 and unconfirmed.value.detail["code"] == "post_unconfirmed"
  assert row.outcomes_json[domain.key(ITEM)]["public_review"]["state"] == "unknown"
  with pytest.raises(HTTPException) as again:
    _post(setup, read)
  assert again.value.status_code == 409 and len(sent) == 1


def test_the_confirm_sheet_quotes_the_exact_words_and_refuses_stale_ones(setup):
  from app.routes import contribution_owner_grants as grants
  db, row = _review(setup)
  report(setup, **FINDINGS)
  words = _preview(setup)
  def ask(sha):
    return grants.Ask(db, _owner(setup), 1, {"run_id": row.id}, routes.PostFindings(**IDENTITY, preview_sha256=sha))
  sheet = asyncio.run(grants.GUARDED["post_findings"].describe(ask(words["preview_sha256"])))
  assert {"label": "Posted exactly as", "value": words["body"]} in sheet["facts"] and sheet["tone"] == "caution"
  with pytest.raises(HTTPException) as stale:
    asyncio.run(grants.GUARDED["post_findings"].describe(ask("0" * 64)))
  assert stale.value.status_code == 409
