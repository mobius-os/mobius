"""Branch exclusion spans ordinary merges, takeovers and immutable legacy grants."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from fastapi import HTTPException

from app import agent_work_claims, contribution_review_runs as domain, models
from app.database import SessionLocal
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, report
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
