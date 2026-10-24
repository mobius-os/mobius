"""Receipt-aware checkout ownership at Stop and confirmed publication."""
import asyncio

from app import contribution_review_repair_actions as actions
from app import contribution_review_repairs as repairs
from app import contribution_review_runs as domain
from tests.test_contribution_review_runs import setup, ITEM


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
  assert asyncio.run(actions.reconcile_push(db, row, ITEM, previous, receipt, gh=None, cwd=None))
  assert deleted == ['/confirmed']
  assert 'checkout' not in row.outcomes_json[domain.key(ITEM)]
