"""Ask for approval and approval carry-over: the rules of the machine on the review facts that decide them.

Mirrors the app's test/pr-machine-reviews.test.mjs; the shared corpus holds the same rules as cases.
"""
from app import contribution_pr_machine as machine

CHANGES = {"by": "sam", "state": "CHANGES_REQUESTED", "commit": "h1", "bot": False}


def facts(reviews, **extra):
  return {"role": "author", "author": "alex", "head": "h2", "lifecycle": "open", "checks": "pass",
          "mergeable": "yes", "permission": "WRITE", "reviews": reviews, "openThreads": 0, "myReview": None,
          "job": None, "result": None, "outbox": None, "readComplete": True, **extra}


def action(decision, action_id):
  return next(item for item in decision["actions"] if item["id"] == action_id)


def comment(commit, **extra):
  return {"by": "sam", "state": "COMMENTED", "commit": commit, "bot": False, **extra}


def approval(**extra):
  return {"by": "sam", "state": "APPROVED", "commit": "h1", "bot": False, **extra}


def test_a_reviewer_who_looked_again_and_only_commented_is_asked_to_approve_and_merge_stays_off():
  decision = machine.decide(facts([CHANGES, comment("h2")]))
  assert decision["state"] == "author.looked_again"
  assert decision["looked_again"] == ["sam"]
  assert "sam looked again and left a comment, but GitHub still holds their change request" in decision["headline"]
  assert next(item["id"] for item in decision["actions"] if item["highlighted"]) == "ask_approval"
  assert action(decision, "ask_approval")["title"] == "Ask sam to approve"
  assert action(decision, "ask_approval")["enabled"] is True
  assert action(decision, "merge")["enabled"] is False


def test_asking_someone_to_look_again_is_for_people_who_have_not_looked_past_their_old_review():
  looked = machine.decide(facts([CHANGES, comment("h2")]))
  assert looked["rereview_from"] == []
  assert action(looked, "ask_rereview")["reason"] == "They already looked at this version. Ask them to approve."
  not_yet = machine.decide(facts([CHANGES]))
  assert not_yet["rereview_from"] == ["sam"]
  assert action(not_yet, "ask_approval")["reason"] == "Nobody has commented without approving."
  assert action(not_yet, "ask_approval")["title"] == "Ask for approval"
  mixed = machine.decide(facts([CHANGES, comment("h2"), {**approval(), "by": "kim", "sameWork": False}]))
  assert (mixed["looked_again"], mixed["rereview_from"]) == (["sam"], ["kim"])


def test_anyone_who_only_commented_on_this_version_can_be_asked_to_approve_whether_or_not_they_hold_a_request():
  commented = machine.decide(facts([comment("h2")]))
  assert commented["state"] == "author.commented"
  assert (commented["looked_again"], commented["approval_from"]) == ([], ["sam"])
  assert commented["headline"] == "Your PR · sam left a comment but hasn’t approved. Ask for their approval"
  assert action(commented, "ask_approval")["title"] == "Ask sam to approve"
  assert next(item["id"] for item in commented["actions"] if item["highlighted"]) == "ask_approval"
  holding = machine.decide(facts([CHANGES, comment("h2"), comment("h2", by="kim")]))
  assert (holding["state"], holding["looked_again"], holding["approval_from"]) == (
    "author.looked_again", ["sam"], ["sam", "kim"])
  older = machine.decide(facts([approval(sameWork=False), comment("h2")]))
  assert (older["state"], older["rereview_from"]) == ("author.commented", [])
  other_approved = machine.decide(facts([comment("h2"), {**approval(commit="h2"), "by": "kim"}]))
  assert other_approved["state"] == "author.approved"
  owed = machine.decide(facts([comment("h2")], openThreads=1))
  assert owed["state"] == "author.feedback"
  assert action(owed, "ask_approval")["enabled"] is True
  assert machine.decide(facts([comment("h2", bot=True)]))["state"] == "author.waiting_review"


def test_a_comment_never_approves_and_one_from_a_bot_or_from_nobody_holding_a_request_changes_nothing():
  assert action(machine.decide(facts([comment("h2")])), "merge")["enabled"] is False
  assert machine.decide(facts([comment("h2")]))["state"] == "author.commented"
  assert machine.decide(facts([CHANGES, comment("h2", by="kim")]))["state"] == "author.waiting_rereview"
  assert machine.decide(facts([CHANGES, comment("h2", bot=True)]))["state"] == "author.waiting_rereview"
  assert machine.decide(facts([CHANGES, comment("h1")]))["state"] == "author.waiting_rereview"


def test_a_reviewers_name_matches_their_comment_without_case():
  assert machine.decide(facts([CHANGES, comment("h2", by="SAM")]))["looked_again"] == ["sam"]


def test_a_bots_plain_comment_is_conversation_not_a_finding_to_answer():
  quiet = machine.decide(facts([comment("h2", by="qa-bot", bot=True)]))
  assert action(quiet, "respond")["reason"] == "Nothing to respond to."
  blocking = facts([{"by": "qa-bot", "state": "CHANGES_REQUESTED", "commit": "h2", "bot": True}])
  assert action(machine.decide(blocking), "respond")["enabled"] is True


def test_an_approval_of_an_older_commit_carries_over_only_when_its_work_was_proved_the_same():
  carried = machine.decide(facts([approval(sameWork=True)]))
  assert carried["state"] == "author.approved"
  assert (carried["approved_by"], carried["carried_from"], carried["rereview_from"]) == (
    ["sam"], [{"by": "sam", "commit": "h1"}], [])
  assert ("approved by sam, checks green. The lines you added and removed are the same as when they approved. "
          "Merge it") in carried["headline"]
  assert action(carried, "ask_rereview")["reason"] == "Nobody reviewed an older version."
  for same_work in (False, None):
    old = machine.decide(facts([approval(sameWork=same_work)]))
    assert old["state"] == "author.approval_stale" and old["carried_from"] == []
  assert machine.decide(facts([approval()]))["state"] == "author.approval_stale"


def test_an_approval_of_this_very_commit_keeps_its_plain_wording_even_when_another_carried_over():
  decision = machine.decide(facts([approval(sameWork=True), {**approval(commit="h2"), "by": "kim"}]))
  assert decision["approved_by"] == ["sam", "kim"]
  assert "approved by sam, kim at this version" in decision["headline"]


def test_a_carried_approval_still_waits_for_checks_conflicts_and_any_standing_change_request():
  assert machine.decide(facts([approval(sameWork=True)], checks="fail"))["state"] == "author.blocked"
  assert machine.decide(facts([approval(sameWork=True)], checks="pending"))["state"] == "author.approved_checks"
  standing = machine.decide(facts([approval(sameWork=True), {**CHANGES, "by": "kim"}]))
  assert action(standing, "merge")["reason"] == "kim asked for changes and hasn’t approved since."


def test_an_unreadable_pr_never_offers_to_ask_for_approval():
  decision = machine.decide(facts([CHANGES, comment("h2")], readComplete=False))
  assert decision["state"] == "unknown"
  assert action(decision, "ask_approval")["reason"] == "This PR could not be read. Try again."
