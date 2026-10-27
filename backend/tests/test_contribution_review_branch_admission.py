"""Branch exclusion spans ordinary merges, takeovers and immutable legacy grants."""
import asyncio
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import agent_work_claims, contribution_review_runs as domain, models
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_effects import DispatchResult
from app.database import SessionLocal
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, CHECKS, report
from tests.test_contribution_review_workflows import configure_repair, overlapping_grant, publish_body, repairs, NEW


def inspected_target(monkeypatch, *, mode="review_merge", number=7, head_repo_id=1,
                     head_ref="topic", head_repo="example/project", fork_push=True, head_changes=None):
  item = {**ITEM, "number": number, "base_ref": "main" if number == 7 else "release"}
  head = {"sha": SHA, "repo": {"id": head_repo_id, "full_name": head_repo}, "ref": head_ref,
          **(head_changes or {})}
  pull = {**PULL, "node_id": f"PR_{number}", "head": head, "base": {"ref": item["base_ref"]}}
  responses = {"repos/example/project": REPO,
    f"repos/example/project/pulls/{number}": pull,
    f"repos/{head_repo}": {"id": head_repo_id, "full_name": head_repo, "permissions": {"push": fork_push}}}
  if head_repo == "example/project":
    responses["repos/example/project"] = REPO
  with monkeypatch.context() as patch:
    patch.setattr(domain, "read", lambda gh, cwd, endpoint: responses[endpoint])
    patch.setattr(domain, "base_head", lambda *a: BASE)
    return domain.inspect_target(None, "/tmp", item, mode)


@pytest.mark.parametrize("mode", ["review_merge", "review_fix_merge"])
def test_every_branch_mutation_selection_freezes_usable_head_identity(monkeypatch, mode):
  target = inspected_target(monkeypatch, mode=mode)
  assert (target["head_repo_id"], target["head_ref"], target["head_repo"]) == (1, "topic", "example/project")


def test_normal_fork_merge_freezes_identity_without_requiring_fork_push_rights(setup, monkeypatch):
  target = inspected_target(monkeypatch, head_repo_id=2, head_repo="contributor/project", fork_push=False)
  assert target["head_repo_id"] == 2 and target["head_ref"] == "topic"
  with pytest.raises(HTTPException) as error:
    inspected_target(monkeypatch, mode="review_fix_merge", head_repo_id=2,
      head_repo="contributor/project", fork_push=False)
  assert error.value.status_code == 403
  db, row, _ = setup
  row.targets_json = [target]
  db.commit()
  monkeypatch.setattr(domain, "perform_merge", lambda *a: {"merged": True, "sha": "landed"})
  assert report(setup)["run"]["items"][0]["state"] == "merged"


@pytest.mark.parametrize("state", ["merge_unknown", "queued"])
def test_normal_sibling_merge_blocks_prepared_takeover_branch_push(setup, monkeypatch, state):
  db, row, principal = configure_repair(setup, monkeypatch)
  peer = overlapping_grant(db, row, number=8)
  peer.mode = "review_merge"
  peer.targets_json = [inspected_target(monkeypatch, number=8)]
  db.commit()
  domain.save_outcome(db, peer, domain.key(peer.targets_json[0]),
    {"state": state, "head_sha": SHA, "merge_attempted": True})
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **k: pytest.fail("PUBLIC_PUSH"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409


@pytest.mark.parametrize("state", ["pushing", "push_unknown"])
def test_sibling_takeover_push_blocks_normal_merge_of_lagging_predecessor(setup, monkeypatch, state):
  db, row, principal = setup
  row.options_json = {}
  row.targets_json = [inspected_target(monkeypatch)]
  db.commit()
  peer = overlapping_grant(db, row, number=8)
  peer.mode = "review_fix_merge"
  peer.targets_json = [inspected_target(monkeypatch, mode="review_fix_merge", number=8)]
  db.commit()
  domain.save_outcome(db, peer, domain.key(peer.targets_json[0]), {"state": "needs_you",
    "repair_attempts": [{"state": state, "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("PUBLIC_MERGE"))
  with pytest.raises(HTTPException) as error:
    report(setup)
  assert error.value.status_code == 409


def mixed_grants(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  peer = overlapping_grant(db, row, number=8)
  peer.mode = "review_merge"
  peer.targets_json = [inspected_target(monkeypatch, number=8)]
  db.commit()
  return db, row, peer, principal


def pending_outcome(action, state=None):
  if action == "merge":
    return {"state": state or "merge_unknown", "head_sha": SHA, "merge_attempted": True}
  return {"state": "needs_you", "repair_attempts": [{"state": state or "push_unknown",
    "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]}


def arm(db, row, action, principal):
  target = row.targets_json[0]
  if action == "merge":
    return domain.arm_merge(db, row, target, {"head_sha": SHA}, principal)
  return domain.arm_repair(db, row, target, {}, {"id": "push", "state": "pushing",
    "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}, principal)


@pytest.mark.parametrize("action", ["merge", "repair"])
def test_mixed_mode_same_branch_is_rechecked_after_claim_in_another_session(setup, monkeypatch, action):
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  active, peer = (normal, takeover) if action == "merge" else (takeover, normal)
  real_claim = agent_work_claims.claim_work
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as writer:
      saved = writer.get(models.ContributionReviewRun, peer.id)
      domain.save_outcome(writer, saved, domain.key(saved.targets_json[0]),
        pending_outcome("repair" if action == "merge" else "merge"))
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  with pytest.raises(HTTPException) as error:
    arm(db, active, action, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(active)
  item = active.outcomes_json.get(domain.key(active.targets_json[0]), {})
  assert not item.get("merge_attempted") and not item.get("repair_attempts")


def test_mixed_mode_distinct_claims_share_cross_session_branch_admission(setup, monkeypatch):
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  real_claim = agent_work_claims.claim_work
  barrier = Barrier(2)
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    barrier.wait(timeout=10)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  row_ids = {"merge": normal.id, "repair": takeover.id}
  def reserve(action):
    with SessionLocal() as session:
      row = session.get(models.ContributionReviewRun, row_ids[action])
      try:
        assert arm(session, row, action, principal) is None
        return "admitted"
      except HTTPException as exc:
        session.rollback()
        assert exc.status_code == 409
        return "blocked"
  with ThreadPoolExecutor(max_workers=2) as pool:
    assert sorted(pool.map(reserve, ["merge", "repair"])) == ["admitted", "blocked"]


@pytest.mark.parametrize("missing", ["head_repo_id", "head_ref"])
@pytest.mark.parametrize("pending", ["merge_unknown", "queued", "pushing", "push_unknown"])
def test_legacy_unresolved_identity_cannot_be_assumed_disjoint_or_rewritten(setup, monkeypatch, missing, pending):
  db, row, peer, principal = mixed_grants(setup, monkeypatch)
  legacy = {**peer.targets_json[0], "head_ref": "otherwise-disjoint"}
  legacy.pop(missing, None)
  peer.targets_json = [legacy]
  peer.mode = "review_merge" if pending in {"merge_unknown", "queued"} else "review_fix_merge"
  db.commit()
  outcome = pending_outcome("merge" if peer.mode == "review_merge" else "repair", pending)
  domain.save_outcome(db, peer, domain.key(legacy), outcome)
  selection, receipt = deepcopy(peer.targets_json), deepcopy(peer.outcomes_json)
  with pytest.raises(HTTPException) as error:
    arm(db, row, "repair", principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(peer)
  assert peer.targets_json == selection and peer.outcomes_json == receipt


@pytest.mark.parametrize("missing", ["head_repo_id", "head_ref"])
def test_legacy_new_action_identity_cannot_prove_disjoint_from_known_pending_branch(setup, monkeypatch, missing):
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  normal.targets_json = [{k: v for k, v in normal.targets_json[0].items() if k != missing}]
  db.commit()
  domain.save_outcome(db, takeover, domain.key(takeover.targets_json[0]), pending_outcome("repair"))
  with pytest.raises(HTTPException) as error:
    arm(db, normal, "merge", principal)
  assert error.value.status_code == 409


@pytest.mark.parametrize("terminal", ["merged", "all_clear", "needs_you"])
def test_identityless_settled_history_does_not_block_other_pr_grants(setup, monkeypatch, terminal):
  db, row, peer, principal = mixed_grants(setup, monkeypatch)
  peer.targets_json = [{**TARGET, "number": 8}]
  db.commit()
  domain.save_outcome(db, peer, domain.key(peer.targets_json[0]), {"state": terminal,
    "head_sha": SHA, "merge_attempted": terminal == "merged"})
  assert arm(db, row, "repair", principal) is None


@pytest.mark.parametrize("disjoint", ["head_ref", "head_repo_id"])
def test_fully_identified_pending_branch_can_be_proven_disjoint(setup, monkeypatch, disjoint):
  db, row, peer, principal = mixed_grants(setup, monkeypatch)
  peer.targets_json = [{**peer.targets_json[0], disjoint: "other-topic" if disjoint == "head_ref" else 2}]
  db.commit()
  domain.save_outcome(db, peer, domain.key(peer.targets_json[0]), pending_outcome("merge"))
  assert arm(db, row, "repair", principal) is None


@pytest.mark.parametrize("head", [
  {"repo": {"id": None, "full_name": "example/project"}},
  {"repo": {"id": True, "full_name": "example/project"}},
  {"repo": {"id": "1", "full_name": "example/project"}},
  {"repo": {"id": 0, "full_name": "example/project"}},
  {"ref": None}, {"ref": ""}, {"ref": " "},
])
def test_new_normal_merge_grant_refuses_unusable_head_identity(monkeypatch, head):
  with pytest.raises(HTTPException) as error:
    inspected_target(monkeypatch, head_changes=head)
  assert error.value.status_code == 409


@pytest.mark.parametrize("field,value", [("ref", "other-topic"),
  ("repo", {"id": 2, "full_name": "another/project"})])
def test_normal_merge_frozen_branch_identity_survives_same_sha_live_retarget(monkeypatch, field, value):
  target = inspected_target(monkeypatch)
  live = {**PULL, "head": {"sha": SHA, "repo": {"id": 1, "full_name": "example/project"},
    "ref": "topic", field: value}}
  with monkeypatch.context() as patch:
    patch.setattr(domain, "read", lambda gh, cwd, endpoint: REPO if endpoint == "repos/example/project" else live)
    with pytest.raises(HTTPException) as error:
      domain.current_pull(None, "/tmp", target)
  assert error.value.status_code == 409


def test_stopped_observation_cannot_resolve_or_reinterpret_identityless_pending_grant(setup, monkeypatch):
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  normal.targets_json = [{**TARGET, "number": 8, "pr_id": "PR_8", "base_ref": "release"}]
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  domain.save_outcome(db, normal, domain.key(normal.targets_json[0]), pending_outcome("merge"))
  selection = deepcopy(normal.targets_json)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {"headRefOid": SHA})
  observed = asyncio.run(routes.observe_review(1, normal.id, db, Principal(owner=principal.owner, app_id=None)))
  assert observed["run"]["items"][0]["state"] == "merge_unknown"
  assert normal.targets_json == selection
  with SessionLocal() as restarted:
    saved = restarted.get(models.ContributionReviewRun, takeover.id)
    with pytest.raises(HTTPException) as error:
      domain.require_public_transition_clear(restarted, saved, saved.targets_json[0])
    assert error.value.status_code == 409


@pytest.mark.parametrize("action", ["merge", "repair"])
def test_refused_admission_releases_its_claim_for_another_chat(setup, monkeypatch, action):
  """A 409 after claim acquisition must not leave held work with no attempt."""
  from tests.test_contribution_review_runs import create_chat
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  active, peer = (normal, takeover) if action == "merge" else (takeover, normal)
  domain.save_outcome(db, peer, domain.key(peer.targets_json[0]),
    pending_outcome("repair" if action == "merge" else "merge"))
  with pytest.raises(HTTPException) as error:
    arm(db, active, action, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(active)
  assert not active.outcomes_json.get(domain.key(active.targets_json[0]), {}).get("repair_attempts")
  target = active.targets_json[0]
  work_key = (domain.merge_work_key(target) if action == "merge" else
    f"github:{target['head_repo'].lower()}:branch:{target['head_ref']}:{target['head_sha']}:repair")
  db.add(create_chat(id="later-chat", title="Later owner"))
  db.add(models.ChatRun(id="later-run", chat_id="later-chat", status="running"))
  db.commit()
  claim = agent_work_claims.claim_work(db, owner_id=active.owner_id, chat_id="later-chat",
    run_id="later-run", work_key=work_key, summary="Retry after the blocker settles")
  assert claim["state"] == "claimed"


@pytest.mark.parametrize("action", ["merge", "repair"])
@pytest.mark.parametrize("held", [False, True], ids=["newly_acquired", "already_owned"])
def test_same_key_early_return_releases_only_a_newly_acquired_claim(setup, monkeypatch, action, held):
  """An earlier saved attempt owns the effect; this admission starts none."""
  from tests.test_contribution_review_runs import create_chat
  db, row, principal = configure_repair(setup, monkeypatch)
  target = row.targets_json[0]
  work_key = (domain.merge_work_key(target) if action == "merge" else
    f"github:{target['head_repo'].lower()}:branch:{target['head_ref']}:{target['head_sha']}:repair")
  saved = pending_outcome(action)
  if action == "repair":
    saved["repair_attempts"][0]["work_key"] = work_key
  domain.save_outcome(db, row, domain.key(target), saved)
  if held:
    # This chat's claim backs the saved attempt and must survive.
    agent_work_claims.claim_work(db, owner_id=row.owner_id, chat_id=row.chat_id,
      run_id=principal.run_id, work_key=work_key, summary="Backs the saved attempt")
  assert arm(db, row, action, principal)
  db.add(create_chat(id="later-chat", title="Later owner"))
  db.add(models.ChatRun(id="later-run", chat_id="later-chat", status="running"))
  db.commit()
  claim = agent_work_claims.claim_work(db, owner_id=row.owner_id, chat_id="later-chat",
    run_id="later-run", work_key=work_key, summary="Another chat looks at the same work")
  assert claim["state"] == ("held_by_peer" if held else "claimed")


# The real reads, before the setup fixture stubs current_pull.
REAL_CURRENT_PULL = domain.current_pull
LATER = "e" * 40
BRANCH = {"repo": {"id": 1, "full_name": "example/project"}, "ref": "topic"}


def serve(monkeypatch, responses):
  monkeypatch.setattr(domain, "current_pull", REAL_CURRENT_PULL)
  def read(gh, cwd, endpoint):
    if endpoint not in responses:
      raise HTTPException(404, "Not Found")
    return responses[endpoint]
  monkeypatch.setattr(domain, "read", read)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: pytest.fail("a moved head has no exact queue entry"))


def observe_item(db, row, principal):
  return asyncio.run(routes.observe_review(1, row.id, db,
    Principal(owner=principal.owner, app_id=None)))["run"]["items"][0]


def fence_clear(row):
  with SessionLocal() as fresh:
    saved = fresh.get(models.ContributionReviewRun, row.id)
    try:
      domain.require_public_transition_clear(fresh, saved, saved.targets_json[0])
    except HTTPException as error:
      assert error.status_code == 409
      return False
    return True


def moved_pull(**changes):
  return {**PULL, "node_id": "PR_8", "base": {"ref": "release"}, "state": "open", "merged": False,
          "head": {"sha": LATER, **BRANCH}, **changes}


def iso(seconds):
  return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


# Armed long enough ago that no request issued for it can still be processed.
CLOSED = iso(time.time() - domain.MERGE_DISPATCH_WINDOW_SECONDS - 60)


def attempted_merge(setup, monkeypatch, state, armed_at=CLOSED):
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  attempt = {**pending_outcome("merge", state), "summary": "GitHub did not confirm the merge."}
  if armed_at is not None:
    attempt["merge_armed_at"] = armed_at
  if state == "queued":
    attempt["queue_entry_id"] = "MQE_1"
  domain.save_outcome(db, normal, domain.key(normal.targets_json[0]), attempt)
  assert not fence_clear(takeover) and not fence_clear(normal)
  return db, takeover, normal, principal, attempt


@pytest.mark.parametrize("state", ["merge_unknown", "queued", "merging"])
def test_head_moved_after_a_pinned_attempt_settles_it_without_rearming(setup, monkeypatch, state):
  """The queue dropped the PR, the author pushed: the old attempt cannot land."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, state)
  serve(monkeypatch, {"repos/example/project": REPO, "repos/example/project/pulls/8": moved_pull()})
  item = observe_item(db, normal, principal)
  assert item["state"] == "needs_you"
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["head_moved"] == {"attempted_head_sha": SHA, "attempt_state": state, "head_sha": LATER}
  # Audit history stays: the attempted head, its receipt and the attempt flag.
  assert saved["head_sha"] == SHA and saved["merge_attempted"] is True
  assert saved.get("queue_entry_id") == attempt.get("queue_entry_id")
  assert fence_clear(takeover) and fence_clear(normal)
  # The settled attempt still forbids replaying its own head.
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("PUBLIC_MERGE"))
  replay = domain.arm_merge(db, normal, normal.targets_json[0], {"head_sha": SHA}, principal)
  assert replay["state"] == "needs_you" and replay["head_moved"] == saved["head_moved"]
  db.refresh(normal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == saved
  # Observation is now a no-op: the attempt is settled.
  monkeypatch.setattr(domain, "read", lambda *a: pytest.fail("the attempt is already settled"))
  observe_item(db, normal, principal)


def test_pr_merged_at_a_later_head_settles_as_merged_not_a_non_merge(setup, monkeypatch):
  """A merge is terminal at any time, even while the old request may be in flight."""
  db, takeover, normal, principal, _ = attempted_merge(setup, monkeypatch, "merge_unknown",
    armed_at=iso(time.time()))
  serve(monkeypatch, {"repos/example/project": REPO, "repos/example/project/pulls/8": moved_pull(
    state="closed", merged=True, merge_commit_sha="d" * 40, merged_at="2026-10-24T01:00:00Z")})
  observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["pr_merged"] == {"head_sha": LATER, "merge_commit_sha": "d" * 40,
    "merged_at": "2026-10-24T01:00:00Z"}
  assert "head_moved" not in saved and saved["merge_attempted"] is True
  assert fence_clear(takeover)


@pytest.mark.parametrize("pull,repo", [
  (moved_pull(state="closed", merged=None), REPO),
  (moved_pull(merged=None), REPO),
  (moved_pull(node_id="PR_other"), REPO),
  (moved_pull(), {**REPO, "id": 2}),
  (None, REPO),
], ids=["closed-unconfirmed-merge-flag", "unconfirmed-merge-flag", "other-pr", "other-repository", "failed-read"])
def test_unproven_moved_head_leaves_the_attempt_fencing(setup, monkeypatch, pull, repo):
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown")
  responses = {"repos/example/project": repo}
  if pull is not None:
    responses["repos/example/project/pulls/8"] = pull
  serve(monkeypatch, responses)
  observe_item(db, normal, principal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == attempt
  assert not fence_clear(takeover)


def unknown_push(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **k: DispatchResult.unknown("Lost push response"))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert row.outcomes_json[domain.key(ITEM)]["repair_attempts"][0]["state"] == "push_unknown"
  return db, row, principal


def later_head(head=None):
  return {"repos/example/project": REPO, "repos/example/project/pulls/7": {
    **PULL, "merged": False, "head": {"sha": LATER, **(head or BRANCH)}}}


def test_later_push_on_top_of_an_unknown_repair_push_confirms_it(setup, monkeypatch):
  db, row, principal = unknown_push(setup, monkeypatch)
  serve(monkeypatch, {**later_head(),
    f"repos/example/project/compare/{NEW}...{LATER}": {"status": "ahead", "ahead_by": 1, "behind_by": 0}})
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **k: pytest.fail("PUBLIC_PUSH"))
  item = observe_item(db, row, principal)
  assert item["repair_attempts"][0]["state"] == "pushed"
  assert row.outcomes_json[domain.key(ITEM)]["successor"]["head_sha"] == NEW


@pytest.mark.parametrize("responses", [
  {**later_head(), f"repos/example/project/compare/{NEW}...{LATER}": {"status": "diverged", "ahead_by": 1, "behind_by": 1}},
  {**later_head(), f"repos/example/project/compare/{NEW}...{LATER}": {"status": "behind", "ahead_by": 0, "behind_by": 2}},
  later_head(),
  {**later_head({**BRANCH, "ref": "other"}), f"repos/example/project/compare/{NEW}...{LATER}": {"status": "ahead", "behind_by": 0}},
  {**later_head({**BRANCH, "repo": {"id": 2, "full_name": "example/project"}}),
   f"repos/example/project/compare/{NEW}...{LATER}": {"status": "ahead", "behind_by": 0}},
], ids=["diverged", "behind", "failed-compare", "other-branch", "other-head-repository"])
def test_unknown_push_not_contained_in_the_same_branch_stays_unknown(setup, monkeypatch, responses):
  db, row, principal = unknown_push(setup, monkeypatch)
  serve(monkeypatch, responses)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **k: pytest.fail("PUBLIC_PUSH"))
  item = observe_item(db, row, principal)
  assert item["repair_attempts"][0]["state"] == "push_unknown"
  assert "successor" not in row.outcomes_json[domain.key(ITEM)]


def pull_at(head, **changes):
  return {"repos/example/project": REPO,
          "repos/example/project/pulls/8": moved_pull(head={"sha": head, **BRANCH}, **changes)}


@pytest.mark.parametrize("state", ["merging", "merge_unknown"])
def test_head_moved_and_back_within_the_dispatch_window_keeps_the_attempt(setup, monkeypatch, state):
  """A->B->A: the old request's sha pin matches again, so it can still land."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, state,
    armed_at=iso(time.time()))
  serve(monkeypatch, pull_at(LATER))
  observe_item(db, normal, principal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == attempt
  assert not fence_clear(takeover) and not fence_clear(normal)
  # The head returns to the attempted one and the original request lands.
  serve(monkeypatch, pull_at(SHA, state="closed", merged=True, merge_commit_sha="d" * 40))
  item = observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert item["state"] == saved["state"] == "merged" and saved["merge_sha"] == "d" * 40
  assert "head_moved" not in saved


def test_crash_left_merging_settles_only_after_the_dispatch_window(setup, monkeypatch):
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  before = time.time()
  assert domain.arm_merge(db, normal, normal.targets_json[0], {"head_sha": SHA}, principal) is None
  # The process died here: no dispatch result was ever saved.
  armed = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert armed["state"] == "merging" and armed["merge_attempted"] is True
  armed_at = datetime.fromisoformat(armed["merge_armed_at"].replace("Z", "+00:00")).timestamp()
  assert before - 1 <= armed_at <= time.time() + 1
  serve(monkeypatch, pull_at(LATER))
  observe_item(db, normal, principal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == armed
  assert not fence_clear(takeover)
  monkeypatch.setattr(domain, "MERGE_DISPATCH_WINDOW_SECONDS", 0)
  observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["head_moved"] == {"attempted_head_sha": SHA, "attempt_state": "merging", "head_sha": LATER}
  assert fence_clear(takeover)


@pytest.mark.parametrize("armed_at", [None, "not a time"], ids=["missing", "unreadable"])
def test_legacy_attempt_without_arm_time_waits_a_window_from_process_load(setup, monkeypatch, armed_at):
  """An older build armed it before this process loaded; that bounds its dispatch."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown",
    armed_at=armed_at)
  serve(monkeypatch, pull_at(LATER))
  monkeypatch.setattr(domain, "_LOADED_AT", time.time())
  observe_item(db, normal, principal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == attempt
  assert not fence_clear(takeover)
  monkeypatch.setattr(domain, "_LOADED_AT", time.time() - domain.MERGE_DISPATCH_WINDOW_SECONDS - 1)
  observe_item(db, normal, principal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])]["head_moved"]["attempt_state"] == "merge_unknown"
  assert fence_clear(takeover)


def test_moved_head_settles_only_from_the_window_close_onward():
  target, armed = {"head_sha": SHA}, {"head_sha": SHA, "merge_armed_at": iso(1000)}
  closes = 1000 + domain.MERGE_DISPATCH_WINDOW_SECONDS
  assert domain.moved_head_fact(target, armed, moved_pull(), closes - 1) is None
  assert domain.moved_head_fact(target, armed, moved_pull(), closes)["head_moved"]["head_sha"] == LATER


def closed_pull(head=SHA, branch=BRANCH, **changes):
  return {"repos/example/project": REPO, "repos/example/project/pulls/8": moved_pull(
    state="closed", closed_at="2026-10-24T02:00:00Z", head={"sha": head, **branch}, **changes)}


def no_queue_entry(monkeypatch, head):
  """The closed PR's live checks: the attempted head is in no merge queue."""
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {"headRefOid": head, "mergeQueueEntry": None})


def still_pending(saved, attempt):
  assert saved["state"] == attempt["state"] and domain.merge_pending(saved)
  assert not set(domain.MERGE_SETTLEMENTS).intersection(saved)


@pytest.mark.parametrize("head", [SHA, LATER], ids=["attempted-head", "later-head"])
def test_closed_unmerged_within_the_dispatch_window_keeps_the_attempt(setup, monkeypatch, head):
  """It can be reopened at the attempted head while a request is in flight."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown",
    armed_at=iso(time.time()))
  serve(monkeypatch, closed_pull(head))
  no_queue_entry(monkeypatch, head)
  observe_item(db, normal, principal)
  still_pending(normal.outcomes_json[domain.key(normal.targets_json[0])], attempt)
  assert not fence_clear(takeover) and not fence_clear(normal)


@pytest.mark.parametrize("head", [SHA, LATER], ids=["attempted-head", "later-head"])
@pytest.mark.parametrize("state", ["merge_unknown", "queued", "merging"])
def test_closed_unmerged_after_the_dispatch_window_settles_without_rearming(setup, monkeypatch, state, head):
  """No request for the attempt can still be processed, and a closed PR cannot merge."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, state)
  serve(monkeypatch, closed_pull(head))
  no_queue_entry(monkeypatch, head)
  item = observe_item(db, normal, principal)
  assert item["state"] == "needs_you"
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["pr_closed_unmerged"] == {"attempted_head_sha": SHA, "attempt_state": state,
    "head_sha": head, "closed_at": "2026-10-24T02:00:00Z"}
  assert "head_moved" not in saved and "pr_merged" not in saved
  assert saved["head_sha"] == SHA and saved["merge_attempted"] is True
  assert saved.get("queue_entry_id") == attempt.get("queue_entry_id")
  assert fence_clear(takeover) and fence_clear(normal)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("PUBLIC_MERGE"))
  replay = domain.arm_merge(db, normal, normal.targets_json[0], {"head_sha": SHA}, principal)
  assert replay["state"] == "needs_you" and replay["pr_closed_unmerged"] == saved["pr_closed_unmerged"]
  db.refresh(normal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == saved
  monkeypatch.setattr(domain, "read", lambda *a: pytest.fail("the attempt is already settled"))
  observe_item(db, normal, principal)


def queue_read(monkeypatch, outcome):
  def pull_checks(*a):
    if isinstance(outcome, BaseException):
      raise outcome
    return outcome
  monkeypatch.setattr(domain, "pull_checks", pull_checks)


@pytest.mark.parametrize("head", [SHA, LATER], ids=["attempted-head", "later-head"])
@pytest.mark.parametrize("queue", [
  {"headRefOid": SHA, "mergeQueueEntry": {"id": "MQE_9", "enqueuedAt": "2026-10-24T02:30:00Z"}},
  {"headRefOid": LATER, "mergeQueueEntry": {"id": "MQE_9", "enqueuedAt": "2026-10-24T02:30:00Z"}},
  HTTPException(409, "GitHub could not confirm this pull request's checks."),
  {"headRefOid": SHA},
], ids=["entry-at-attempted-head", "entry-at-other-head", "read-failed", "entry-not-reported"])
def test_closed_pr_settles_only_once_a_live_read_shows_no_queue_entry(setup, monkeypatch, head, queue):
  """Closing is not documented to dequeue: observe the exit, never infer it."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown")
  serve(monkeypatch, closed_pull(head))
  queue_read(monkeypatch, queue)
  observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert domain.merge_pending(saved) and not set(domain.MERGE_SETTLEMENTS).intersection(saved)
  assert saved["head_sha"] == SHA and saved["merge_attempted"] is True
  assert not fence_clear(takeover) and not fence_clear(normal)
  # The same closed PR, once read in no merge queue, settles.
  no_queue_entry(monkeypatch, head)
  observe_item(db, normal, principal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])]["pr_closed_unmerged"]["head_sha"] == head
  assert fence_clear(takeover)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("gh", 90), OSError("gone"),
  ValueError("unreadable"), ContributionSubmitError("GitHub failed.", code="gh_failed")],
  ids=["timeout", "os-error", "unreadable", "gh-error"])
def test_a_failed_queue_read_never_settles_a_closed_pr(monkeypatch, failure):
  queue_read(monkeypatch, failure)
  armed = {"head_sha": SHA, "merge_armed_at": CLOSED}
  pull = moved_pull(state="closed", head={"sha": LATER, **BRANCH})
  assert domain.moved_head_fact({"head_sha": SHA}, armed, pull, time.time())["pr_closed_unmerged"]
  assert domain.later_pull_fact(None, "/tmp", {"head_sha": SHA, "pr_id": "PR_8"}, armed, pull, time.time()) is None


def test_closed_window_is_judged_from_when_the_read_started(setup, monkeypatch):
  """A read that starts before the window closes proves nothing, even if it ends after."""
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown",
    armed_at=iso(time.time()))
  armed = datetime.fromisoformat(attempt["merge_armed_at"].replace("Z", "+00:00")).timestamp()
  closes = armed + domain.MERGE_DISPATCH_WINDOW_SECONDS
  responses = closed_pull()
  serve(monkeypatch, responses)
  no_queue_entry(monkeypatch, SHA)
  clock = [closes - 1]
  served = domain.read
  def slow_read(gh, cwd, endpoint):
    clock[0] = closes + 60  # GitHub answers after the window closed.
    return served(gh, cwd, endpoint)
  monkeypatch.setattr(domain, "read", slow_read)
  monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: clock[0]))
  observe_item(db, normal, principal)
  still_pending(normal.outcomes_json[domain.key(normal.targets_json[0])], attempt)
  assert not fence_clear(takeover)
  observe_item(db, normal, principal)  # This read starts after the close.
  assert normal.outcomes_json[domain.key(normal.targets_json[0])]["pr_closed_unmerged"]["head_sha"] == SHA


def test_a_new_report_after_reopening_at_the_settled_head_never_dispatches(setup, monkeypatch):
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown")
  serve(monkeypatch, closed_pull())
  no_queue_entry(monkeypatch, SHA)
  observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["pr_closed_unmerged"]
  # Reopened at the attempted head, and GitHub would accept a merge now.
  serve(monkeypatch, {**pull_at(SHA), "user": {"id": 42}})
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "mergeQueueEntry": None})
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("PUBLIC_MERGE"))
  monkeypatch.setattr(domain, "enqueue", lambda *a: pytest.fail("PUBLIC_ENQUEUE"))
  item = report((db, normal, principal), number=8)["run"]["items"][0]
  assert item["state"] == "needs_you"
  db.refresh(normal)
  assert normal.outcomes_json[domain.key(normal.targets_json[0])] == saved


def test_closed_then_reopened_at_the_attempted_head_within_the_window_stays_pending(setup, monkeypatch):
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown",
    armed_at=iso(time.time()))
  serve(monkeypatch, closed_pull())
  no_queue_entry(monkeypatch, SHA)
  observe_item(db, normal, principal)
  still_pending(normal.outcomes_json[domain.key(normal.targets_json[0])], attempt)
  # Reopened at the attempted head: an in-flight request can still land.
  serve(monkeypatch, pull_at(SHA))
  no_queue_entry(monkeypatch, SHA)
  observe_item(db, normal, principal)
  still_pending(normal.outcomes_json[domain.key(normal.targets_json[0])], attempt)
  assert not fence_clear(takeover) and not fence_clear(normal)


def test_merge_after_close_and_reopen_records_pr_merged(setup, monkeypatch):
  db, takeover, normal, principal, attempt = attempted_merge(setup, monkeypatch, "merge_unknown",
    armed_at=iso(time.time()))
  serve(monkeypatch, closed_pull())
  no_queue_entry(monkeypatch, SHA)
  observe_item(db, normal, principal)
  still_pending(normal.outcomes_json[domain.key(normal.targets_json[0])], attempt)
  # Reopened, pushed and merged at a later head.
  serve(monkeypatch, closed_pull(LATER, merged=True, merge_commit_sha="d" * 40,
    merged_at="2026-10-24T03:00:00Z"))
  observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["pr_merged"] == {"head_sha": LATER, "merge_commit_sha": "d" * 40,
    "merged_at": "2026-10-24T03:00:00Z"}
  assert "pr_closed_unmerged" not in saved
  assert fence_clear(takeover)


def test_identityless_legacy_closed_pr_settles_a_window_after_process_load(setup, monkeypatch):
  """A legacy fork receipt without arm time or head-branch identity fences the repository."""
  db, takeover, normal, principal = mixed_grants(setup, monkeypatch)
  normal.targets_json = [{k: v for k, v in normal.targets_json[0].items()
                          if k not in {"head_repo_id", "head_ref"}}]
  db.commit()
  attempt = pending_outcome("merge")
  domain.save_outcome(db, normal, domain.key(normal.targets_json[0]), attempt)
  # The fork was deleted: GitHub reports no head repository.
  serve(monkeypatch, closed_pull(branch={"repo": None, "ref": "topic"}))
  no_queue_entry(monkeypatch, SHA)
  monkeypatch.setattr(domain, "_LOADED_AT", time.time())
  observe_item(db, normal, principal)
  still_pending(normal.outcomes_json[domain.key(normal.targets_json[0])], attempt)
  assert not fence_clear(takeover)
  monkeypatch.setattr(domain, "_LOADED_AT", time.time() - domain.MERGE_DISPATCH_WINDOW_SECONDS - 1)
  observe_item(db, normal, principal)
  saved = normal.outcomes_json[domain.key(normal.targets_json[0])]
  assert saved["pr_closed_unmerged"]["attempt_state"] == "merge_unknown"
  assert fence_clear(takeover)


def test_closed_pr_settles_only_from_the_window_close_onward():
  target, armed = {"head_sha": SHA}, {"head_sha": SHA, "merge_armed_at": iso(1000)}
  closes = 1000 + domain.MERGE_DISPATCH_WINDOW_SECONDS
  for head in (SHA, LATER):
    pull = moved_pull(state="closed", head={"sha": head, **BRANCH})
    assert domain.moved_head_fact(target, armed, pull, closes - 1) is None
    assert domain.moved_head_fact(target, armed, pull, closes)["pr_closed_unmerged"]["head_sha"] == head
  # An open PR at the attempted head, or a merge there, is not this fact.
  assert domain.moved_head_fact(target, armed, moved_pull(head={"sha": SHA, **BRANCH}), closes) is None
  assert domain.moved_head_fact(target, armed, moved_pull(state="closed", merged=True,
    head={"sha": SHA, **BRANCH}), closes) is None
