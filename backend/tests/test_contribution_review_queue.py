"""Complete queue-history proof and historical no-replay fences."""
import asyncio
import json
import subprocess
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import contribution_review_runs as domain, models
from app.routes import contribution_reviews as routes
from app.chat_writer import create_chat
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, CHECKS, report
QUEUE_TIME = "2026-10-07T20:13:24Z"
REMOVED_TIME = "2026-10-07T20:18:09Z"


def test_historical_same_head_attempt_blocks_new_merge_even_after_successor(setup):
  db, row, principal = setup
  old = models.ContributionReviewRun(id="historical-head", app_id=1, owner_id=principal.owner.id,
    request_id="historical-request", mode="review_fix_merge", targets_json=[TARGET],
    outcomes_json={domain.key(TARGET): {"state": "reviewing", "head_sha": "c" * 40,
      "merge_attempts": [{"state": "removed_from_queue", "head_sha": SHA,
        "merge_attempted": True}]}},
    chat_id=row.chat_id, github_actor_id="42", app_nonce="nonce")
  db.add(old)
  db.commit()
  outcome = domain.arm_merge(db, row, TARGET, {"state": "all_clear", "head_sha": SHA}, principal)
  assert outcome and outcome["state"] == "needs_you"
  assert outcome["review_selection_id"] == old.id


def queue_timeline():
  # Actual GraphQL shapes: removal has no entry id and beforeCommit is NOT
  # the PR head. Neither reason nor this synthetic SHA supplies identity.
  return {"id": TARGET["pr_id"], "headRefOid": SHA, "state": "OPEN",
    "merged": False, "mergeQueueEntry": None, "timelineItems": {
      "pageInfo": {"hasPreviousPage": False, "hasNextPage": False},
      "nodes": [
        {"__typename": "AddedToMergeQueueEvent", "id": "added-1", "createdAt": QUEUE_TIME},
        {"__typename": "RemovedFromMergeQueueEvent", "id": "removed-1",
         "createdAt": REMOVED_TIME, "beforeCommit": {"oid": "e" * 40},
         "reason": "failed_checks token=PRIVATE ghp_do_not_disclose"}]}}


def queue_graphql(monkeypatch, node):
  """Schema fake for only the read-only fields these routes actually request."""
  calls = []
  def gh(cwd, *args):
    query = next(a[len("query="):] for a in args if a.startswith("query="))
    assert query.startswith("query(") and "mutation" not in query
    assert f"id={TARGET['pr_id']}" in args
    assert "timelineItems(last:100," in query
    assert "ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT" in query
    assert "pageInfo{hasPreviousPage hasNextPage}" in query
    assert "... on AddedToMergeQueueEvent{id createdAt}" in query
    assert "... on RemovedFromMergeQueueEvent{id createdAt}" in query
    assert "beforeCommit" not in query and "reason" not in query
    calls.append(query)
    return SimpleNamespace(stdout=json.dumps({"data": {"node": node}}))
  monkeypatch.setattr(routes, "_gh", gh)
  return gh, calls


def saved_queue_receipt(**changes):
  return {"state": "queued", "head_sha": SHA, "merge_attempted": True,
    "queue_entry_id": "entry-1", "queue_enqueued_at": QUEUE_TIME, **changes}


@pytest.mark.parametrize("legacy", [False, True])
def test_confirmed_queue_removal_uses_admission_not_synthetic_commit(monkeypatch, legacy):
  gh, calls = queue_graphql(monkeypatch, queue_timeline())
  previous = saved_queue_receipt()
  if legacy:
    previous.pop("queue_enqueued_at")
  proof = domain.confirmed_queue_removal(gh, "/tmp", TARGET, previous)
  assert proof == {"queue_entry_id": "entry-1", "head_sha": SHA,
    "added_event_id": "added-1", "enqueued_at": QUEUE_TIME,
    "removed_event_id": "removed-1", "removed_at": REMOVED_TIME}
  assert len(calls) == 1 and "PRIVATE" not in json.dumps(proof)


@pytest.mark.parametrize("case", ["missing_receipt", "unknown_receipt", "missing_events",
  "incomplete", "later_page", "missing_paging", "stale", "ambiguous_legacy", "readded",
  "unordered", "duplicate_id", "bad_date", "wrong_pr",
  "changed_head", "merged", "closed", "live_entry", "unknown_entry", "prior_proof_changed"])
def test_removal_proof_fails_closed(monkeypatch, case):
  node = queue_timeline()
  previous = saved_queue_receipt()
  timeline = node["timelineItems"]
  events = timeline["nodes"]
  later = [{**events[0], "id": "added-2", "createdAt": "2026-10-07T21:00:00Z"},
           {**events[1], "id": "removed-2", "createdAt": "2026-10-07T21:05:00Z"}]
  if case == "missing_receipt": previous.pop("queue_entry_id")
  elif case == "unknown_receipt": previous["queue_receipt_unconfirmed"] = True
  elif case == "missing_events": timeline["nodes"] = []
  elif case == "incomplete": timeline["pageInfo"]["hasPreviousPage"] = True
  elif case == "later_page": timeline["pageInfo"]["hasNextPage"] = True
  elif case == "missing_paging": timeline.pop("pageInfo")
  elif case == "stale": previous["queue_enqueued_at"] = "2026-10-07T20:00:00Z"
  elif case == "ambiguous_legacy":
    previous.pop("queue_enqueued_at")
    events.extend(later)
  elif case == "readded": events.extend(later[:1])
  elif case == "unordered": events.reverse()
  elif case == "duplicate_id": events[1]["id"] = events[0]["id"]
  elif case == "bad_date": events[1]["createdAt"] = "unparseable"
  elif case == "wrong_pr": node["id"] = "PR_other"
  elif case == "changed_head": node["headRefOid"] = "c" * 40
  elif case == "merged": node["merged"] = True
  elif case == "closed": node["state"] = "CLOSED"
  elif case == "live_entry": node["mergeQueueEntry"] = {"id": "entry-2"}
  elif case == "unknown_entry": node.pop("mergeQueueEntry")
  elif case == "prior_proof_changed": previous["queue_removal"] = {"removed_event_id": "older"}
  gh, _ = queue_graphql(monkeypatch, node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, previous) is None


def test_equal_time_ordered_add_and_remove_proves_saved_admission(monkeypatch):
  node = queue_timeline()
  node["timelineItems"]["nodes"][1]["createdAt"] = QUEUE_TIME
  gh, _ = queue_graphql(monkeypatch, node)
  proof = domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt())
  assert proof and proof["removed_event_id"] == "removed-1"


def test_saved_admission_can_precede_later_completed_external_cycle(monkeypatch):
  node = queue_timeline()
  events = node["timelineItems"]["nodes"]
  events.extend([
    {**events[0], "id": "added-later", "createdAt": "2026-10-07T21:00:00Z"},
    {**events[1], "id": "removed-later", "createdAt": "2026-10-07T21:05:00Z"}])
  gh, _ = queue_graphql(monkeypatch, node)
  proof = domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt())
  assert proof and proof["added_event_id"] == "added-1"


def test_duplicate_admission_timestamp_is_ambiguous(monkeypatch):
  node = queue_timeline()
  events = node["timelineItems"]["nodes"]
  events[1]["createdAt"] = QUEUE_TIME
  events.extend([
    {**events[0], "id": "added-later"},
    {**events[1], "id": "removed-later", "createdAt": REMOVED_TIME}])
  gh, _ = queue_graphql(monkeypatch, node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt()) is None


@pytest.mark.parametrize("bad_node", [None, [], {"timelineItems": []}, {"timelineItems": {"nodes": [None]}}])
def test_malformed_queue_provider_shape_never_proves_removal(monkeypatch, bad_node):
  gh, _ = queue_graphql(monkeypatch, bad_node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt()) is None


def test_unexpected_queue_proof_bug_is_not_silently_unknown(monkeypatch):
  def broken_gh(*args):
    raise RuntimeError("programming error")
  with pytest.raises(RuntimeError, match="programming error"):
    domain.confirmed_queue_removal(broken_gh, "/tmp", TARGET, saved_queue_receipt())


def test_timestamp_receipt_can_match_latest_pair_in_complete_history(monkeypatch):
  node = queue_timeline()
  events = node["timelineItems"]["nodes"]
  events[:0] = [
    {**events[0], "id": "added-older", "createdAt": "2026-10-07T19:00:00Z"},
    {**events[1], "id": "removed-older", "createdAt": "2026-10-07T19:05:00Z"}]
  gh, _ = queue_graphql(monkeypatch, node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt())["added_event_id"] == "added-1"


def test_enqueue_timestamp_is_requested_and_durably_saved(setup, monkeypatch):
  calls = []
  def gh(cwd, *args, check=True):
    assert check is False
    query = next(a for a in args if a.startswith("query="))
    assert "mergeQueueEntry{id enqueuedAt}" in query
    calls.append(query)
    return SimpleNamespace(stdout=json.dumps({"data": {"enqueuePullRequest": {
      "mergeQueueEntry": {"id": "entry-1", "enqueuedAt": QUEUE_TIME}}}}))
  monkeypatch.setattr(routes, "_gh", gh)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "isMergeQueueEnabled": True})
  result = report(setup)["run"]["items"][0]
  assert result["queue_enqueued_at"] == QUEUE_TIME and result["merge_attempted"]
  assert len(calls) == 1
  assert setup[1].outcomes_json[domain.key(ITEM)]["queue_enqueued_at"] == QUEUE_TIME


def test_requeue_observation_never_overwrites_original_receipt(setup, monkeypatch):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), saved_queue_receipt())
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
    "mergeQueueEntry": {"id": "entry-2", "enqueuedAt": "2026-10-07T21:00:00Z"}})
  result = report(setup)["run"]["items"][0]
  assert result["state"] == "queued" and result["queue_entry_id"] == "entry-1"
  assert result["queue_enqueued_at"] == QUEUE_TIME


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-10-07T20:13:24"])
def test_new_receipt_missing_valid_timestamp_cannot_use_legacy_exception(monkeypatch, timestamp):
  previous = saved_queue_receipt(**domain.queue_receipt({"id": "entry-1", "enqueuedAt": timestamp}))
  assert previous["queue_receipt_unconfirmed"] is True
  gh, calls = queue_graphql(monkeypatch, queue_timeline())
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, previous) is None
  assert calls == []


def test_unknown_attempt_observed_in_queue_never_becomes_legacy_repair_receipt(setup, monkeypatch):
  db, row, _ = setup
  domain.save_outcome(db, row, domain.key(ITEM), {
    "state": "merge_unknown", "head_sha": SHA, "merge_attempted": True})
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
    "mergeQueueEntry": {"id": "entry-1", "enqueuedAt": QUEUE_TIME}})
  queued = report(setup)["run"]["items"][0]
  assert queued["state"] == "queued" and queued["queue_receipt_unconfirmed"] is True
  monkeypatch.setattr(domain, "pull_checks", lambda *a: CHECKS)
  queue_graphql(monkeypatch, queue_timeline())
  removed = report(setup)["run"]["items"][0]
  assert removed["state"] == "merge_unknown" and not removed.get("queue_removal")


def test_queue_timeout_preserves_unknown_and_observe_returns_saved_evidence(setup, monkeypatch):
  db, row, principal = setup
  previous = saved_queue_receipt(summary="Private review evidence")
  domain.save_outcome(db, row, domain.key(TARGET), previous)
  def timed_out(*args):
    raise subprocess.TimeoutExpired(["gh", "api", "graphql"], 30)
  monkeypatch.setattr(routes, "_gh", timed_out)
  assert domain.confirmed_queue_removal(timed_out, "/tmp", TARGET, previous) is None
  result = asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert result["run"]["items"][0]["state"] == "merge_unknown"
  assert row.outcomes_json[domain.key(TARGET)]["queue_entry_id"] == "entry-1"


def test_complete_but_non_alternating_queue_history_is_not_removal_proof(monkeypatch):
  node = queue_timeline()
  added, removed = node["timelineItems"]["nodes"]
  node["timelineItems"]["nodes"] = [added,
    {**added, "id": "added-2", "createdAt": "2026-10-07T20:14:00Z"},
    removed, {**removed, "id": "removed-2", "createdAt": "2026-10-07T20:19:00Z"}]
  gh, _ = queue_graphql(monkeypatch, node)
  assert domain.confirmed_queue_removal(gh, "/tmp", TARGET, saved_queue_receipt()) is None


def test_queue_removal_exemption_belongs_only_to_its_own_grant(setup):
  from tests.test_contribution_review_workflows import overlapping_grant
  db, row, principal = setup
  evidence = saved_queue_receipt(state="needs_you", queue_removal={"removed_event_id": "proof"})
  domain.save_outcome(db, row, domain.key(TARGET), evidence)
  domain.require_public_transition_clear(db, row, TARGET)
  row.options_json = {}
  db.commit()
  peer = overlapping_grant(db, row)
  domain.save_outcome(db, peer, domain.key(TARGET), evidence)
  with pytest.raises(HTTPException, match="earlier public attempt"):
    domain.require_public_transition_clear(db, row, TARGET)


def test_shared_fence_projects_only_overlapping_or_unmatchable_grants(setup):
  db, row, principal = setup
  target = {**TARGET, "head_repo_id": 11, "head_ref": "topic"}
  row.targets_json = [target]
  db.commit()
  selections = {
    "same-pr": {**target, "head_repo_id": 99, "head_ref": "different"},
    "same-branch": {**target, "number": 8},
    "legacy": {**TARGET, "number": 9},
    "bad-type": {**target, "number": 10, "head_repo_id": "11", "head_ref": "else"},
    "bad-ref": {**target, "number": 11, "head_ref": " "},
    "unicode-ref": {**target, "number": 13, "head_ref": "\u3000legacy"},
    "disjoint": {**target, "number": 12, "head_repo_id": 99, "head_ref": "different"},
    "disjoint-case": {**target, "repo": "another/repo", "head_ref": "different"},
    "legacy-other-repo": {**TARGET, "repo": "another/repo"},
  }
  for name, item in selections.items():
    db.add(models.ContributionReviewRun(id=name, app_id=row.app_id, owner_id=row.owner_id,
      request_id=name, mode=row.mode, github_actor_id=row.github_actor_id, app_nonce=row.app_nonce,
      options_json={}, targets_json=[item], outcomes_json={}, chat_id=row.chat_id))
  db.commit()
  candidates = domain._transition_candidates(db, row.owner_id, target)
  assert {grant.id for grant in candidates} == {row.id, "same-pr", "same-branch", "legacy", "bad-type", "bad-ref", "unicode-ref"}
  assert all(set(grant._mapping) == {"id", "chat_id", "targets_json", "outcomes_json"} for grant in candidates)
  # Legacy caller identity is uncertain within its repository, not account-wide.
  assert {grant.id for grant in domain._transition_candidates(db, row.owner_id, TARGET)} == (
    {row.id, *selections} - {"disjoint-case", "legacy-other-repo"})


def test_removed_attempt_snapshot_excludes_recursive_aggregate_history(monkeypatch):
  previous = saved_queue_receipt(**{name: [{"nested": "old"}] for name in (
    "steps", "repair_attempts", "independent_reviews", "reviewer_steps", "ready_audit")},
    merge_attempts=[{"head_sha": "c" * 40}], checkout={"path": "/owned"}, summary="Exact evidence")
  gh, _ = queue_graphql(monkeypatch, queue_timeline())
  monkeypatch.setattr(domain, "pull_checks", lambda *a: CHECKS)
  result = domain.reconcile_merge(gh, "/tmp", TARGET, previous, PULL)
  snapshot = result["merge_attempts"][-1]
  assert snapshot["summary"] == "Exact evidence" and snapshot["head_sha"] == SHA
  assert not {"steps", "merge_attempts", "repair_attempts", "independent_reviews",
    "reviewer_steps", "ready_audit", "checkout"}.intersection(snapshot)


def test_repair_prior_receipts_use_the_shared_sql_projection(setup, monkeypatch):
  from tests.test_contribution_review_workflows import configure_repair, NEW
  db, row, principal = configure_repair(setup, monkeypatch)
  target = row.targets_json[0]
  real_candidates = domain._transition_candidates
  selected = []
  def candidates(*args):
    result = real_candidates(*args)
    assert all(not isinstance(grant, models.ContributionReviewRun) for grant in result)
    selected.append(result)
    return result
  monkeypatch.setattr(domain, "_transition_candidates", candidates)
  domain.arm_repair(db, row, target, {}, {"id": "projection-repair", "state": "pushing",
    "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}, principal)
  # Prior repair receipts and the cross-action fence both use the projection.
  assert len(selected) == 2
  assert row.outcomes_json[domain.key(target)]["repair_attempts"][-1]["id"] == "projection-repair"
