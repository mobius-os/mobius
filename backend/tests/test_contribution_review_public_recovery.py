"""Real transport and read-only recovery of the exact opted-in public effect."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app import contribution_review_public as public
from app import contribution_review_runs as domain, github_contribution_git as git
from app.contribution_errors import ContributionSubmitError
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, setup, report
from tests.test_contribution_review_public import opted_in


def test_real_gh_streams_public_body_over_stdin(tmp_path, monkeypatch):
  executable = tmp_path / 'gh'
  payload = tmp_path / 'payload.json'
  executable.write_text('#!/bin/sh\ncat > "$PAYLOAD_FILE"\nprintf \'{"id":5,"html_url":"review-url"}\'\n')
  executable.chmod(0o755)
  monkeypatch.setattr(git, '_git_env', lambda cwd: {'PATH': f'{tmp_path}:/usr/bin:/bin', 'PAYLOAD_FILE': str(payload)})
  result = public.post_review(git._gh, tmp_path, ITEM, '😀' * 30_000)
  assert result.state == 'confirmed' and result.receipt['id'] == 5
  sent = json.loads(payload.read_text())
  assert sent == {'commit_id': ITEM['head_sha'], 'event': 'COMMENT', 'body': '😀' * 30_000}


def test_not_dispatched_all_clear_retries_only_recorded_evidence(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  monkeypatch.setattr(domain, 'read', lambda *a: {'id': 99})
  report(setup, publish=True)
  assert row.outcomes_json[domain.key(ITEM)]['public_review']['state'] == 'not_dispatched'
  monkeypatch.setattr(domain, 'read', lambda *a: {'id': 42})
  calls = []
  monkeypatch.setattr(public, 'post_review', lambda *a: calls.append(a) or public.DispatchResult.confirmed({'id': 1}))
  with pytest.raises(HTTPException) as exc:
    report(setup, summary='changed evidence')
  assert exc.value.status_code == 409
  # Publishing retries also preserve the recorded all-clear.
  with pytest.raises(HTTPException) as exc:
    report(setup, publish=True, summary='changed evidence')
  assert exc.value.status_code == 409
  assert calls == []
  report(setup, publish=True)
  report(setup, publish=True)
  assert len(calls) == 1


@pytest.mark.parametrize('error,state', [
  (ContributionSubmitError('gh: forbidden (HTTP 403)'), 'not_dispatched'),
  (ContributionSubmitError('gh: bad commit (HTTP 422)'), 'not_dispatched'),
  (ContributionSubmitError('gh: unavailable (HTTP 503)'), 'unknown'),
  (ContributionSubmitError('gh: timeout (HTTP 408)'), 'unknown'),
  (ContributionSubmitError('gh: throttled (HTTP 429)'), 'unknown'),
  (ContributionSubmitError('connection dropped'), 'unknown'),
])
def test_public_provider_refusal_is_distinct_from_uncertainty(error, state):
  def gh(*a, **kw):
    raise error
  assert public.post_review(gh, None, ITEM, 'body').state == state


def test_programming_error_is_not_silently_saved_as_transport_uncertainty():
  def incompatible(cwd, *args):
    pytest.fail('argument binding must fail before entry')
  with pytest.raises(TypeError):
    public.post_review(incompatible, None, ITEM, 'body')


@pytest.mark.parametrize('initial', ['posting', 'unknown'])
@pytest.mark.parametrize('verdict', ['all_clear', 'needs_you'])
def test_observe_recovers_only_exact_review_without_reposting(setup, monkeypatch, initial, verdict):
  db, row, principal = setup
  opted_in(setup)
  monkeypatch.setattr(public, 'post_review', lambda *a: public.DispatchResult.unknown('response lost'))
  first = report(setup, publish=True, state=verdict)['run']
  assert first['state'] == 'needs_you'
  previous = row.outcomes_json[domain.key(ITEM)]
  receipt = {**previous['public_review'], 'state': initial}
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, 'public_review': receipt})
  expected = {'id': 19, 'html_url': 'review-url', 'user': {'id': 42}, 'commit_id': ITEM['head_sha'],
              'state': 'COMMENTED', 'body': receipt['marker']}
  calls = []
  def gh(cwd, *args):
    calls.append(args)
    assert args[0] == 'api' and '/reviews?' in args[1]
    return SimpleNamespace(stdout=json.dumps([expected]))
  monkeypatch.setattr(routes, '_gh', gh)
  monkeypatch.setattr(public, 'post_review', lambda *a: pytest.fail('replayed'))
  monkeypatch.setattr(domain, 'current_pull', lambda *a: pytest.fail('historical evidence needs no live PR'))
  result = asyncio.run(routes.observe_review(1, row.id, db, principal))['run']
  assert result['state'] == ('complete' if verdict == 'all_clear' else 'needs_you')
  saved = row.outcomes_json[domain.key(ITEM)]
  assert saved['summary'] == previous['summary'] and saved['tests'] == previous['tests']
  assert saved['public_review']['state'] == 'posted' and saved['public_review']['id'] == 19
  assert len(calls) == 1


@pytest.mark.parametrize('change', [{'user': {'id': 99}}, {'commit_id': 'b'*40}, {'body': 'unrelated'}, {'state': 'PENDING'}])
def test_recovery_cannot_attribute_another_review(change):
  receipt = {'state': 'unknown', 'marker': '<!-- mobius-review:unique -->'}
  item = {'id': 3, 'user': {'id': 42}, 'commit_id': ITEM['head_sha'], 'state': 'COMMENTED', 'body': receipt['marker'], **change}
  gh = lambda *a: SimpleNamespace(stdout=json.dumps([item]))
  assert public.reconcile_review(gh, None, ITEM, receipt, '42')['state'] == 'unknown'


def test_recovery_scans_later_page_and_keeps_legacy_uncertainty():
  receipt = {'state': 'posting', 'marker': '<!-- mobius-review:unique -->'}
  item = {'id': 3, 'user': {'id': 42}, 'commit_id': ITEM['head_sha'], 'state': 'COMMENTED', 'body': receipt['marker']}
  pages = iter([[{}]*100, [item]])
  gh = lambda *a: SimpleNamespace(stdout=json.dumps(next(pages)))
  assert public.reconcile_review(gh, None, ITEM, receipt, '42')['id'] == 3
  legacy = public.reconcile_review(lambda *a: pytest.fail('unattributable legacy receipt'), None, ITEM, {'state': 'posting'}, '42')
  assert legacy['state'] == 'unknown'


def test_find_review_still_matches_a_review_dismissed_after_it_was_sent():
  receipt = {'state': 'unknown', 'marker': '<!-- mobius-review:unique -->'}
  sent = {'id': 5, 'user': {'id': 42}, 'commit_id': ITEM['head_sha'], 'state': 'DISMISSED', 'body': receipt['marker']}
  gh = lambda *a: SimpleNamespace(stdout=json.dumps([sent]))
  for event in ('APPROVE', 'REQUEST_CHANGES'):
    review, complete = public.find_review(gh, None, ITEM, receipt, '42', event)
    assert (review['id'], review['state'], complete) == (5, 'DISMISSED', True)
  other = {**sent, 'user': {'id': 99}}
  assert public.find_review(lambda *a: SimpleNamespace(stdout=json.dumps([other])), None, ITEM, receipt, '42', 'APPROVE') == (None, True)


def test_find_review_proves_absence_only_when_the_whole_list_was_read():
  receipt = {'state': 'unknown', 'marker': '<!-- mobius-review:unique -->'}
  assert public.find_review(lambda *a: SimpleNamespace(stdout='[]'), None, ITEM, receipt, '42') == (None, True)
  full = lambda *a: SimpleNamespace(stdout=json.dumps([{}] * 100))
  assert public.find_review(full, None, ITEM, receipt, '42') == (None, False), 'ten full pages cap the scan: absence is unproven'
  broken = lambda *a: (_ for _ in ()).throw(OSError('network'))
  assert public.find_review(broken, None, ITEM, receipt, '42') == (None, False)
  assert public.find_review(lambda *a: SimpleNamespace(stdout='{}'), None, ITEM, receipt, '42') == (None, False)
  assert public.find_review(lambda *a: pytest.fail('no marker, nothing to look for'), None, ITEM, {'state': 'unknown'}, '42') == (None, False)


def test_marker_is_within_unicode_body_limit():
  marker = '<!-- mobius-review:unique -->'
  body = public.review_comment_body({'head_sha': ITEM['head_sha'], 'summary': '😀'*200_000, 'tests': '😀'*200_000}, marker=marker)
  assert body.endswith(marker) and public._body_units(body) <= public.GITHUB_REVIEW_BODY_MAX


@pytest.mark.parametrize('response', ['not json', '{}', '{"id":true}', '{"id":"not-an-id"}'])
def test_unreadable_public_receipt_stays_unknown(response):
  gh = lambda *a, **kw: SimpleNamespace(stdout=response)
  assert public.post_review(gh, None, ITEM, 'body').state == 'unknown'


def test_public_admission_rechecks_receipt_after_remote_actor_read(setup, monkeypatch):
  from app import models
  from app.database import SessionLocal
  db, row, _ = setup
  opted_in(setup)
  frozen = public.evidence(routes.ReviewOutcome(**{**ITEM, 'state': 'all_clear',
    'summary': 'Full diff reviewed', 'scope': sorted(routes.SCOPE), 'tests': 'Focused tests passed'}).model_dump())
  def actor(*args):
    other = SessionLocal()
    try:
      live = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, live, domain.key(ITEM), {**frozen,
        'public_review': {'state': 'posting', 'marker': 'another-worker-marker', 'evidence': frozen}})
    finally:
      other.close()
    return {'id': 42}
  monkeypatch.setattr(domain, 'read', actor)
  monkeypatch.setattr(public, 'post_review', lambda *a: pytest.fail('another worker already armed this review'))
  result = report(setup, publish=True)['run']['items'][0]
  assert result['public_review']['marker'] == 'another-worker-marker'


def test_stale_public_observation_cannot_erase_confirmed_receipt(setup):
  db, row, _ = setup
  receipt = {'state': 'posted', 'id': 42, 'marker': 'exact-attempt'}
  confirmed = {'state': 'all_clear', 'summary': 'immutable evidence', 'public_review': receipt}
  domain.save_outcome(db, row, domain.key(ITEM), confirmed)
  domain.save_effect_outcome(db, row, domain.key(ITEM), {**confirmed,
    'public_review': {**receipt, 'state': 'unknown'}})
  assert row.outcomes_json[domain.key(ITEM)] == confirmed


@pytest.mark.parametrize('state', ['posting', 'unknown'])
def test_other_attempt_cannot_disarm_public_receipt(setup, state):
  db, row, _ = setup
  original = {'state': 'all_clear', 'public_review': {'state': state, 'marker': 'armed-by-other-worker'}}
  domain.save_outcome(db, row, domain.key(ITEM), original)
  domain.save_effect_outcome(db, row, domain.key(ITEM), {'state': 'all_clear',
    'public_review': {'state': 'not_dispatched'}})
  assert row.outcomes_json[domain.key(ITEM)] == original


def test_provider_refusal_releases_only_own_public_attempt_for_retry(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  calls = []
  def gh(*args, **kwargs):
    calls.append(kwargs['input_text'])
    if len(calls) == 1:
      raise ContributionSubmitError('gh: rejected commit (HTTP 422)')
    return SimpleNamespace(stdout='{"id":77}')
  monkeypatch.setattr(routes, '_gh', gh)
  report(setup, publish=True)
  first = row.outcomes_json[domain.key(ITEM)]['public_review']
  assert first['state'] == 'not_dispatched'
  report(setup, publish=True)
  final = row.outcomes_json[domain.key(ITEM)]['public_review']
  assert final['state'] == 'posted' and final['id'] == 77
  assert final['marker'] != first['marker'] and len(calls) == 2


def test_moved_head_keeps_reported_verdict_but_blocks_public_post(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  def moved(*args):
    raise HTTPException(409, 'The selected head moved; approve a new selection.')
  monkeypatch.setattr(domain, 'current_pull', moved)
  monkeypatch.setattr(public, 'post_review', lambda *args: pytest.fail('stale head must not post'))
  result = report(setup, publish=True)['run']
  item = result['items'][0]
  assert item['state'] == 'all_clear' and item['summary'] == 'Full diff reviewed'
  assert item['public_review']['state'] == 'not_dispatched'
  assert 'head moved' in item['live_blocker'] and result['state'] == 'needs_you'


def test_stop_during_account_lookup_refuses_even_account_changed_path(setup, monkeypatch):
  from app import models
  from app.database import SessionLocal
  db, row, _ = setup
  opted_in(setup)
  def actor(*args):
    other = SessionLocal()
    try:
      live = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, live, routes.REVIEW_STOP_KEY, {'state': 'requested'})
    finally:
      other.close()
    return {'id': 99}
  monkeypatch.setattr(domain, 'read', actor)
  monkeypatch.setattr(public, 'post_review', lambda *args: pytest.fail('must not post after Stop'))
  with pytest.raises(HTTPException) as error:
    report(setup, publish=True)
  assert error.value.status_code == 409
  db.refresh(row)
  assert domain.key(ITEM) not in row.outcomes_json


def test_failed_live_read_cannot_replace_refused_public_all_clear(setup, monkeypatch):
  from copy import deepcopy
  db, row, _ = setup
  opted_in(setup)
  monkeypatch.setattr(domain, 'read', lambda *a: {'id': 99})
  report(setup, publish=True)
  saved = deepcopy(row.outcomes_json[domain.key(ITEM)])
  assert saved['state'] == 'all_clear'
  assert saved['public_review']['state'] == 'not_dispatched'
  current_pull = domain.current_pull
  monkeypatch.setattr(domain, 'current_pull', lambda *a: (_ for _ in ()).throw(
    HTTPException(409, 'Live PR read unavailable')))
  calls = []
  monkeypatch.setattr(public, 'post_review', lambda *a: calls.append(a) or public.DispatchResult.confirmed({'id': 1}))
  changed = {'publish': True, 'state': 'needs_you', 'summary': 'Changed verdict'}
  with pytest.raises(HTTPException) as error:
    report(setup, **changed)
  assert error.value.status_code == 409
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)] == saved
  monkeypatch.setattr(domain, 'current_pull', current_pull)
  monkeypatch.setattr(domain, 'read', lambda *a: {'id': 42})
  with pytest.raises(HTTPException) as error:
    report(setup, **changed)
  assert error.value.status_code == 409
  assert row.outcomes_json[domain.key(ITEM)] == saved
  assert calls == []
  report(setup, publish=True)
  assert len(calls) == 1
  assert row.outcomes_json[domain.key(ITEM)]['summary'] == 'Full diff reviewed'
