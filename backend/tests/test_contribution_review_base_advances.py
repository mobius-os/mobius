"""Scoped takeover follows a moving target branch without bypassing review.

The owner's review-fix-merge grant includes bringing a PR up to date with its
exact current target branch. A moved base is a new exact (head, base) version
that needs a fresh independent review; pinned review-merge stays pinned; Stop and receipts still win.
"""


import asyncio


import hashlib


import pytest


from fastapi import HTTPException


from app import contribution_review_runs as domain, contribution_review_repairs as repairs, models


from app.chat_writer import create_chat


from app.deps import Principal


from app.routes import contribution_reviews as routes


from tests.test_contribution_review_runs import setup, ITEM, SHA, BASE, REPO, PULL, CHECKS, report  # noqa: F401


from tests.test_contribution_review_workflows import takeover, publish_body, checkout_body


NEW_BASE = "e" * 40


NEXT_BASE = "f" * 40


NEW_HEAD = "c" * 40


REAL_ASSERT_CURRENT_BASE = domain.assert_current_base


def moving_base(monkeypatch, live, *, status="ahead"):
  """GitHub reads: the live target tip and a compare from the reviewed base."""
  state = {"live": live, "status": status, "compares": []}
  monkeypatch.setattr(domain, "assert_current_base", REAL_ASSERT_CURRENT_BASE)
  monkeypatch.setattr(domain, "base_head", lambda *a: state["live"])
  def read(_gh, _cwd, endpoint):
    if "/compare/" in endpoint:
      old, new = endpoint.rsplit("/", 1)[1].split("...")
      state["compares"].append((old, new))
      return {"status": state["status"], "base_commit": {"sha": old},
              "merge_base_commit": {"sha": old if state["status"] == "ahead" else "0" * 40}}
    return {"id": 42}
  monkeypatch.setattr(domain, "read", read)
  return state


def receipt(head=SHA, base=BASE, receipt_id="independent-clear"):
  return {"id": receipt_id, "head_sha": head, "base_sha": base, "state": "all_clear", "tests_passed": True}


def clear_takeover(setup, monkeypatch, receipts=None):
  db, row, principal = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "reviewing",
    "independent_reviews": receipts or [receipt()]})
  return db, row, principal


def clear(setup, receipt_id="independent-clear", **changes):
  return report(setup, tests_passed=True, reviewed_base_sha=changes.pop("base", BASE),
                independent_receipt_id=receipt_id, **changes)


def item(result):
  return result["run"]["items"][0]


def test_base_advance_under_unchanged_head_requires_fresh_independent_review(setup, monkeypatch):
  db, row, _ = clear_takeover(setup, monkeypatch)
  live = moving_base(monkeypatch, NEW_BASE)
  merges = []
  monkeypatch.setattr(domain, "perform_merge", lambda *a: merges.append(a[2]["base_sha"]) or {"merged": True, "sha": "landed"})
  advanced = item(clear(setup))
  assert advanced["state"] == "reviewing" and advanced["current_base_sha"] == NEW_BASE
  assert advanced["next_step"] == {"action": "independent_review", "head_sha": SHA, "base_sha": NEW_BASE}
  assert "independent_receipt_id" not in advanced and "reviewed_base_sha" not in advanced
  assert live["compares"] == [(BASE, NEW_BASE)]
  assert merges == []
  # The receipt for the old base cannot authorize merge of the new version.
  with pytest.raises(HTTPException) as stale:
    clear(setup, base=NEW_BASE)
  assert stale.value.status_code == 422
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "independent_reviews": [
    *previous["independent_reviews"], receipt(base=NEW_BASE, receipt_id="fresh")]})
  assert item(clear(setup, "fresh", base=NEW_BASE))["state"] == "merged"
  assert merges == [NEW_BASE]


def test_repeated_advances_chain_from_the_receipted_base_and_stay_bounded(setup, monkeypatch):
  db, row, _ = clear_takeover(setup, monkeypatch)
  live = moving_base(monkeypatch, NEW_BASE)
  clear(setup)
  live["live"] = NEXT_BASE
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, "independent_reviews": [
    *previous["independent_reviews"], receipt(base=NEW_BASE, receipt_id="second")]})
  advanced = item(clear(setup, "second", base=NEW_BASE))
  assert advanced["current_base_sha"] == NEXT_BASE
  assert advanced["base_advance"]["from_base_sha"] == BASE
  assert advanced["base_advance"]["previous_base_sha"] == NEW_BASE
  assert advanced["base_advance"]["count"] == 2
  assert live["compares"] == [(BASE, NEW_BASE), (NEW_BASE, NEXT_BASE)]


def test_pinned_review_merge_never_follows_a_moved_base(setup, monkeypatch):
  moving_base(monkeypatch, NEW_BASE)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("stale pinned base"))
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 409
  assert setup[1].outcomes_json == {}


@pytest.mark.parametrize("status", ["diverged", "behind", "identical"])
def test_rewritten_target_branch_is_an_owner_decision_not_an_advance(setup, monkeypatch, status):
  clear_takeover(setup, monkeypatch)
  moving_base(monkeypatch, NEW_BASE, status=status)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("rewritten base"))
  saved = item(clear(setup))
  assert saved["state"] == "needs_you" and "rewritten" in saved["summary"]
  assert saved["current_base_sha"] == BASE and "base_advance" not in saved


@pytest.mark.parametrize("unsettled", [
  {"repair_attempts": [{"from_sha": SHA, "head_sha": NEW_HEAD, "state": "push_unknown"}]},
])
def test_unsettled_public_attempts_block_base_advance(setup, monkeypatch, unsettled):
  db, row, _ = clear_takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {**row.outcomes_json[domain.key(ITEM)], **unsettled})
  moving_base(monkeypatch, NEW_BASE)
  saved = item(clear(setup))
  assert saved["current_base_sha"] == BASE and "base_advance" not in saved
  assert saved["state"] in {"needs_you", "ready_unknown"}


def test_attempted_merge_is_reconciled_never_advanced(setup, monkeypatch):
  db, row, _ = clear_takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {**row.outcomes_json[domain.key(ITEM)],
    "state": "merge_unknown", "merge_attempted": True, "head_sha": SHA})
  moving_base(monkeypatch, NEW_BASE)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("never repeat"))
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "mergeQueueEntry": None})
  saved = item(clear(setup))
  assert saved["merge_attempted"] is True and "base_advance" not in saved


def test_stop_during_base_observation_records_nothing(setup, monkeypatch):
  db, row, principal = clear_takeover(setup, monkeypatch)
  before = dict(row.outcomes_json[domain.key(ITEM)])
  moving_base(monkeypatch, NEW_BASE)
  reads = []
  def stop_while_reading(*a):
    if reads:
      db.get(models.ChatRun, principal.run_id).status = "stopped"
      db.commit()
    reads.append(1)
    return NEW_BASE
  monkeypatch.setattr(domain, "base_head", stop_while_reading)
  with pytest.raises(HTTPException) as error:
    clear(setup)
  assert error.value.status_code == 409
  db.refresh(row)
  assert {k: v for k, v in row.outcomes_json[domain.key(ITEM)].items() if k != "steps"} == {
    k: v for k, v in before.items() if k != "steps"}


def test_reviewer_admission_adopts_the_advanced_base_before_freezing_the_child(setup, monkeypatch):
  from app import delegations
  db, row, principal = takeover(setup, monkeypatch)
  moving_base(monkeypatch, NEW_BASE)
  prompts = []
  async def fake_start(db, child, prompt, **kwargs):
    prompts.append(prompt)
    return True
  monkeypatch.setattr(delegations, "ensure_delegation_started", fake_start)
  result = asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  step = item(result)["reviewer_steps"][-1]
  assert (step["head_sha"], step["base_sha"]) == (SHA, NEW_BASE)
  assert f"against base {NEW_BASE}" in prompts[0]


def test_independent_evidence_for_the_old_base_is_refused_after_advance(setup, monkeypatch):
  db, row, parent = takeover(setup, monkeypatch)
  child_chat = create_chat(id="old-base-child", title="Independent")
  db.add(child_chat)
  db.add(models.ChatRun(id="old-base-run", chat_id=child_chat.id, status="running"))
  target = row.targets_json[0]
  child = models.Delegation(id="old-base-review", parent_chat_id=row.chat_id,
    parent_root_run_id=parent.run_id, task_key="old-base", child_chat_id=child_chat.id,
    provider="codex", model="gpt-5", effort="xhigh", scope="write", cwd="/tmp",
    prompt_sha256=hashlib.sha256(domain.independent_brief(row, target).encode()).hexdigest())
  db.add(child)
  db.commit()
  from tests.test_contribution_review_workflows import register_reviewer
  register_reviewer(db, row, child, target)
  moving_base(monkeypatch, NEW_BASE)
  clear_takeover_outcome = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**clear_takeover_outcome, "independent_reviews": [receipt()]})
  clear(setup)
  reviewer = Principal(owner=parent.owner, app_id=None, chat_id=child_chat.id,
                       run_id="old-base-run", delegation_id=child.id)
  body = routes.ReviewOutcome(**ITEM, state="all_clear", summary="Old base", scope=sorted(routes.SCOPE),
                              tests="passed", tests_passed=True, reviewed_base_sha=BASE)
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.independent_review(1, row.id, body, db, reviewer))
  assert error.value.status_code in {403, 409}


def test_target_move_during_repair_validation_publishes_nothing(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  snapshot = {"checkout": "/fake/server-repair", "initial_head_sha": SHA, "allowed_files": ["owned.py"]}
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: snapshot)
  asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  tips = iter([NEW_BASE, NEXT_BASE])
  monkeypatch.setattr(domain, "base_head", lambda *a: next(tips))
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **kw: {**snapshot, "head_sha": NEW_HEAD,
    "files": ["owned.py"], "diff_sha256": "d" * 64, "merged_base_sha": NEW_BASE})
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("moved base"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")


ROLLUP = lambda state: {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]}}  # noqa: E731


@pytest.mark.parametrize("status", ["diverged", "behind", "identical", None])
def test_incoherent_compare_evidence_never_proves_an_advance(setup, monkeypatch, status):
  """Both commit fields can look right while GitHub's own verdict says otherwise.

  Only an explicit ``ahead`` proves a fast-forward of the reviewed base; a
  contradictory or partial answer is treated like a rewrite, never an advance.
  """
  clear_takeover(setup, monkeypatch)
  moving_base(monkeypatch, NEW_BASE)
  def read(_gh, _cwd, endpoint):
    if "/compare/" in endpoint:
      return {**({"status": status} if status else {}),
              "base_commit": {"sha": BASE}, "merge_base_commit": {"sha": BASE}}
    return {"id": 42}
  monkeypatch.setattr(domain, "read", read)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("unproven base"))
  saved = item(clear(setup))
  assert saved["state"] == "needs_you" and "rewritten" in saved["summary"]
  assert saved["current_base_sha"] == BASE and "base_advance" not in saved


@pytest.mark.parametrize("via", ["outcomes", "reviewers"])
def test_a_base_advance_drops_every_verdict_recorded_for_the_old_base(setup, monkeypatch, via):
  """A waiting item holds the old base's evidence; none of it may survive the move."""
  from app import delegations
  db, row, principal = clear_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, **ROLLUP("PENDING"), "mergeStateStatus": "BLOCKED"})
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("stale verdict"))
  waiting = item(clear(setup))
  stale = ("reviewed_base_sha", "independent_receipt_id", "scope", "tests", "tests_passed")
  assert waiting["state"] == "needs_you" and all(field in waiting for field in stale)
  moving_base(monkeypatch, NEW_BASE)
  if via == "outcomes":
    advanced = item(clear(setup))
  else:
    async def fake_start(db, child, prompt, **kwargs):
      return True
    monkeypatch.setattr(delegations, "ensure_delegation_started", fake_start)
    advanced = item(asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal)))
  assert advanced["state"] == "reviewing" and advanced["current_base_sha"] == NEW_BASE
  assert [field for field in stale if field in advanced] == []
  db.refresh(row)
  assert [field for field in stale if field in row.outcomes_json[domain.key(ITEM)]] == []



def test_independent_reviewer_belongs_to_the_parent_runs_goal(setup, monkeypatch):
  from app import delegations
  db, row, principal = takeover(setup, monkeypatch)
  db.add(models.ChatGoal(id="review-goal", chat_id=row.chat_id, objective="Merge it", status="open"))
  db.get(models.ChatRun, principal.run_id).goal_id = "review-goal"
  db.commit()
  async def fake_start(db, child, prompt, **kwargs):
    return True
  monkeypatch.setattr(delegations, "ensure_delegation_started", fake_start)
  started = asyncio.run(routes.start_independent_reviewer(1, row.id, routes.PullIdentity(**ITEM), db, principal))
  assert db.get(models.Delegation, started["delegation"]["id"]).goal_id == "review-goal"


def test_model_drift_flags_unfinished_runs_but_not_completed_ones(setup):
  db, row, _ = setup
  row.mode = "review"
  row.options_json = {"provider": "codex", "model": "gpt-5", "reasoning_effort": "xhigh"}
  chat = db.get(models.Chat, row.chat_id)
  chat.provider = "codex"
  chat.agent_settings_json = {"model": "gpt-5", "effort": "xhigh"}
  db.commit()
  assert routes._run_view(db, row)["state"] != "complete"
  chat.agent_settings_json = {"model": "gpt-5", "effort": "low"}
  db.commit()
  assert "model choice" in routes._run_view(db, row)["summary"]
  chat.agent_settings_json = {"model": "gpt-5", "effort": "xhigh"}
  db.commit()
  assert report(setup)["run"]["state"] == "complete"
  chat.agent_settings_json = {"model": "gpt-5", "effort": "low"}
  db.commit()
  assert routes._run_view(db, row)["state"] == "complete"
