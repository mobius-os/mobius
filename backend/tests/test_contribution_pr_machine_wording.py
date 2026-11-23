"""The platform's PR machine speaks in the app's words: every owner-visible string of every shared case matches.

The shared cases pin the state, the highlighted action and the allowed actions; they say nothing of the words the owner
reads (the chip, the headline, who it waits on, each action's title, detail and refusal). The expected words in
fixtures/contribution_pr_machine_wording.json come from the APP's machine, per case name.
Regenerate: print per case of the shared case file the app's decide() label, headline, waitingOn and each action's id, title,
detail and reason (pr-machine.js), keyed by case name, as JSON.
"""
import json
from pathlib import Path

import pytest

from app import contribution_pr_machine as machine

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "contribution_pr_machine_cases.json").read_text("utf-8"))["cases"]
WORDS = json.loads((FIXTURES / "contribution_pr_machine_wording.json").read_text("utf-8"))


def test_the_wording_fixture_covers_exactly_the_shared_cases():
  assert sorted(WORDS) == sorted(case["name"] for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_owner_visible_string_is_the_apps(case):
  decision = machine.decide(case["facts"])
  expected = WORDS[case["name"]]
  assert {key: decision[key] for key in ("label", "headline", "waiting_on")} == {
    key: expected[key] for key in ("label", "headline", "waiting_on")}
  assert [{key: item[key] for key in ("id", "title", "detail", "reason")} for item in decision["actions"]] == expected["actions"]
