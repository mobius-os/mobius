"""A readiness receipt an older build armed fences merges by the same branch identity."""
import pytest
from fastapi import HTTPException

from app import contribution_review_runs as domain
from tests.test_contribution_review_runs import setup, SHA, BASE  # noqa: F401  (fixture)
from tests.test_contribution_review_readiness import ready_takeover
from tests.test_contribution_review_workflows import overlapping_grant
from tests.test_contribution_review_branch_admission import inspected_target


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
