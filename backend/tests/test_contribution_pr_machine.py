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
  "mergeable": ["yes", "conflict", "unknown"], "permission": ["READ", "WRITE", "MAINTAIN"],
  "reviews": [[], [{"by": "sam", "state": "APPROVED", "commit": "h2"}],
              [{"by": "sam", "state": "APPROVED", "commit": "h1"}],
              [{"by": "sam", "state": "CHANGES_REQUESTED", "commit": "h1"}],
              [{"by": "qa-bot", "state": "APPROVED", "commit": "h2", "bot": True}]],
  "myReview": [None, {"state": "APPROVED", "commit": "h2"}, {"state": "CHANGES_REQUESTED", "commit": "h1"}],
  "job": [None, {"state": "running"}, {"state": "stalled"}],
  "outbox": [None, {"head": "h2", "state": "drafted"}, {"head": "h2", "state": "unconfirmed"}],
  "readComplete": [True, False],
  "result": [None, {"head": "h2", "outcome": "findings"}, {"head": "h2", "outcome": "clear"}],
}


def sweep():
  for values in itertools.product(*SPACE.values()):
    facts = {"head": "h2", "openThreads": 0, **dict(zip(SPACE, values))}
    yield {**facts, "author": "alex" if facts["role"] == "author" else "sam"}


def enabled_ids(decision):
  return [action["id"] for action in decision["actions"] if action["enabled"]]


def action(decision, action_id):
  return next((item for item in decision["actions"] if item["id"] == action_id), None)


def merge_enabled(decision):
  return bool((action(decision, "merge") or {}).get("enabled"))


def other_approved_this_version(facts):
  return any(review["state"] == "APPROVED" and review["commit"] == facts["head"] and not review.get("bot")
             and review["by"] != facts["author"] for review in facts["reviews"])


def merge_needs_approval_green_and_no_conflicts(decision, facts):
  return not merge_enabled(decision) or (
    other_approved_this_version(facts) and facts["checks"] in ("pass", "none")
    and facts["mergeable"] == "yes" and facts["lifecycle"] == "open")


def merge_waits_for_every_change_request(decision, facts):
  return not merge_enabled(decision) or not any(
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


def unknown_facts_never_enable_merge(decision, facts):
  unknown = (facts["checks"] == "unknown" or facts["mergeable"] == "unknown"
             or facts.get("readComplete") is False or facts["lifecycle"] == "unknown")
  return not unknown or not merge_enabled(decision)


INVARIANTS = {
  "an author never gets a public review action": lambda decision, facts: decision["role"] != "author" or not any(
    item["id"] in ("draft_verdict", "review", "take_over") for item in decision["actions"]),
  "merge needs someone else's approval of this version, green or no checks, no conflicts":
    merge_needs_approval_green_and_no_conflicts,
  "merge waits while anyone (but a bot) still asks for changes": merge_waits_for_every_change_request,
  "no agent job starts while one is running on the PR": no_second_agent_job,
  "no agent job starts while something sent to GitHub is unconfirmed": no_agent_job_races_an_unconfirmed_send,
  "unknown facts never enable merge": unknown_facts_never_enable_merge,
  "take over is for maintainers only": lambda decision, facts: not (action(decision, "take_over") or {}).get("enabled")
    or facts["permission"] in ("MAINTAIN", "ADMIN"),
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
  never_reached_here = {"merged", "closed", "needs_answer", "sending", "author.approved_no_rights"}
  assert set(machine.STATES) - reached <= never_reached_here
  action_ids = {item.id for item in machine.ACTIONS}
  assert all(state["next"] in action_ids for state in machine.STATES.values() if state["next"])


def test_the_platform_refusal_is_the_reason_the_screen_shows():
  facts = next(case["facts"] for case in CASES if case["name"].startswith("Your PR, nobody approved"))
  assert machine.action_blocker(facts, "merge") == "Nobody but the author has approved it yet."
  assert "as the author" in machine.action_blocker({**facts, "role": "author"}, "take_over")


def test_a_reply_alone_never_brings_their_pr_back_for_re_review_but_a_new_commit_does():
  base = next(case["facts"] for case in CASES if case["name"] == "You requested changes at this version")
  assert machine.decide({**base, "openThreads": 4})["state"] == "reviewer.waiting_author"
  assert machine.decide({**base, "head": "h3"})["state"] == "reviewer.re_review"


def test_reviewer_and_author_names_compare_without_case():
  facts = {"role": "author", "author": "Alex", "head": "h2", "lifecycle": "open", "checks": "pass",
           "mergeable": "yes", "permission": "WRITE", "readComplete": True, "openThreads": 0,
           "reviews": [{"by": "alex", "state": "APPROVED", "commit": "h2"}]}
  assert machine.action_blocker(facts, "merge") == "Nobody but the author has approved it yet."


def test_a_stalled_job_never_blocks_starting_again_but_a_running_one_does():
  base = next(case["facts"] for case in CASES if case["name"] == "Their PR, not reviewed by you")
  assert machine.action_blocker({**base, "job": {"state": "stalled"}}, "review") == ""
  assert machine.action_blocker({**base, "job": {"state": "running"}}, "review") == machine.JOB_BUSY
