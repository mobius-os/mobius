"""Receipt-aware checkout ownership at Stop and confirmed publication."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_repair_actions as actions
from app import contribution_review_repairs as repairs
from app import contribution_review_runs as domain, models
from app.database import SessionLocal
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM, PULL, REPO, report
from tests.test_contribution_review_workflows import BASE, NEW, SHA, takeover

FIRST = {"id": "push-1", "state": "pushing", "from_sha": SHA, "head_sha": NEW,
         "base_sha": BASE, "checkout": "/first", "work_key": "first-push"}
SECOND = {"id": "push-2", "state": "pushing", "from_sha": NEW, "head_sha": "d" * 40,
          "base_sha": BASE, "checkout": "/second", "work_key": "second-push"}


def test_confirmed_reconcile_discards_checkout_after_durable_successor(setup, monkeypatch):
  db, row, _ = setup
  receipt = {'id': 'push-1', 'from_sha': ITEM['head_sha'], 'head_sha': 'c'*40,
             'base_sha': ITEM['base_sha'], 'state': 'push_unknown', 'checkout': '/confirmed'}
  previous = {'state': 'needs_you', 'repair_attempts': [receipt], 'checkout': {'checkout': '/confirmed'}}
  domain.save_outcome(db, row, domain.key(ITEM), previous)
  deleted = []
  def discard(path):
    assert row.outcomes_json[domain.key(ITEM)]['repair_attempts'][0]['state'] == 'pushed'
    deleted.append(path)
  monkeypatch.setattr(repairs, 'discard_checkout', discard)
  assert asyncio.run(actions.reconcile_push(db, row, ITEM, receipt, gh=None, cwd=None))
  assert deleted == ['/confirmed']
  assert 'checkout' not in row.outcomes_json[domain.key(ITEM)]


def confirm_and_arm_next(run_id):
  """A second worker (own session) confirms repair 1, then arms repair 2."""
  with SessionLocal() as other:
    row = other.get(models.ContributionReviewRun, run_id)
    previous = row.outcomes_json[domain.key(ITEM)]
    pushed = [{**FIRST, "state": "pushed"}]
    domain.save_effect_outcome(other, row, domain.key(ITEM),
      domain.confirmed_successor(previous, pushed, FIRST))
    successor = row.outcomes_json[domain.key(ITEM)]
    domain.save_outcome(other, row, domain.key(ITEM), {**successor, "state": "pushing",
      "repair_attempts": [*pushed, SECOND], "findings": "second round"})


def assert_later_repair_survives(db, row):
  db.refresh(row)
  item = row.outcomes_json[domain.key(ITEM)]
  assert [(a["id"], a["state"]) for a in item["repair_attempts"]] == [
    ("push-1", "pushed"), ("push-2", "pushing")]
  assert item["successor"] == {"head_sha": NEW, "base_sha": BASE}
  assert item["findings"] == "second round"
  target = domain.effective_target(row, row.targets_json[0])
  with pytest.raises(HTTPException) as error:
    domain.require_public_transition_clear(db, row, target)
  assert "earlier public attempt" in error.value.detail


def test_delayed_confirmation_keeps_a_later_armed_repair(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "pushing", "repair_attempts": [FIRST]})
  def delayed_read(*args):
    confirm_and_arm_next(row.id)
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", delayed_read)
  monkeypatch.setattr(repairs, "discard_checkout", lambda path: None)
  asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert_later_repair_survives(db, row)


def test_delayed_failed_observation_keeps_confirmed_successor_and_later_repair(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "pushing", "repair_attempts": [FIRST]})
  def delayed_failure(*args):
    confirm_and_arm_next(row.id)
    raise HTTPException(409, "PR projection lags")
  monkeypatch.setattr(domain, "current_pull", delayed_failure)
  asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert_later_repair_survives(db, row)


def test_private_report_cannot_overwrite_a_repair_armed_during_its_read(setup, monkeypatch):
  db, row, _ = takeover(setup, monkeypatch)
  def arm_during_read(*args):
    with SessionLocal() as other:
      current = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, current, domain.key(ITEM), {"state": "pushing", "repair_attempts": [FIRST]})
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", arm_during_read)
  with pytest.raises(HTTPException) as error:
    report(setup, state="needs_you", summary="A question for the owner")
  assert error.value.status_code == 409
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)] == {"state": "pushing", "repair_attempts": [FIRST]}


@pytest.mark.parametrize("armed", [{"state": "pushing", "repair_attempts": [FIRST]},
                                   {"state": "merging", "merge_attempted": True}])
def test_refreshed_stale_write_cannot_drop_a_saved_public_attempt(setup, armed):
  """E.g. a merge-blocker report: authorization refreshes the row after I/O."""
  db, row, _ = setup
  stale = row.outcomes_json.get(domain.key(ITEM), {})
  with SessionLocal() as other:
    domain.save_outcome(other, other.get(models.ContributionReviewRun, row.id), domain.key(ITEM), armed)
  db.refresh(row)
  with pytest.raises(HTTPException) as error:
    domain.save_outcome(db, row, domain.key(ITEM), {**stale, "state": "needs_you", "summary": "Blocked"})
  assert error.value.status_code == 409
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)] == armed


@pytest.mark.parametrize("observer", ["observe", "report"])
def test_delayed_merge_observation_cannot_revive_a_disarmed_merge_over_a_repair(setup, monkeypatch, observer):
  """A reads 'merging'; B disarms after definite non-dispatch and arms a repair."""
  db, row, principal = takeover(setup, monkeypatch)
  target = row.targets_json[0]
  assert domain.arm_merge(db, row, target, {"head_sha": SHA}, principal) is None
  armed_repair = {**FIRST, "work_key": (
    f"github:{target['head_repo'].lower()}:branch:{target['head_ref']}:{target['head_sha']}:repair")}
  def disarm_and_repair_during_read(*args):
    with SessionLocal() as other:
      current = other.get(models.ContributionReviewRun, row.id)
      domain.disarm_merge(other, current, target, "GitHub refused the merge; nothing was sent.")
      assert domain.arm_repair(other, current, target, {}, FIRST, principal) is None
    return REPO, PULL
  monkeypatch.setattr(domain, "current_pull", disarm_and_repair_during_read)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {"headRefOid": SHA})
  if observer == "observe":
    asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  else:
    report(setup)
  db.refresh(row)
  item = row.outcomes_json[domain.key(ITEM)]
  assert item["state"] == "pushing" and item["merge_attempted"] is False
  assert item["repair_attempts"] == [armed_repair]


@pytest.mark.parametrize("outcome", ["confirmed", "refused", "cancelled_before_push"])
def test_cancellation_mid_push_still_records_the_definite_result(setup, monkeypatch, outcome):
  """A restart or cancelled request mid-push must not strand 'pushing'."""
  import threading
  from app.contribution_review_effects import DispatchResult
  from tests.test_contribution_review_workflows import configure_repair, publish_body
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "discard_checkout", lambda path: None)
  started, cancelled = threading.Event(), threading.Event()
  sent = []
  def push(*args, before_push=None, **kwargs):
    started.set()
    assert cancelled.wait(5)
    if outcome == "cancelled_before_push":
      try:
        before_push()
      except Exception:
        return DispatchResult.not_dispatched("The repair failed a final pre-dispatch guard.")
    sent.append(1)
    if outcome == "refused":
      return DispatchResult.not_dispatched("GitHub refused before any update.")
    return DispatchResult.confirmed({"head_sha": NEW})
  monkeypatch.setattr(repairs, "push_repair", push)

  async def cancel_mid_push():
    task = asyncio.create_task(routes.publish_repair(1, row.id, publish_body(), db, principal))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
      await task
  asyncio.run(cancel_mid_push())

  db.refresh(row)
  item = row.outcomes_json[domain.key(ITEM)]
  work_key = "github:example/project:branch:topic:" + SHA + ":repair"
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=work_key).one()
  if outcome == "confirmed":
    assert [a["state"] for a in item["repair_attempts"]] == ["pushed"]
    assert item["successor"]["head_sha"] == NEW
    assert claim.released_at is None and claim.completed_at is None
  else:
    assert sent == ([1] if outcome == "refused" else [])
    assert item["repair_attempts"] == [] and item["state"] == "needs_you"
    assert claim.released_at is not None
