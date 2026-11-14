"""Legacy replay defaults and the explicit-public-verdict request contract."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException
from app import contribution_review_presets as presets, contribution_review_runs as domain
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM


@pytest.mark.parametrize('saved,requested', [
  ({'max_rounds': None}, {}),
  ({'post_review': True, 'max_rounds': None}, {'post_review': True}),
  ({'post_review': True, 'max_rounds': None, 'autopilot': False,
    'fix_prompt': presets.FIX_PROMPT}, {'post_review': True}),
])
def test_identical_replay_accepts_only_obsolete_default_options(setup, saved, requested):
  db, row, principal = setup
  row.mode = 'review'
  row.options_json = {'input_options': saved, **requested}
  db.commit()
  before = deepcopy(row.options_json)
  request = routes.StartReviews(request_id=row.request_id, mode='review', items=[ITEM],
    options=routes.ReviewOptions(**requested))
  result = asyncio.run(routes.start_reviews(1, request, db,
    Principal(owner=principal.owner, app_id=None)))
  assert result['run']['id'] == row.id
  db.refresh(row)
  assert row.options_json == before  # comparison only, not a consent migration


@pytest.mark.parametrize('difference', [
  {'max_rounds': 5}, {'autopilot': True}, {'autopilot': 0}, {'fix_prompt': 'Custom fix instructions'},
  {'review_prompt': 'Custom review instructions'}, {'post_review': False},
])
def test_replay_preserves_real_saved_option_differences(setup, difference):
  db, row, principal = setup
  row.mode = 'review'
  row.options_json = {'post_review': True, 'input_options': {'post_review': True, **difference}}
  db.commit()
  request = routes.StartReviews(request_id=row.request_id, mode='review', items=[ITEM],
    options=routes.ReviewOptions(post_review=True))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, request, db, Principal(owner=principal.owner, app_id=None)))
  assert error.value.status_code == 409
  assert 'different selection' in error.value.detail


@pytest.mark.parametrize('posting', [False, True])
def test_brief_opener_and_outcome_template_explain_optional_public_verdict(setup, posting):
  _, row, _ = setup
  row.mode = 'review'
  row.options_json = {'post_review': posting}
  text = domain.brief(row)
  if posting:
    assert 'report privately unless you publish a verdict' in text
    assert '"publish": false' in text
  else:
    assert 'Review this exact selected public PR batch privately.' in text
    assert '"publish": false' not in text


def test_a_verdict_run_tells_the_agent_to_propose_findings_not_keep_them_private(setup):
  """A proposal posts nothing until the owner submits it, so a verdict result must be proposed."""
  _, row, _ = setup
  row.mode = 'review'
  row.options_json = {'post_review': True, 'propose_verdict': True}
  text = domain.brief(row)
  assert 'set publish:true in /outcomes' in text and 'REQUEST_CHANGES proposal, never only a private note' in text
  assert 'nothing reaches GitHub until they submit it in Contribute' in text
  assert 'only for a deliberate public verdict' not in text
  assert 'report privately unless you publish a verdict' in text and '"publish": false' in text
  row.options_json = {'post_review': True}
  assert 'only for a deliberate public verdict' in domain.brief(row), 'a comment-only grant keeps its stricter rule'


def test_foreign_list_options_exclude_unused_round_limit(setup, monkeypatch):
  db, row, _ = setup
  row.app_id = None
  monkeypatch.setattr(routes, '_run_view', lambda *a, **kw: {
    'items': [], 'options': {'model': 'frozen-model', 'post_review': True, 'max_rounds': 5}})
  assert routes._list_run_view(db, row, 1)['options'] == {
    'model': 'frozen-model', 'post_review': True}


def test_replay_normalization_does_not_change_takeover_options(setup):
  _, row, _ = setup
  row.mode = 'review_fix_merge'
  saved = {'max_rounds': None, 'autopilot': False, 'fix_prompt': presets.FIX_PROMPT}
  row.options_json = {'input_options': saved}
  assert routes._saved_options_input(row) == saved
