"""The review-runs list never hides an unfinished run behind its page size, and says whether more exist.

The newest 100 runs used to be the whole answer: older runs vanished without a word, finished or not. The
first page now holds every run that is not complete plus the newest finished ones, and `has_more` /
`next_cursor` page through the rest. A client that reads one page keeps working.
"""
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app import contribution_review_queries as queries, contribution_review_runs as domain, models
from app.routes import contribution_reviews as routes
from app.timeutil import now_naive_utc
from tests.test_contribution_review_runs import ITEM, SHA, setup  # noqa: F401  (the fixture)

KEY = domain.key(ITEM)
FINISHED = {KEY: {"state": "all_clear", "summary": "Reviewed", "head_sha": SHA}}


def add(db, like, run_id, minutes_ago, outcomes, mode="review", app_id=1):
  db.add(models.ContributionReviewRun(
    id=run_id, app_id=app_id, owner_id=like.owner_id, request_id=f"request-{run_id}", mode=mode,
    targets_json=like.targets_json, chat_id=like.chat_id, github_actor_id="42", app_nonce=like.app_nonce,
    options_json={}, outcomes_json=outcomes, created_at=now_naive_utc() - timedelta(minutes=minutes_ago)))
  db.commit()


def listed(setup, **query):
  db, _row, principal = setup
  return routes.list_reviews(1, db, principal, **query)


def ids(answer):
  return [run["id"] for run in answer["runs"]]


@pytest.fixture
def many(setup):
  """The fixture's own run is the oldest and still open; 101 finished runs are newer than it."""
  db, row, _ = setup
  row.created_at = now_naive_utc() - timedelta(days=30)
  db.commit()
  for index in range(101):
    add(db, row, f"finished-{index:03d}", 1000 - index, FINISHED)
  return setup


# --- characterization: a short list is one page, newest first, with nothing more behind it -------------

def test_a_short_list_is_one_complete_page_newest_first(setup):
  db, row, _ = setup
  add(db, row, "newer", 5, FINISHED)
  add(db, row, "newest", 1, FINISHED)
  answer = listed(setup)
  assert ids(answer) == [row.id, "newest", "newer"]
  assert answer["has_more"] is False and answer["next_cursor"] is None


def test_finished_runs_still_stop_at_one_hundred_on_the_first_page(many):
  db, row, _ = many
  db.get(models.ContributionReviewRun, row.id).outcomes_json = FINISHED
  db.commit()
  assert len(listed(many)["runs"]) == 100


# --- an unfinished run is never hidden behind the page size -------------------------------------------

def test_an_unfinished_run_older_than_the_newest_hundred_is_still_listed(many):
  db, row, _ = many
  answer = listed(many)
  assert row.id in ids(answer) and len(answer["runs"]) == 101
  assert ids(answer)[-1] == row.id  # still newest first: the old open run comes last
  assert answer["has_more"] is True and answer["next_cursor"]


def test_the_cursor_pages_through_the_finished_runs_and_lists_none_twice(many):
  db, row, _ = many
  first = listed(many)
  second = listed(many, cursor=first["next_cursor"])
  assert len(second["runs"]) == 1 and second["has_more"] is False and second["next_cursor"] is None
  assert row.id not in ids(second)
  everything = ids(first) + ids(second)
  assert len(everything) == len(set(everything)) == 102


def test_a_small_limit_pages_and_keeps_every_unfinished_run_on_the_first_page(many):
  db, row, _ = many
  seen = ids(listed(many, limit=40))
  assert row.id in seen and len(seen) == 41
  page = listed(many, limit=40)
  collected = list(ids(page))
  while page["has_more"]:
    page = listed(many, limit=40, cursor=page["next_cursor"])
    collected += ids(page)
  assert len(collected) == len(set(collected)) == 102


def test_a_cursor_that_is_not_one_the_list_returned_is_refused(setup):
  with pytest.raises(HTTPException) as refused:
    listed(setup, cursor="yesterday|x")
  assert refused.value.status_code == 400


def test_a_run_waiting_on_a_verdict_or_a_readiness_check_counts_as_unfinished(setup):
  db, row, _ = setup
  row.created_at = now_naive_utc() - timedelta(days=30)
  db.commit()
  waiting = {KEY: {"state": "all_clear", "summary": "Reviewed", "head_sha": SHA,
                   "verdict_proposal": {"id": "v1", "state": "unknown", "event": "APPROVE", "head_sha": SHA}}}
  add(db, row, "unknown-verdict", 20000, waiting)
  for index in range(100):
    add(db, row, f"finished-{index:03d}", 1000 - index, FINISHED)
  assert {"unknown-verdict", row.id} <= set(ids(listed(setup)))


def verdict(state, event="APPROVE", **extra):
  return {"verdict_proposal": {"id": "v1", "state": state, "event": event, "head_sha": SHA, **extra}}


def clear(**extra):
  return {"state": "all_clear", "summary": "Reviewed", "head_sha": SHA, **extra}


# Every shape `contribution_review_runs.view` overlays on a saved item, in each mode it matters in:
# (name, mode, saved item, whether view calls the run complete, whether the query must agree exactly).
# "Exactly" is for the finished shapes a long-lived instance holds many of: listing them all on the first
# page would undo the page size. The query may be wider than `view` only where the second flag is False.
SHAPES = [
  ("no result yet", "review", None, False, True),
  ("plain all clear", "review", clear(), True, True),
  ("merged", "review_merge", {"state": "merged", "head_sha": SHA}, True, True),
  ("merged after a posted approval", "review_merge", {"state": "merged", "head_sha": SHA, **verdict("posted")}, True, True),
  ("approval posted, plain review", "review", clear(**verdict("posted")), True, True),
  ("comment posted, merge run", "review_merge", clear(**verdict("posted", "COMMENT")), True, True),
  ("discarded, plain review", "review", clear(**verdict("discarded")), True, True),
  ("public review posted", "review", clear(public_review={"state": "posted"}), True, True),
  ("approval posted, merge never ran", "review_merge", clear(**verdict("posted")), False, True),
  ("approval posted then dismissed on GitHub", "review_merge",
   clear(**verdict("posted", dismissed_on_github=True)), True, False),
  ("approval posted, merge attempted", "review_merge", clear(**verdict("posted"), merge_attempted=True), True, False),
  ("discarded, merge run", "review_merge", clear(**verdict("discarded")), False, True),
  *[(f"verdict {state}", mode, clear(**verdict(state)), False, True)
    for state in ("proposed", "posting", "unknown", "not_dispatched") for mode in ("review", "review_merge")],
  *[(f"readiness {state}", "review", clear(ready_attempt={"state": state}), False, True)
    for state in ("attempting", "unknown", "unattributable")],
  *[(f"public review {state}", "review", clear(public_review={"state": state}), False, True)
    for state in ("posting", "unknown", "not_dispatched")],
  *[(f"word {word}", "review", {"state": word, "head_sha": SHA}, False, True)
    for word in ("reviewing", "needs_you", "merge_unknown", "merging", "repairing", "pushing", "queued")],
  ("successor with no repair receipt", "review_fix_merge",
   clear(successor={"head_sha": "b" * 40, "base_sha": "c" * 40}), False, True),
]


@pytest.mark.parametrize("name, mode, outcome, complete, exact", SHAPES, ids=[shape[0] for shape in SHAPES])
def test_the_database_question_misses_no_run_view_calls_not_complete(setup, name, mode, outcome, complete, exact):
  """The query only narrows the rows `view` reads, so a run `view` calls anything but complete must be in it."""
  db, row, _ = setup
  db.delete(row)
  db.commit()
  add(db, row, "probe", 1, {} if outcome is None else {KEY: outcome}, mode=mode)
  probe = db.get(models.ContributionReviewRun, "probe")
  assert (domain.view(probe)["state"] == "complete") is complete  # the shape means what its row says
  listed_by_query = "probe" in queries.run_ids(db, queries.UNFINISHED, probe.owner_id)
  assert listed_by_query or complete
  assert listed_by_query is not complete or not exact


def test_the_database_question_is_scoped_to_the_owner(setup):
  db, row, _ = setup
  assert queries.run_ids(db, queries.UNFINISHED, row.owner_id) == [row.id]
  assert queries.run_ids(db, queries.UNFINISHED, row.owner_id + 1) == []


def test_the_core_list_pages_the_same_way(setup):
  db, row, principal = setup
  from app.deps import Principal
  owner = Principal(owner=principal.owner, app_id=None)
  assert routes.core_list_reviews(db, owner)["runs"] == []  # the fixture's run belongs to an app
  assert routes.core_list_reviews(db, owner, limit=5, cursor=None)["has_more"] is False


def test_the_core_route_forwards_its_limit_and_cursor_to_the_list(setup):
  """A core run (no app) pages through `GET /api/github/review-runs` as it does through an app's route."""
  db, row, principal = setup
  from app.deps import Principal
  owner = Principal(owner=principal.owner, app_id=None)
  for index in range(3):
    add(db, row, f"core-{index}", 10 - index, FINISHED, app_id=None)
  first = routes.core_list_reviews(db, owner, limit=2)
  assert ids(first) == ["core-2", "core-1"] and first["has_more"] is True and first["next_cursor"]
  second = routes.core_list_reviews(db, owner, limit=2, cursor=first["next_cursor"])
  assert ids(second) == ["core-0"] and second["has_more"] is False and second["next_cursor"] is None
