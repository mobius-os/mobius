"""Public verdict consent, cross-grant admission and private list projections."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException
from app import models, contribution_review_runs as domain, contribution_review_public as public
from app.database import SessionLocal
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, TARGET, setup, report, settle_fixture_review
from tests.test_contribution_review_public import opted_in


@pytest.mark.parametrize('state', ['posting', 'unknown', 'posted'])
def test_failed_live_read_preserves_concurrently_armed_receipt(setup, monkeypatch, state):
  db, row, _ = setup
  opted_in(setup)
  confirmed = {'state': 'needs_you', 'summary': 'Actual public finding', 'head_sha': ITEM['head_sha'],
    'public_review': {'state': state, 'marker': 'other-worker', 'id': 7}}
  def fail(*args):
    with SessionLocal() as other:
      live = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, live, domain.key(ITEM), confirmed)
    raise HTTPException(409, 'Live read failed')
  monkeypatch.setattr(domain, 'current_pull', fail)
  report(setup, publish=True)
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)] == confirmed


@pytest.mark.parametrize('state', ['posting', 'unknown', 'posted'])
def test_private_report_waiting_for_admission_cannot_erase_public_receipt(setup, monkeypatch, state):
  db, row, _ = setup
  opted_in(setup)
  item_key = domain.key(ITEM)
  concurrent = {'state': 'needs_you', 'summary': 'Frozen public finding',
    'head_sha': ITEM['head_sha'], 'public_review': {'state': state, 'marker': 'other-worker'}}
  real_parent = routes._parent
  calls = 0
  def parent(session, run, principal):
    nonlocal calls
    calls += 1
    if calls == 2:
      # A second DB worker publishes while this private report waits for the
      # admission lock; _parent will refresh the owning row afterward.
      with SessionLocal() as writer:
        live = writer.get(models.ContributionReviewRun, row.id)
        domain.save_outcome(writer, live, item_key, concurrent)
    return real_parent(session, run, principal)
  monkeypatch.setattr(routes, '_parent', parent)
  with pytest.raises(HTTPException) as error:
    report(setup, state='needs_you', summary='Private owner question', publish=False)
  assert error.value.status_code == 409
  db.refresh(row)
  assert row.outcomes_json[item_key] == concurrent


def test_owner_question_stays_private_until_explicit_public_verdict(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  sent = []
  monkeypatch.setattr(public, 'post_review', lambda *args: sent.append(args[-1]) or public.DispatchResult.confirmed({'id': 7}))
  result = report(setup, state='needs_you', summary='Can I access the private fixture?', publish=False)
  assert result['run']['state'] == 'needs_you'
  assert not sent and 'public_review' not in row.outcomes_json[domain.key(ITEM)]
  report(setup, state='needs_you', summary='A concrete public correctness finding.', publish=True)
  assert len(sent) == 1 and 'private fixture' not in sent[0]


def test_route_preview_start_freeze_opt_in_and_publish_requires_explicit_signal(setup, monkeypatch):
  db, _old, principal = setup
  settle_fixture_review(db, _old)
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, 'resolve_round_choice', lambda db: {
    'provider': 'codex', 'model': 'gpt-5', 'effort': 'xhigh'})
  monkeypatch.setattr(domain, 'inspect_target', lambda *args: TARGET)
  async def admit(**kwargs):
    db.add(models.ChatRun(id='posted-review-run', chat_id=kwargs['chat_id'], status='running'))
    db.commit()
    return True
  monkeypatch.setattr(routes, 'start_programmatic_chat_turn', admit)
  option = routes.ReviewOptions(post_review=True)
  preview = routes.review_preview(1, routes.ReviewPreview(options=option), db, owner)
  private = routes.review_preview(1, routes.ReviewPreview(), db, owner)
  assert preview['options']['post_review'] is True
  assert 'publish:true' in preview['options']['mandatory_instructions']
  assert preview['preview_sha256'] != private['preview_sha256']
  start = routes.StartReviews(request_id='posted-opt-in', mode='review', items=[ITEM],
    options=option, preview_sha256=preview['preview_sha256'])
  with pytest.raises(HTTPException):
    asyncio.run(routes.start_reviews(1, start.model_copy(update={
      'options': routes.ReviewOptions(post_review=False)}), db, owner))
  selected = asyncio.run(routes.start_reviews(1, start, db, owner))
  row = db.get(models.ContributionReviewRun, selected['run']['id'])
  assert row.options_json['post_review'] is True
  assert row.options_json['input_options'] == {'post_review': True}
  assert 'publish:true' in selected['brief']
  reporter = Principal(owner=principal.owner, app_id=None, chat_id=row.chat_id,
    run_id='posted-review-run')
  calls = []
  monkeypatch.setattr(public, 'post_review', lambda *args: calls.append(args[-1]) or
    public.DispatchResult.confirmed({'id': 7}))
  def outcome(summary, publish):
    return routes.ReviewOutcome(**ITEM, state='needs_you', summary=summary, publish=publish)
  asyncio.run(routes.report_outcome(1, row.id, outcome('Private owner question', False), db, reporter))
  assert not calls and not row.outcomes_json[domain.key(ITEM)].get('public_review')
  asyncio.run(routes.report_outcome(1, row.id, outcome('Public correctness finding', True), db, reporter))
  assert len(calls) == 1 and 'Private owner question' not in calls[0]


@pytest.mark.parametrize('state', ['posting', 'unknown', 'posted'])
@pytest.mark.parametrize('app_id', [None, 1])
def test_public_review_fences_another_grant_and_names_holder(setup, monkeypatch, state, app_id):
  db, row, _ = setup
  opted_in(setup)
  holder = models.ContributionReviewRun(id='holding-run', app_id=app_id, owner_id=row.owner_id,
    request_id='another-request', mode='review', targets_json=deepcopy(row.targets_json),
    outcomes_json={domain.key(ITEM): {'state': 'all_clear', 'head_sha': ITEM['head_sha'],
      'public_review': {'state': state, 'marker': 'other-attempt'}}},
    chat_id=row.chat_id, github_actor_id='42', app_nonce=row.app_nonce if app_id else None)
  db.add(holder); db.commit()
  monkeypatch.setattr(public, 'post_review', lambda *args: pytest.fail('other grant already holds this head'))
  with pytest.raises(HTTPException) as error:
    report(setup, publish=True)
  assert error.value.status_code == 409
  assert holder.id in str(error.value.detail) and holder.chat_id in str(error.value.detail)
  assert domain.key(ITEM) not in row.outcomes_json


def test_public_receipt_does_not_duplicate_frozen_evidence(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  monkeypatch.setattr(public, 'post_review', lambda *args: public.DispatchResult.confirmed({'id': 7}))
  report(setup, publish=True)
  saved = row.outcomes_json[domain.key(ITEM)]
  assert saved['summary'] == 'Full diff reviewed'
  assert 'evidence' not in saved['public_review']
  with pytest.raises(HTTPException):
    report(setup, publish=True, summary='Changed finding')


@pytest.mark.parametrize("context_app,row_app,caller_app", [(1, 1, 1), (None, None, None), (1, 1, None)])
def test_owned_list_preserves_full_findings_and_prompt_snapshot(setup, context_app, row_app, caller_app):
  db, row, principal = setup
  row.app_id = row_app
  row.options_json = {"review_prompt": "Exact frozen prompt", "merge_prompt": "Exact merge prompt"}
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "needs_you",
    "summary": "s"*200000, "tests": "t"*200000})
  caller = principal if caller_app else Principal(owner=principal.owner, app_id=None)
  listed = routes.list_reviews(context_app, db, caller)["runs"][0]
  assert listed["items"][0]["summary"] == "s"*200000
  assert listed["items"][0]["tests"] == "t"*200000
  assert listed["options"]["review_prompt"] == "Exact frozen prompt"
  assert "summary_truncated" not in listed["items"][0]


def test_foreign_list_still_excludes_private_findings_and_prompts(setup):
  db, row, principal = setup
  row.app_id = None
  row.options_json = {"review_prompt": "PRIVATE PROMPT"}
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "needs_you", "summary": "PRIVATE FINDINGS"})
  listed = routes.list_reviews(1, db, principal)["runs"][0]
  assert listed["details_in_owning_chat"] is True
  assert "PRIVATE" not in str(listed) and "options" not in listed


def test_bounded_foreign_navigation_summary_has_explicit_omission(setup, monkeypatch):
  db, row, _ = setup
  row.app_id = None
  # The upstream privacy projection owns disclosure; this layer bounds only
  # already-approved navigation text, never reaches into private outcomes.
  monkeypatch.setattr(routes, "_run_view", lambda *a, **k: {
    "items": [{"summary": "Public navigation text " * 40, "state": "needs_you"}]})
  item = routes._list_run_view(db, row, 1)["items"][0]
  assert len(item["summary"]) <= 240
  assert item["summary"].endswith("… open this run for the full findings")
  assert item["summary_truncated"] is True


@pytest.mark.parametrize("error", [HTTPException(409, "revoked"), RuntimeError("authorization unavailable")])
def test_authorization_refusal_rolls_back_public_owner_fence(setup, error):
  from app import contribution_review_public_actions as actions
  db, row, _ = setup
  def authorize():
    assert db.in_transaction()
    raise error
  with pytest.raises(type(error)):
    actions.arm_public_review(db, row, domain.key(ITEM), TARGET, {}, "marker", authorize)
  assert not db.in_transaction()


def test_public_instructions_and_label_are_unambiguous():
  from app import contribution_review_presets as presets
  options = presets.resolve_options(None, {'post_review': True}, choice={'provider': 'codex', 'model': 'test', 'effort': None})
  instruction = options['mandatory_instructions']
  assert 'publish' in instruction and 'server' in instruction
  assert 'Never post comments or reviews' not in instruction
  text = public.review_comment_body({'summary': 'Finding', 'tests': 'Reviewer ran pytest', 'head_sha': ITEM['head_sha']})
  assert '**Verification:**' in text and '**Checks:**' not in text


@pytest.mark.parametrize('change', ['head', 'number', 'repo', 'not_dispatched'])
def test_other_head_or_proved_non_dispatch_does_not_fence_public_review(setup, monkeypatch, change):
  db, row, _ = setup
  opted_in(setup)
  target = {**ITEM}
  if change == 'head': target['head_sha'] = 'c' * 40
  if change == 'number': target['number'] += 1
  if change == 'repo': target['repo'] = 'other/repo'
  holder = models.ContributionReviewRun(id='nonblocking', app_id=None, owner_id=row.owner_id,
    request_id='different', mode='review', targets_json=[target],
    outcomes_json={domain.key(target): {'state': 'all_clear', 'head_sha': target['head_sha'],
      'public_review': {'state': 'not_dispatched' if change == 'not_dispatched' else 'unknown'}}},
    chat_id=row.chat_id, github_actor_id='42')
  db.add(holder); db.commit()
  sent = []
  monkeypatch.setattr(public, 'post_review', lambda *a: sent.append(1) or public.DispatchResult.confirmed({'id': 8}))
  report(setup, publish=True)
  assert sent == [1]


def test_public_holder_lookup_occurs_after_database_writer_fence(setup, monkeypatch):
  from app import contribution_review_public_actions as actions
  db, row, _ = setup
  opted_in(setup)
  actual_fence = actions.fence_owner
  def fence(session, owner_id):
    actual_fence(session, owner_id)
    session.add(models.ContributionReviewRun(id='fence-holder', app_id=None, owner_id=owner_id,
      request_id='at-fence', mode='review', targets_json=deepcopy(row.targets_json),
      outcomes_json={domain.key(ITEM): {'state': 'all_clear', 'head_sha': ITEM['head_sha'],
        'public_review': {'state': 'unknown'}}}, chat_id=row.chat_id, github_actor_id='42'))
    session.flush()
  monkeypatch.setattr(actions, 'fence_owner', fence)
  monkeypatch.setattr(public, 'post_review', lambda *a: pytest.fail('holder at write fence was missed'))
  with pytest.raises(HTTPException, match='fence-holder'):
    report(setup, publish=True)


def test_report_builder_preserves_transition_facts_without_mutating_saved_evidence(setup):
  db, row, principal = setup
  previous = {"state": "needs_you", "summary": "old", "public_review": {"state": "not_dispatched"}}
  body = routes.ReviewOutcome(**{**ITEM, "state": "needs_you", "summary": "new", "tests": "verified"})
  result = routes._reported_outcome(previous, body, row, principal)
  assert result == {**previous, "summary": "new", "scope": body.scope, "tests": "verified",
    "tests_passed": body.tests_passed, "reviewed_base_sha": body.reviewed_base_sha,
    "head_sha": body.head_sha, "review_chat_id": row.chat_id, "review_run_id": principal.run_id}
  assert previous["summary"] == "old"
