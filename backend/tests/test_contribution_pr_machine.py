"""The platform's PR machine answers exactly like Contribute's: one shared corpus, one sweep.

The corpus is byte-identical to the app's test/pr-machine.cases.json, and the
sweep and invariants mirror its test/pr-machine.test.mjs, so a rule changed
in only one evaluator fails here or there.
"""
import itertools
import json
from pathlib import Path

import pytest

from app import contribution_pr_machine as machine

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "contribution_pr_machine_cases.json").read_text("utf-8"))
CASES = CORPUS["cases"]

# Every combination of the facts that change a decision: the app sweeps the same space.
SPACE = {
  "role": ["author", "reviewer"], "lifecycle": ["draft", "open", "unknown"],
  "checks": ["pass", "fail", "pending", "none", "unknown"],
  "mergeable": ["yes", "conflict", "unknown"], "permission": ["READ", "TRIAGE", "WRITE", "MAINTAIN", "unknown"],
  "reviews": [[], [{"by": "sam", "state": "APPROVED", "commit": "h2"}],
              [{"by": "sam", "state": "APPROVED", "commit": "h1"}],
              [{"by": "sam", "state": "CHANGES_REQUESTED", "commit": "h1"}],
              [{"by": "qa-bot", "state": "APPROVED", "commit": "h2", "bot": True}],
              [{"by": "sam", "state": "APPROVED", "commit": "h1", "sameWork": True}],
              [{"by": "sam", "state": "APPROVED", "commit": "h1", "sameWork": False}],
              [{"by": "sam", "state": "CHANGES_REQUESTED", "commit": "h1"},
               {"by": "sam", "state": "COMMENTED", "commit": "h2"}],
              [{"by": "qa-bot", "state": "COMMENTED", "commit": "h2", "bot": True}]],
  "myReview": [None, {"state": "APPROVED", "commit": "h2"}, {"state": "CHANGES_REQUESTED", "commit": "h1"}],
  "job": [None, {"state": "running"}, {"state": "stalled"}],
  "outbox": [None, {"head": "h2", "state": "drafted"}, {"head": "h2", "state": "unconfirmed"}],
  "readComplete": [True, False],
  "result": [None, {"head": "h2", "outcome": "findings"}, {"head": "h2", "outcome": "clear"}],
  "queued": [None, True],
  "reviewRequested": [None, True],
}


def sweep():
  for values in itertools.product(*SPACE.values()):
    facts = {"head": "h2", "openThreads": 0, **dict(zip(SPACE, values))}
    # The fact reader sets a review request only on someone else's PR, so an author with one is not swept: half the cost.
    if facts["role"] == "author" and facts["reviewRequested"]:
      continue
    yield {**facts, "author": "alex" if facts["role"] == "author" else "sam"}


def enabled_ids(decision):
  return [action["id"] for action in decision["actions"] if action["enabled"]]


def action(decision, action_id):
  return next((item for item in decision["actions"] if item["id"] == action_id), None)


# Ready to merge words the standing and the highlighted step, and it is all the agent's unattended merge
# asks; the owner's own Merge is never switched off by it.
def ready_to_merge(facts):
  return machine.not_ready_to_merge(facts) == ""


# What changes the PR or starts an agent on it, and what a quiet readable PR always offers its author or reviewer.
# Take over is left out of OFFERED: it stays for maintainers.
CHANGES_THE_PR = ("merge", "close", "open_for_review", "back_to_draft", "check_my_pr", "respond", "finish_it",
                  "review", "take_over")
READS_ONLY = ("READ", "TRIAGE")
OFFERED = {"author": ("check_my_pr", "respond", "finish_it", "open_for_review", "back_to_draft", "request_review",
                      "merge", "close"),
           "reviewer": ("review", "merge", "close")}


def other_approved_this_version(facts):
  return any(review["state"] == "APPROVED" and (review["commit"] == facts["head"] or review.get("sameWork") is True)
             and not review.get("bot") and review["by"] != facts["author"] for review in facts["reviews"])


def ready_needs_approval_green_and_no_conflicts(decision, facts):
  return not ready_to_merge(facts) or (
    other_approved_this_version(facts) and facts["checks"] in ("pass", "none")
    and facts["mergeable"] == "yes" and facts["lifecycle"] == "open")


def not_ready_while_anyone_asks_for_changes(decision, facts):
  return not ready_to_merge(facts) or not any(
    review["state"] == "CHANGES_REQUESTED" and not review.get("bot") and review["by"] != facts["author"]
    for review in facts["reviews"])


def no_second_agent_job(decision, facts):
  job = facts.get("job")
  return (job is None or job.get("state") == "stalled"
          or all(not item["enabled"] for item in decision["actions"] if item["by"] == "agent"))


def no_agent_job_races_an_unconfirmed_send(decision, facts):
  outbox = facts.get("outbox") or {}
  in_flight = outbox.get("head") == facts["head"] and outbox.get("state") in ("sending", "unconfirmed")
  return not in_flight or all(not item["enabled"] for item in decision["actions"] if item["by"] == "agent")


def unknown_checks_or_conflicts_are_never_ready_to_merge(decision, facts):
  unknown = (facts["checks"] == "unknown" or facts["mergeable"] == "unknown"
             or facts.get("readComplete") is False or facts["lifecycle"] == "unknown")
  return not unknown or not ready_to_merge(facts)


def a_pr_that_cannot_be_read_is_never_acted_on(decision, facts):
  unread = facts.get("readComplete") is False or facts["lifecycle"] == "unknown"
  return not unread or not any(item["id"] in CHANGES_THE_PR and item["enabled"] for item in decision["actions"])


def with_nothing_in_the_way_every_action_of_yours_is_offered(decision, facts):
  job = facts.get("job")
  in_the_way = (facts.get("readComplete") is False or facts["lifecycle"] not in ("open", "draft")
                or (job is not None and job.get("state") != "stalled") or facts.get("outbox")
                or facts.get("queued") is True)
  # Merge alone also waits for write access, and only when GitHub said the account has less.
  offered = [item for item in OFFERED[facts["role"]] if item != "merge" or facts.get("permission") not in READS_ONLY]
  return in_the_way or all((action(decision, action_id) or {}).get("enabled") for action_id in offered)


def merge_is_off_with_the_reason_for_an_account_that_cannot_write(decision, facts):
  merge = action(decision, "merge")
  stopped = (facts["lifecycle"] in ("open", "draft") and facts.get("readComplete") is not False
             and facts.get("queued") is not True and facts.get("permission") in READS_ONLY)
  return not stopped or (not merge["enabled"] and merge["reason"]
                         == f"Merging needs write access to this repository. Your account has {facts['permission'].lower()} access.")


def a_queued_pr_reads_as_in_the_merge_queue(decision, facts):
  held = facts.get("queued") is True and facts["lifecycle"] in ("open", "draft") and facts.get("readComplete") is not False
  return not held or (decision["state"] == "in_merge_queue"
                      and action(decision, "merge")["reason"] == "This pull request is already in the merge queue.")


def a_request_beside_your_review_of_this_version_is_your_turn(decision, facts):
  mine = facts.get("myReview") or {}
  free = (facts["role"] == "reviewer" and facts.get("reviewRequested") is True and mine.get("commit") == facts["head"]
          and facts["lifecycle"] == "open" and facts.get("readComplete") is not False and not facts.get("job")
          and not facts.get("outbox") and facts.get("queued") is not True)
  return not free or (decision["state"] == "reviewer.re_requested" and decision["turn"] == "you"
                      and action(decision, "review")["highlighted"] is True)


def a_review_request_changes_the_words_and_the_turn_only(decision, facts):
  if facts.get("reviewRequested") is not True:
    return True
  rows = lambda made: [(item["id"], item["enabled"], item["reason"]) for item in made["actions"]]
  return rows(machine.decide({**facts, "reviewRequested": None})) == rows(decision)


INVARIANTS = {
  "an author never gets a public review action": lambda decision, facts: decision["role"] != "author" or not any(
    item["id"] in ("draft_verdict", "review", "take_over") for item in decision["actions"]),
  "ready to merge needs someone else's approval of this version (or of the same work), green or no checks, no conflicts":
    ready_needs_approval_green_and_no_conflicts,
  "not ready to merge while anyone (but a bot) still asks for changes": not_ready_while_anyone_asks_for_changes,
  "no agent job starts while one is running on the PR": no_second_agent_job,
  "no agent job starts while something sent to GitHub is unconfirmed": no_agent_job_races_an_unconfirmed_send,
  "unknown checks or conflicts are never ready to merge": unknown_checks_or_conflicts_are_never_ready_to_merge,
  "the Ask actions are offered whenever someone could be asked": lambda decision, facts: (
    facts["role"] != "author" or not decision["askable"] or facts.get("readComplete") is False
    or facts["lifecycle"] not in ("open", "draft")
    or all((action(decision, action_id) or {}).get("enabled") for action_id in ("ask_rereview", "ask_approval"))),
  "request review is the author's whenever the PR can be read and is open, even before anyone has reviewed": lambda decision, facts: (
    (action(decision, "request_review") or {}).get("enabled", False) == (
      facts["role"] == "author" and facts["lifecycle"] in ("open", "draft") and facts.get("readComplete") is not False)),
  "nobody who approved this version can be asked": lambda decision, facts: all(
    not any(name.lower() == person["by"].lower() for name in decision["approved_by"]) for person in decision["askable"]),
  "a PR that cannot be read is never acted on": a_pr_that_cannot_be_read_is_never_acted_on,
  "with nothing in the way, every action of yours is offered, whatever the review standing":
    with_nothing_in_the_way_every_action_of_yours_is_offered,
  "Merge is off, with the reason, for an account GitHub says has no write access; an unknown permission leaves it to the "
  "platform": merge_is_off_with_the_reason_for_an_account_that_cannot_write,
  "a PR GitHub's merge queue holds reads as in the merge queue, and its Merge is off for that reason":
    a_queued_pr_reads_as_in_the_merge_queue,
  "take over is for maintainers only": lambda decision, facts: not (action(decision, "take_over") or {}).get("enabled")
    or facts["permission"] in ("MAINTAIN", "ADMIN"),
  "a request GitHub lists beside your review of this version is your turn to review, unless the PR is a draft or already "
  "has an agent job, a draft of yours or the queue": a_request_beside_your_review_of_this_version_is_your_turn,
  "a review request changes the words and the turn only: what may be done is exactly what it is without it":
    a_review_request_changes_the_words_and_the_turn_only,
  "at most one action is highlighted": lambda decision, facts: sum(item["highlighted"] for item in decision["actions"]) <= 1,
  "a state that highlights an action always offers it": lambda decision, facts: not decision["next"] or any(
    item["id"] == decision["next"] and item["enabled"] for item in decision["actions"]),
}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_shared_case_gives_its_state_highlighted_action_and_allowed_actions(case):
  decision = machine.decide(case["facts"])
  assert decision["state"] == case["state"]
  assert next((item["id"] for item in decision["actions"] if item["highlighted"]), "") == case["next"]
  assert enabled_ids(decision) == case["enabled"]


@pytest.mark.parametrize("name", INVARIANTS)
def test_every_invariant_holds_for_every_shared_case(name):
  holds = INVARIANTS[name]
  for case in CASES:
    assert holds(machine.decide(case["facts"]), case["facts"]), f"{name}: {case['name']}"


def test_every_invariant_holds_across_the_whole_sweep_and_every_state_is_reachable():
  # One pass: each sweep decision is checked against every invariant at once.
  broken = {}
  reached = {machine.decide(case["facts"])["state"] for case in CASES}
  for facts in sweep():
    decision = machine.decide(facts)
    reached.add(decision["state"])
    for name, holds in INVARIANTS.items():
      if name not in broken and not holds(decision, facts):
        broken[name] = facts
  assert broken == {}
  assert {"author.looked_again", "author.approved"} <= reached
  never_reached_here = {"merged", "closed", "needs_answer", "sending", "author.approved_no_rights"}
  assert set(machine.STATES) - reached <= never_reached_here
  action_ids = {item.id for item in machine.ACTIONS}
  assert all(state["next"] in action_ids for state in machine.STATES.values() if state["next"])


def test_the_platform_refusal_is_the_reason_the_screen_shows():
  facts = next(case["facts"] for case in CASES if case["name"].startswith("Your PR, nobody approved"))
  assert machine.action_blocker(facts, "merge") == "", "nobody approving it does not stop the owner's own Merge"
  assert machine.action_blocker({**facts, "job": {"state": "running"}}, "merge") == machine.JOB_BUSY
  assert machine.action_blocker({**facts, "lifecycle": "closed"}, "close") == "This PR is no longer open."
  assert "as the author" in machine.action_blocker({**facts, "role": "author"}, "take_over")


def test_the_agents_own_merge_keeps_the_full_readiness_rule_that_the_owners_tap_no_longer_carries():
  facts = next(case["facts"] for case in CASES if case["name"].startswith("Your PR, nobody approved"))
  assert machine.action_blocker(facts, "merge") == ""
  assert machine.not_ready_to_merge(facts) == "Nobody but the author has approved it yet."


def test_a_reply_alone_never_brings_their_pr_back_for_re_review_but_a_new_commit_does():
  base = next(case["facts"] for case in CASES if case["name"] == "You requested changes at this version")
  assert machine.decide({**base, "openThreads": 4})["state"] == "reviewer.waiting_author"
  assert machine.decide({**base, "head": "h3"})["state"] == "reviewer.re_review"


def test_a_review_request_listed_again_after_your_review_of_this_version_is_your_turn_whatever_your_review_said():
  base = next(case["facts"] for case in CASES if case["name"] == "You requested changes at this version")
  for state in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED"):
    decision = machine.decide({**base, "myReview": {"state": state, "commit": "h2"}, "reviewRequested": True})
    assert [decision["state"], decision["turn"], decision["label"], decision["next"]] == [
      "reviewer.re_requested", "you", "Asked to review again", "review"], state
    assert machine.decide({**base, "myReview": {"state": state, "commit": "h2"}})["state"] != "reviewer.re_requested"
  assert machine.decide({**base, "reviewRequested": True, "lifecycle": "draft"})["state"] == "reviewer.waiting_author"
  assert machine.decide({**base, "reviewRequested": True, "queued": True})["state"] == "in_merge_queue"
  assert machine.decide({**base, "reviewRequested": True, "job": {"state": "running"}})["state"] == "working"
  assert machine.decide({**base, "reviewRequested": False})["state"] == "reviewer.waiting_author"
  own = machine.decide({**base, "role": "author", "author": "alex", "reviewRequested": True})
  assert not own["state"].startswith("reviewer."), "an own PR is never read as a request to you"


def test_reviewer_and_author_names_compare_without_case():
  facts = {"role": "author", "author": "Alex", "head": "h2", "lifecycle": "open", "checks": "pass",
           "mergeable": "yes", "permission": "WRITE", "readComplete": True, "openThreads": 0,
           "reviews": [{"by": "alex", "state": "APPROVED", "commit": "h2"}]}
  assert machine.decide(facts)["state"] == "author.waiting_review", "the author's own approval is no approval"
  assert machine.decide(facts)["approved_by"] == []


def test_a_stalled_job_never_blocks_starting_again_but_a_running_one_does():
  base = next(case["facts"] for case in CASES if case["name"] == "Their PR, not reviewed by you")
  assert machine.action_blocker({**base, "job": {"state": "stalled"}}, "review") == ""
  assert machine.action_blocker({**base, "job": {"state": "running"}}, "review") == machine.JOB_BUSY


def test_a_pr_in_the_merge_queue_waits_on_github_and_is_nobodys_step():
  facts = next(case["facts"] for case in CASES if case["name"] == "Their PR is in GitHub’s merge queue")
  decision = machine.decide(facts)
  assert (decision["state"], decision["turn"], decision["waiting_on"]) == ("in_merge_queue", "other", "GitHub")
  assert machine.action_blocker(facts, "merge") == machine.IN_MERGE_QUEUE
  assert machine.decide({**facts, "role": "author", "author": "alex"})["waiting_on"] == "GitHub"


def test_your_own_approval_of_the_same_work_says_it_still_holds_and_a_change_request_of_yours_never_does():
  base = next(case["facts"] for case in CASES if case["name"].startswith("Your own approval of an older commit"))
  assert machine.decide(base)["headline"].startswith("Their PR · your approval still holds, checks green.")
  unproved = {**base, "myReview": {**base["myReview"], "sameWork": None}}
  assert machine.decide(unproved)["state"] == "reviewer.re_review", "an approval whose work was not proved is of an older version"
  request = {**base, "myReview": {"state": "CHANGES_REQUESTED", "commit": "h1", "sameWork": True}}
  assert machine.decide(request)["state"] == "reviewer.re_review", "only an approval carries over"
