"""An agent review whose chat died stalls and says so, and a verdict that never got an answer times out.

The machine is the REVIEW and VERDICT regions of `contribution_lifecycle_tables`. The saved `state` word of an
item never changes for the stall (the Contribute app reads it); the vector lives in the item's `lifecycle` key,
the run's own view says `needs_you` / `interrupted` as it already did for a dead chat, and the periodic sweep
writes the move with no screen open.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal
from app import contribution_review_lifecycle as review_lifecycle
from app import contribution_review_queries as queries, contribution_review_runs as domain, models
from app import contribution_review_verdicts as verdicts
from app.routes import contribution_reviews as routes
from app.timeutil import now_naive_utc
from tests.test_contribution_review_admission import owner, refusal  # noqa: F401  (fixture and helper)
from tests.test_contribution_review_runs import ITEM, SHA, report, setup  # noqa: F401  (the fixture)

KEY = domain.key(ITEM)


def aged(db, row, minutes):
  row.created_at = now_naive_utc() - timedelta(minutes=minutes)
  db.commit()


def end_chat(db, status="interrupted", ended_minutes_ago=None):
  run = db.get(models.ChatRun, "physical-run")
  run.status = status
  run.ended_at = None if ended_minutes_ago is None else now_naive_utc() - timedelta(minutes=ended_minutes_ago)
  db.commit()


def quiet_for_an_hour(db, row):
  """An hour in, and the chat's last turn has only just ended: silent, but not yet the dead chat a restart leaves."""
  aged(db, row, 61)
  end_chat(db, "completed", ended_minutes_ago=0)


def revive(db):
  """The owner reopened the review chat: its run is working again (the same run row stands for it)."""
  run = db.get(models.ChatRun, "physical-run")
  run.status, run.ended_at = "running", None
  db.commit()


def stored(db, row):
  db.refresh(row)
  return (row.outcomes_json or {}).get(KEY, {})


def review_state(item):
  return item["lifecycle"]["regions"]["review"]["state"]


def view(setup):
  db, row, principal = setup
  return routes.get_review(1, row.id, db, principal)["run"]


def iso_ago(minutes):
  return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


# --- characterization: what the API said before the machine, and must keep saying ---------------------

def test_a_review_whose_chat_died_reads_needs_you_interrupted_and_its_item_stays_reviewing(setup):
  db, row, _ = setup
  end_chat(db, "interrupted")
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("needs_you", "interrupted")
  assert run["items"][0]["state"] == "reviewing"


def test_a_review_with_a_live_chat_inside_its_hour_reads_reviewing_and_running(setup):
  db, row, _ = setup
  aged(db, row, 10)
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("reviewing", "running")
  assert run["items"][0]["state"] == "reviewing"


def test_an_open_question_is_waiting_for_the_owner_however_old_the_review(setup):
  db, row, _ = setup
  aged(db, row, 600)
  db.get(models.Chat, row.chat_id).pending_question_id = "question-1"
  db.commit()
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("needs_you", "awaiting_owner")


# --- the stall is written, by a read and by the sweep -------------------------------------------------

def test_a_read_writes_the_stall_of_a_review_whose_chat_died(setup):
  db, row, _ = setup
  end_chat(db, "interrupted")
  view(setup)
  item = stored(db, row)
  assert review_state(item) == "STALLED"
  assert item["state"] == "reviewing"  # the word the app reads is untouched
  assert [entry["type"] for entry in item["lifecycle"]["history"]] == ["CHAT_ENDED"]


@pytest.mark.parametrize("shape", ["full", "summary"])
def test_a_list_read_writes_the_stall_of_a_review_whose_chat_died(setup, shape):
  """The list is the read the app's screens poll: a stall it finds is written, not only shown."""
  db, row, principal = setup
  end_chat(db, "interrupted")
  listed = routes.list_reviews(1, db, principal, view=shape)["runs"]
  assert review_state(listed[0]["items"][0]) == "STALLED"
  assert review_state(stored(db, row)) == "STALLED"
  assert [entry["type"] for entry in stored(db, row)["lifecycle"]["history"]] == ["CHAT_ENDED"]


def test_an_observe_read_writes_the_stall_of_a_review_whose_chat_died(setup):
  """Observing reads GitHub and settles lost receipts; the run it answers with is settled by the clock too."""
  db, row, principal = setup
  end_chat(db, "interrupted")
  answered = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert review_state(answered["items"][0]) == "STALLED"
  assert review_state(stored(db, row)) == "STALLED"


def test_the_sweep_stalls_a_dead_review_with_no_screen_open(setup):
  db, row, _ = setup
  end_chat(db, "failed")
  assert routes.sweep_review_lifecycles(db) == 1
  assert review_state(stored(db, row)) == "STALLED"
  assert routes.sweep_review_lifecycles(db) == 0  # settled: nothing left for the next tick


def test_an_hour_without_a_result_does_not_stall_a_review_whose_chat_is_still_working(setup):
  """The hour measures silence, not a long review: a live run is not silent."""
  db, row, _ = setup
  aged(db, row, 600)
  assert routes.sweep_review_lifecycles(db) == 0
  assert stored(db, row) == {}
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("reviewing", "running")
  assert review_state(run["items"][0]) == "REVIEWING"
  assert routes._pr_job(db, row.owner_id, ITEM["repo"], ITEM["number"])["state"] == "running"


def test_an_hour_without_a_result_stalls_a_review_whose_chat_has_gone_quiet(setup):
  db, row, _ = setup
  quiet_for_an_hour(db, row)
  assert routes.sweep_review_lifecycles(db) == 1
  item = stored(db, row)
  assert review_state(item) == "STALLED"
  assert [entry["type"] for entry in item["lifecycle"]["history"]] == ["TIMEOUT"]
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("needs_you", "interrupted")
  assert run["items"][0]["state"] == "reviewing"


def test_a_review_that_never_got_a_turn_is_stalled_and_says_why(setup):
  db, row, _ = setup
  db.delete(db.get(models.ChatRun, "physical-run"))
  aged(db, row, 61)
  assert routes.sweep_review_lifecycles(db) == 1
  assert [entry["type"] for entry in stored(db, row)["lifecycle"]["history"]] == ["CHAT_ENDED"]
  run = view(setup)
  assert (run["state"], run["execution_state"], run["summary"]) == ("needs_you", "interrupted", routes.STALLED_SUMMARY)
  assert run["items"][0]["state"] == "reviewing"


def test_a_review_inside_its_hour_with_a_live_chat_is_not_stalled(setup):
  db, row, _ = setup
  aged(db, row, 59)
  assert routes.sweep_review_lifecycles(db) == 0
  assert stored(db, row) == {}
  assert review_state(view(setup)["items"][0]) == "REVIEWING"


def test_a_turn_that_only_just_ended_is_not_yet_a_dead_review_in_the_machine(setup):
  db, row, _ = setup
  end_chat(db, "completed", ended_minutes_ago=0)
  assert routes.sweep_review_lifecycles(db) == 0
  end_chat(db, "completed", ended_minutes_ago=10)
  assert routes.sweep_review_lifecycles(db) == 1
  assert review_state(stored(db, row)) == "STALLED"


def test_a_question_holds_the_clock_and_the_answer_starts_it_again(setup):
  db, row, _ = setup
  aged(db, row, 600)
  chat = db.get(models.Chat, row.chat_id)
  chat.pending_question_id = "question-1"
  db.commit()
  assert routes.sweep_review_lifecycles(db) == 1
  assert review_state(stored(db, row)) == "ASKS"
  assert routes.sweep_review_lifecycles(db) == 0  # still asking: nothing to do, however old
  chat.pending_question_id = None
  db.commit()
  assert routes.sweep_review_lifecycles(db) == 1
  item = stored(db, row)
  assert review_state(item) == "REVIEWING"
  assert item["lifecycle"]["regions"]["review"]["deadline"] - item["lifecycle"]["regions"]["review"]["since"] == 3600
  assert routes.sweep_review_lifecycles(db) == 0  # a fresh hour: not stalled by the old one


def test_stop_ends_the_review_and_is_never_a_stall(setup):
  db, row, _ = setup
  aged(db, row, 600)
  db.get(models.ChatRun, "physical-run").status = "stopped"
  domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  assert routes.sweep_review_lifecycles(db) == 1
  assert review_state(stored(db, row)) == "STOPPED"
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("needs_you", "stopped")


def test_a_late_result_still_counts_after_a_stall(setup):
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  routes.sweep_review_lifecycles(db)
  assert review_state(stored(db, row)) == "STALLED"
  revive(db)
  report(setup)
  assert stored(db, row)["state"] == "all_clear"
  run = view(setup)
  assert review_state(run["items"][0]) == "CLEAR"
  assert (run["state"], run["items"][0]["state"]) == ("complete", "all_clear")


@pytest.mark.parametrize("changes, expected", [
  ({"state": "needs_you", "summary": "A real bug"}, "PROBLEMS"),
  ({"state": "needs_you", "summary": "No diff", "blocked": "The diff could not be read"}, "NOT_FINISHED"),
])
def test_a_late_result_of_findings_or_an_obstacle_moves_a_stalled_review(setup, changes, expected):
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  routes.sweep_review_lifecycles(db)
  revive(db)
  report(setup, **changes)
  assert review_state(view(setup)["items"][0]) == expected


def test_a_stalled_review_keeps_what_the_item_already_held(setup):
  db, row, _ = setup
  domain.save_outcome(db, row, KEY, {"reviewer_steps": [{"delegation_id": "d1"}]})
  quiet_for_an_hour(db, row)
  routes.sweep_review_lifecycles(db)
  item = stored(db, row)
  assert item["reviewer_steps"] == [{"delegation_id": "d1"}] and review_state(item) == "STALLED"


def test_a_finished_review_is_never_stalled_by_age_or_a_dead_chat(setup):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  report(setup)
  aged(db, row, 600)
  end_chat(db, "interrupted")
  assert routes.sweep_review_lifecycles(db) == 0
  assert "lifecycle" not in stored(db, row)
  assert review_state(view(setup)["items"][0]) == "CLEAR"


def test_the_sweep_looks_only_at_runs_that_can_still_move(setup):
  db, row, _ = setup
  assert queries.run_ids(db, queries.OPEN_REVIEW) == [row.id]  # no entry yet: the default reads reviewing
  end_chat(db, "interrupted")
  routes.sweep_review_lifecycles(db)
  assert queries.run_ids(db, queries.OPEN_REVIEW) == []  # stalled: waiting for the owner, not the clock


def test_a_race_with_the_agents_result_never_overwrites_it(setup, monkeypatch):
  """The write is built from the refreshed item: a result saved meanwhile wins, and nothing is stalled."""
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  original = domain._cas_outcome
  def result_lands_first(db_, row_, item_key, outcome, **kwargs):
    # Another request (its own session, so this one's revision is now stale) saves the agent's result.
    monkeypatch.setattr(domain, "_cas_outcome", original)
    other = SessionLocal()
    try:
      domain.save_outcome(other, other.get(models.ContributionReviewRun, row_.id), item_key,
                          {"state": "all_clear", "summary": "Reviewed", "head_sha": SHA})
    finally:
      other.close()
    return original(db_, row_, item_key, outcome, **kwargs)
  monkeypatch.setattr(domain, "_cas_outcome", result_lands_first)
  routes.sweep_review_lifecycles(db)
  item = stored(db, row)
  assert item["state"] == "all_clear" and item["summary"] == "Reviewed"
  assert review_state(view(setup)["items"][0]) == "CLEAR"


def test_the_summary_list_carries_the_vector_without_its_history(setup):
  db, row, principal = setup
  quiet_for_an_hour(db, row)
  item = routes.list_reviews(1, db, principal, view="summary")["runs"][0]["items"][0]
  assert item["state"] == "reviewing"
  assert set(item["lifecycle"]) == {"v", "regions"} and review_state(item) == "STALLED"


# --- a stalled review whose chat works again is running again, and holds its PR ----------------------

def pull_job(db, row):
  return routes._pr_job(db, row.owner_id, ITEM["repo"], ITEM["number"])


def test_a_stalled_review_holds_nothing_until_its_chat_works_again_and_then_holds_its_pr(setup):
  db, row, _ = setup
  end_chat(db, "interrupted")
  assert routes.sweep_review_lifecycles(db) == 1
  assert pull_job(db, row)["state"] == "stalled"
  revive(db)  # the owner reopened the dead review chat, as the stall's own words advise
  assert pull_job(db, row) == {"state": "running", "run_id": row.id}
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("reviewing", "running")
  assert review_state(run["items"][0]) == "REVIEWING"
  vector = stored(db, row)["lifecycle"]  # and the read wrote it, so the stored vector says so too
  assert [entry["type"] for entry in vector["history"]] == ["CHAT_ENDED", "CHAT_WORKING"]
  region = vector["regions"]["review"]
  assert region["state"] == "REVIEWING" and region["deadline"] - region["since"] == 3600
  assert routes.sweep_review_lifecycles(db) == 0


def test_a_second_review_is_refused_again_once_the_stalled_one_works_again(owner):
  db, row, principal, _ = owner
  end_chat(db, "interrupted")
  routes.sweep_review_lifecycles(db)
  assert pull_job(db, row)["state"] == "stalled"  # holds nothing: the owner may start another
  revive(db)
  assert refusal(db, principal) == (409, "An agent is already working on this PR.")


def test_a_chat_that_has_a_next_move_but_no_live_run_is_working_too(setup, monkeypatch):
  """The review chat ends its turn and sleeps until its reviewer helper reports: that is working, not stalled."""
  db, row, _ = setup
  end_chat(db, "completed", ended_minutes_ago=10)
  assert routes.sweep_review_lifecycles(db) == 1 and pull_job(db, row)["state"] == "stalled"
  monkeypatch.setattr(routes, "_has_next_move", lambda *_: True)
  assert pull_job(db, row) == {"state": "running", "run_id": row.id}
  run = view(setup)
  assert (run["state"], run["execution_state"]) == ("reviewing", "completed")
  assert review_state(run["items"][0]) == "REVIEWING"


def test_a_long_review_with_a_helper_still_working_is_not_stalled_by_the_hour(setup, monkeypatch):
  db, row, _ = setup
  aged(db, row, 600)
  end_chat(db, "completed", ended_minutes_ago=30)
  monkeypatch.setattr(routes, "_has_next_move", lambda *_: True)
  assert routes.sweep_review_lifecycles(db) == 0
  assert (view(setup)["state"], stored(db, row)) == ("reviewing", {})


def test_a_stalled_review_the_owner_stopped_stays_stopped_though_its_chat_works(setup):
  db, row, _ = setup
  end_chat(db, "interrupted")
  routes.sweep_review_lifecycles(db)
  revive(db)
  domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
  run = view(setup)  # a stalled item is not the sweep's (it waits for the owner), so the read writes the stop
  assert review_state(run["items"][0]) == "STOPPED" and review_state(stored(db, row)) == "STOPPED"
  assert (run["state"], run["execution_state"]) == ("needs_you", "stopped")


# --- a refused step is never written, and a failing write never fails a read or the batch ------------

def test_a_stalled_review_is_not_written_again_by_the_reads_that_follow(setup):
  """The dead chat was offered to the engine again on every read of a stalled item, and the refusal was kept."""
  db, row, _ = setup
  end_chat(db, "interrupted")
  view(setup)
  first, revision = stored(db, row), row.revision
  for _ in range(3):
    view(setup)
  assert stored(db, row) == first and row.revision == revision
  assert [entry["type"] for entry in first["lifecycle"]["history"]] == ["CHAT_ENDED"]
  assert routes.sweep_review_lifecycles(db) == 0


def broken_write(*_args, **_kwargs):
  from sqlalchemy.exc import OperationalError
  raise OperationalError("UPDATE contribution_review_runs", {}, Exception("database is locked"))


def test_a_write_that_fails_never_fails_the_read_it_rode_on(setup, monkeypatch):
  db, row, _ = setup
  end_chat(db, "interrupted")
  monkeypatch.setattr(domain, "save_effect_outcome", broken_write)
  run = view(setup)  # a read that used to write nothing must not start to fail
  assert (run["state"], run["execution_state"]) == ("needs_you", "interrupted")
  assert review_state(run["items"][0]) == "STALLED"  # computed all the same
  assert stored(db, row) == {}
  monkeypatch.undo()
  assert routes.sweep_review_lifecycles(db) == 1  # and the next tick writes it


def test_one_run_whose_write_fails_does_not_hold_the_others_in_a_sweep(setup, monkeypatch):
  db, row, _ = setup
  db.add(models.ContributionReviewRun(
    id="second", app_id=1, owner_id=row.owner_id, request_id="request-second", mode="review_merge",
    targets_json=row.targets_json, chat_id=row.chat_id, github_actor_id="42", app_nonce=row.app_nonce,
    options_json={}, outcomes_json={}))
  db.commit()
  end_chat(db, "interrupted")
  real, calls = domain.save_effect_outcome, []
  def first_fails(*args, **kwargs):
    calls.append(args[1].id)
    return broken_write() if len(calls) == 1 else real(*args, **kwargs)
  monkeypatch.setattr(domain, "save_effect_outcome", first_fails)
  assert routes.sweep_review_lifecycles(db) == 1  # one written, one logged, nothing raised
  assert len(calls) == 2
  written = [run.id for run in db.query(models.ContributionReviewRun).all()
             if "lifecycle" in (run.outcomes_json or {}).get(KEY, {})]
  assert written == [next(run_id for run_id in ("batch", "second") if run_id != calls[0])]


# --- a damaged stored vector is reseeded from the item's word, as a record's is, and never fails a read ---

def damaged_review_vectors() -> dict[str, dict]:
  good = review_lifecycle._start("review", "REVIEWING", 1000)
  slot = good["regions"]["review"]
  with_slot = lambda **changes: {**good, "regions": {"review": {**slot, **changes}}}  # noqa: E731
  return {
    "history that is a string": {**good, "history": "x"},
    "history of numbers": {**good, "history": [1, 2]},
    "a start that is not a time": with_slot(since="soon"),
    "a deadline that is not a time": with_slot(deadline="soon"),
    "a deadline on a state that has none": with_slot(state="CLEAR", deadline=5),
    "a state the tables do not know": with_slot(state="NOPE"),
    "no regions": {**good, "regions": []},
    "another region as well": {**good, "regions": {**good["regions"], "verdict": {**slot, "state": "NONE"}}},
    "another table version": {**good, "v": 99},
  }


@pytest.mark.parametrize("name", list(damaged_review_vectors()))
def test_a_damaged_review_vector_is_seeded_again_from_the_items_word_and_never_fails_a_read(name):
  damaged = damaged_review_vectors()[name]
  assert not review_lifecycle.readable(damaged)
  item = {"state": "reviewing", "lifecycle": damaged}
  vector = review_lifecycle.review_vector(item, 1000, 2000)
  assert vector == review_lifecycle.review_vector({"state": "reviewing"}, 1000, 2000)
  assert review_lifecycle.state_of(review_lifecycle.advance(vector, {"working": False}, 2000)) == "REVIEWING"


def test_a_vector_this_code_wrote_is_still_read():
  good = review_lifecycle._start("review", "REVIEWING", 1000)
  assert review_lifecycle.readable(good)
  assert review_lifecycle.readable(review_lifecycle._start("verdict", "POSTING", 1000), "verdict")
  assert review_lifecycle.readable({**good, "history": [{"at": 1, "type": "CHAT_ENDED", "source": "platform"}]})


def test_a_damaged_verdict_vector_is_seeded_again_from_the_proposals_word():
  proposal = {"state": "posting", "submitted_at": iso_ago(2), "lifecycle": {"v": 1, "regions": {"verdict": {"state": "POSTING"}}}}
  assert review_lifecycle.verdict_vector(proposal) == review_lifecycle.verdict_vector({k: v for k, v in proposal.items() if k != "lifecycle"})


def test_a_posting_verdict_with_no_time_is_dated_from_when_this_process_loaded(monkeypatch):
  monkeypatch.setattr(domain, "_LOADED_AT", 1_000_000.5)
  assert domain.process_loaded_at() == 1_000_000.5
  assert review_lifecycle.verdict_vector({"state": "posting"})["regions"]["verdict"]["since"] == 1_000_000


# --- verdict: a posting that never got an answer does not stay posting -------------------------------

def save_verdict(db, row, state, submitted_minutes_ago, **extra):
  proposal = {"id": "v1", "state": state, "event": "APPROVE", "head_sha": SHA,
              "submitted_at": iso_ago(submitted_minutes_ago), **extra}
  domain.save_outcome(db, row, KEY, {"state": "all_clear", "summary": "Reviewed", "head_sha": SHA,
                                     "verdict_proposal": proposal})


def test_a_verdict_still_posting_after_five_minutes_is_unconfirmed_and_never_resent(setup):
  db, row, _ = setup
  save_verdict(db, row, "posting", 6)
  assert routes.sweep_review_lifecycles(db) == 1
  proposal = stored(db, row)["verdict_proposal"]
  assert proposal["state"] == "unknown" and proposal["summary"] == verdicts.NO_ANSWER
  assert proposal["lifecycle"]["regions"]["verdict"]["state"] == "UNKNOWN"
  assert view(setup)["items"][0]["state"] == "verdict_unknown"


def test_a_read_alone_times_out_a_verdict_that_never_got_an_answer(setup):
  db, row, _ = setup
  save_verdict(db, row, "posting", 6)
  assert view(setup)["items"][0]["state"] == "verdict_unknown"
  assert stored(db, row)["verdict_proposal"]["state"] == "unknown"


def test_a_verdict_inside_its_five_minutes_is_left_to_the_request_that_sent_it(setup):
  db, row, _ = setup
  save_verdict(db, row, "posting", 4)
  assert routes.sweep_review_lifecycles(db) == 0
  assert stored(db, row)["verdict_proposal"]["state"] == "posting"


@pytest.mark.parametrize("state", ["proposed", "unknown", "not_dispatched", "posted", "discarded"])
def test_no_other_verdict_state_moves_by_the_clock(setup, state):
  db, row, _ = setup
  save_verdict(db, row, state, 600)
  routes.sweep_review_lifecycles(db)
  assert stored(db, row)["verdict_proposal"]["state"] == state


def test_an_unconfirmed_verdict_is_still_settled_by_a_look_at_github_or_dismissed(setup):
  db, row, _ = setup
  save_verdict(db, row, "posting", 6)
  routes.sweep_review_lifecycles(db)
  proposal = stored(db, row)["verdict_proposal"]
  verdicts.settle_observed(db, row, KEY, proposal, {"id": 9, "html_url": "https://example.test/r/9"},
                           complete_scan=True, read_at=datetime.now(UTC).timestamp())
  assert stored(db, row)["verdict_proposal"]["state"] == "posted"


def test_the_owner_can_dismiss_a_verdict_the_clock_made_unconfirmed(setup):
  db, row, _ = setup
  save_verdict(db, row, "posting", 6)
  routes.sweep_review_lifecycles(db)
  verdicts.dismiss(db, row, KEY, "v1")
  assert stored(db, row)["verdict_proposal"]["state"] == "not_dispatched"


def test_a_late_confirmation_of_the_request_that_timed_out_cannot_overwrite_the_unconfirmed_verdict(setup):
  db, row, _ = setup
  save_verdict(db, row, "posting", 6)
  routes.sweep_review_lifecycles(db)
  verdicts._settle(db, row, KEY, "v1", {"state": "posted", "review_id": 5})  # what a stale dispatcher would write
  assert stored(db, row)["verdict_proposal"]["state"] == "unknown"


# --- a review that starts another round is not judged by the vector of the last one -------------------

def second_round(db, row, **kept):
  """What a repair or a base advance saves: the same item, back to `reviewing`, still carrying its old keys."""
  item = {**stored(db, row), "state": "reviewing", "summary": "Fresh independent review required.", **kept}
  domain.save_outcome(db, row, KEY, item)


def test_a_late_result_after_a_stall_is_written_with_the_word_that_reported_it(setup):
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  routes.sweep_review_lifecycles(db)
  revive(db)
  report(setup)
  assert review_state(stored(db, row)) == "STALLED"  # the word moved; the stored vector has not caught up
  assert routes.sweep_review_lifecycles(db) == 1  # the next tick brings it in line, once
  assert review_state(stored(db, row)) == "CLEAR"
  assert routes.sweep_review_lifecycles(db) == 0


def test_a_review_that_reports_after_a_stall_and_then_reviews_again_is_not_stalled_again(setup):
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  routes.sweep_review_lifecycles(db)
  revive(db)
  report(setup)
  routes.sweep_review_lifecycles(db)
  second_round(db, row)
  run = view(setup)  # a read writes the new round, so the sweep after it finds nothing left to do
  assert review_state(run["items"][0]) == "REVIEWING"
  assert (run["state"], run["execution_state"]) == ("reviewing", "running")
  restarted = stored(db, row)["lifecycle"]["regions"]["review"]
  assert restarted["state"] == "REVIEWING" and restarted["deadline"] - restarted["since"] == 3600
  assert stored(db, row)["lifecycle"]["history"][-1]["type"] == "RESEEDED"
  assert routes.sweep_review_lifecycles(db) == 0


def test_the_new_round_gets_its_own_hour_not_the_runs(setup):
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  routes.sweep_review_lifecycles(db)
  revive(db)
  report(setup)
  routes.sweep_review_lifecycles(db)
  second_round(db, row)
  routes.sweep_review_lifecycles(db)
  assert review_state(stored(db, row)) == "REVIEWING"
  assert [entry["type"] for entry in stored(db, row)["lifecycle"]["history"]][-1] == "RESEEDED"


def test_a_new_round_that_never_reports_stalls_after_its_own_hour(setup):
  db, row, _ = setup
  row.mode = "review"
  quiet_for_an_hour(db, row)
  db.commit()
  routes.sweep_review_lifecycles(db)
  revive(db)
  report(setup)
  routes.sweep_review_lifecycles(db)
  second_round(db, row)
  routes.sweep_review_lifecycles(db)
  begun = stored(db, row)["lifecycle"]["regions"]["review"]["since"]
  later = routes.review_settle.now() + 3601
  assert routes.review_settle.sweep(db, lambda r: {}, at=later) == 1
  assert review_state(stored(db, row)) == "STALLED" and begun < later
