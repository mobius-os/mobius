"""Readiness uses the same immutable branch identity as ordinary merge grants."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, SHA, BASE
from tests.test_contribution_review_workflows import ready_takeover, ready_body, overlapping_grant
from tests.test_contribution_review_branch_admission import inspected_target, pending_outcome


@pytest.mark.parametrize("state", ["merge_unknown", "queued"])
@pytest.mark.parametrize("legacy", [False, True])
def test_ready_does_not_cross_normal_sibling_merge_or_unmatchable_legacy_receipt(setup, monkeypatch, state, legacy):
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row, number=8)
  peer.mode = "review_merge"
  target = inspected_target(monkeypatch, number=8)
  if legacy:
    target.pop("head_repo_id", None)
    target.pop("head_ref", None)
  peer.targets_json = [target]
  db.commit()
  domain.save_outcome(db, peer, domain.key(target), pending_outcome("merge", state))
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("PUBLIC_READY"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(row.targets_json[0])].get("ready_attempt")


def test_normal_sibling_merge_cannot_cross_unknown_readiness_receipt(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(row.targets_json[0])]
  domain.save_outcome(db, row, domain.key(row.targets_json[0]), {**previous,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  peer = overlapping_grant(db, row, number=8)
  peer.mode = "review_merge"
  peer.targets_json = [inspected_target(monkeypatch, number=8)]
  db.commit()
  with pytest.raises(HTTPException) as error:
    domain.arm_merge(db, peer, peer.targets_json[0], {"head_sha": SHA}, principal)
  assert error.value.status_code == 409
