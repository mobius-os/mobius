"""Explicit card closure is an answer, not a synthetic model turn."""
import copy

import pytest

from app import chat as chat_mod, models, questions
from app.chat_writer import AppendPending, PersistTranscript, get_writer
from app.database import SessionLocal
from app.routes import chats_stream
from tests.test_owner_approvals import approval_run, _ask, _finish, _row, PROMPT


QUIET = copy.deepcopy(PROMPT)
QUIET['options'][0]['on_answer'] = 'close'


def _quiet(client, chat, auth, qid, **changes):
  body = {'content': '- Answer: Not now', 'hidden': True,
          'question_id': qid, 'answers': {QUIET['question']: 'Not now'},
          'selected_options': {'approval': ['0']}}
  body.update(changes)
  return client.post(f'/api/chats/{chat.id}/messages', headers=auth, json=body)


def _block(chat_id, qid):
  return next(block for message in _row(chat_id)[1] for block in message.get('blocks', [])
              if block.get('question_id') == qid)


@pytest.mark.parametrize('idle', [False, True])
def test_quiet_answer_saves_without_starting_or_queuing_a_turn(
    client, chat, auth, approval_run, monkeypatch, idle):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  if idle:
    _finish(chat, approval_run[0])
  before = _row(chat.id)[1]
  scheduled = []
  monkeypatch.setattr(chats_stream, '_schedule_continuation', lambda **kw: scheduled.append(kw))
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == 200, response.text
  assert response.json() == {'status': 'answered', 'answer_turn': 'none', 'running': not idle}
  marker, messages, pending = _row(chat.id)
  assert marker is None and pending == [] and scheduled == []
  assert len(messages) == len(before)
  assert _block(chat.id, qid)['answer_turn'] == 'none'
  assert _block(chat.id, qid)['selected_options'] == {'approval': ['0']}
  if not idle:
    _finish(chat, approval_run[0])
    assert _block(chat.id, qid)['answer_turn'] == 'none'
  assert _quiet(client, chat, auth, qid).status_code == 200


def test_quiet_retry_cannot_clear_newer_card(client, chat, auth, approval_run):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  assert _quiet(client, chat, auth, qid).status_code == 200
  other = {**QUIET, 'question': 'A different decision?'}
  newer = _ask(client, chat, approval_run, other).json()['question_id']
  assert newer != qid
  assert _quiet(client, chat, auth, qid).status_code == 200
  assert _row(chat.id)[0] == newer
  assert 'answers' not in _block(chat.id, newer)


def test_same_text_without_explicit_selection_still_resumes(client, chat, auth, approval_run):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  response = _quiet(client, chat, auth, qid, selected_options=None)
  assert response.status_code == 202, response.text
  assert response.json()['answer_turn'] == 'queued'
  assert len(_row(chat.id)[2]) == 1
  assert _quiet(client, chat, auth, qid).status_code == 409


@pytest.mark.parametrize('selection,answer', [({'approval': ['missing']}, 'Not now'),
    ({'wrong': ['0']}, 'Not now'), ({'approval': ['0', '0']}, 'Not now'),
    ({'approval': ['0']}, 'a custom answer')])
def test_invalid_selection_keeps_card_open(client, chat, auth, approval_run, selection, answer):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  response = _quiet(client, chat, auth, qid, selected_options=selection,
                    answers={QUIET['question']: answer})
  assert response.status_code == 409, response.text
  assert _row(chat.id)[0] == qid and _row(chat.id)[2] == []


def test_legacy_route_cannot_bypass_typed_card(client, chat, auth, approval_run):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  response = client.post(f'/api/chats/{chat.id}/question-answers', headers=auth,
      json={'question_id': qid, 'answers': {QUIET['question']: 'Not now'}})
  assert response.status_code == 409
  assert _row(chat.id)[0] == qid


def test_quiet_option_identity_minted_and_duplicate_labels_rejected(client, chat, approval_run):
  prompt = {'questions': [{'id': 'pick', 'header': 'Pick', 'question': 'Need more?',
      'options': [{'label': 'No', 'description': 'Done', 'on_answer': 'close'},
                  {'label': 'No', 'description': 'Continue'}]}]}
  response = client.post(f'/api/chats/{chat.id}/question', headers=approval_run[1], json=prompt)
  assert response.status_code == 422
  assert _row(chat.id)[0] is None
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  assert [option['id'] for option in _block(chat.id, qid)['questions'][0]['options']] == ['0', '1']


def _goal(chat, approval_run, status='pending'):
  with SessionLocal() as db:
    run = db.get(models.ChatRun, approval_run[0].run_token)
    run.goal_id = run.id
    run.goal_objective = 'Finish the repair'
    run.goal_plan_json = {'version': 1, 'tasks': [
      {'id': 'repair', 'title': 'Repair', 'status': status, 'depends_on': []}]}
    db.commit()


def test_quiet_answer_cannot_orphan_unfinished_goal(client, chat, auth, approval_run):
  _goal(chat, approval_run)
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == 409, response.text
  assert 'unfinished Goal' in response.text
  assert _row(chat.id)[0] == qid and _row(chat.id)[2] == []
  assert 'answers' not in _block(chat.id, qid)


def test_completed_plan_can_close_without_changing_goal(client, chat, auth, approval_run):
  _goal(chat, approval_run, 'completed')
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  assert _quiet(client, chat, auth, qid).status_code == 200
  with SessionLocal() as db:
    assert db.get(models.ChatRun, approval_run[0].run_token).goal_plan_json['tasks'][0]['status'] == 'completed'


@pytest.mark.parametrize('same_goal', [False, True])
def test_only_exact_goal_monitor_allows_quiet_closure(client, chat, auth, approval_run, monkeypatch, same_goal):
  from app import delegations
  _goal(chat, approval_run)
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  monkeypatch.setattr(delegations, 'background_helper_goal_ids', lambda *_: {
      approval_run[0].run_token if same_goal else 'another-goal'})
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == (200 if same_goal else 409), response.text


def test_mixed_card_and_native_questions_keep_normal_continuation():
  card = {'response_mode': 'continuation', 'questions': [
    {'id': 'a', 'question': 'A?', 'options': [{'id': '0', 'label': 'No', 'on_answer': 'close'}]},
    {'id': 'b', 'question': 'B?', 'options': [{'id': '0', 'label': 'Yes', 'on_answer': 'resume'}]},
  ]}
  assert not questions.closes_without_reply(card, {'A?': 'No', 'B?': 'Yes'}, {'a': ['0'], 'b': ['0']})
  assert not questions.closes_without_reply(card, {'A?': 'No', 'B?': 'Custom'}, {'a': ['0']})
  assert not questions.closes_without_reply({**card, 'response_mode': 'native'}, {'A?': 'No'}, {'a': ['0']})


@pytest.mark.parametrize('idle', [False, True])
def test_quiet_answer_releases_only_preexisting_followup_via_normal_queue(
    client, chat, auth, approval_run, monkeypatch, idle):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  get_writer().submit(AppendPending(chat_id=chat.id,
      user_msg={'role': 'user', 'content': 'B: queued follow-up', 'cid': 'followup-b'})).result(timeout=5)
  before = _row(chat.id)[2]
  if idle:
    _finish(chat, approval_run[0])
  scheduled = []
  monkeypatch.setattr(chats_stream, '_schedule_continuation', lambda **kw: scheduled.append(kw))
  monkeypatch.setattr(chat_mod, '_schedule_continuation', lambda **kw: scheduled.append(kw))
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == 200, response.text
  if not idle:
    assert scheduled == [] and _row(chat.id)[2] == before
    _finish(chat, approval_run[0])
  assert len(scheduled) == 1
  assert scheduled[0]['next_user']['content'] == 'B: queued follow-up'
  assert not scheduled[0]['next_user'].get('continuation_reason')
  assert _row(chat.id)[2] == []


def test_ordinary_followup_does_not_own_unfinished_goal(client, chat, auth, approval_run):
  _goal(chat, approval_run)
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  get_writer().submit(AppendPending(chat_id=chat.id,
      user_msg={'role': 'user', 'content': 'B unrelated', 'cid': 'b'})).result(timeout=5)
  before = _row(chat.id)[2]
  assert _quiet(client, chat, auth, qid).status_code == 409
  assert _row(chat.id)[0] == qid and _row(chat.id)[2] == before


def test_same_goal_continuation_owns_handoff(client, chat, auth, approval_run):
  _goal(chat, approval_run)
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  get_writer().submit(AppendPending(chat_id=chat.id, user_msg={
      'role': 'user', 'kind': 'continuation', 'content': 'Continue', 'cid': 'handoff',
      'continuation_reason': 'goal_handoff', 'goal_id': approval_run[0].run_token,
  })).result(timeout=5)
  assert _quiet(client, chat, auth, qid).status_code == 200


def test_failed_quiet_commit_keeps_card_open_for_identical_retry(
    client, chat, auth, approval_run, monkeypatch):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  writer = get_writer()
  original = writer._answer_question
  monkeypatch.setattr(writer, '_answer_question', lambda *_: (_ for _ in ()).throw(RuntimeError('test write failed')))
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == 503
  assert _row(chat.id)[0] == qid and _row(chat.id)[2] == []
  monkeypatch.setattr(writer, '_answer_question', original)
  assert _quiet(client, chat, auth, qid).status_code == 200


def test_stale_live_snapshot_cannot_erase_quiet_receipt(client, chat, auth, approval_run):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  stale = copy.deepcopy(_row(chat.id)[1][-1])
  assert _quiet(client, chat, auth, qid).status_code == 200
  # A runner may have seen the answer text but not the durable disposition.
  stale['blocks'][-1]['answers'] = {QUIET['question']: 'Not now'}
  get_writer().submit(PersistTranscript(chat_id=chat.id,
      run_token=approval_run[0].run_token, snapshot=stale)).result(timeout=5)
  with SessionLocal() as db:
    live = db.get(models.Chat, chat.id).live_assistant
    assert live['blocks'][-1]['answer_turn'] == 'none'
  _finish(chat, approval_run[0])
  assert _quiet(client, chat, auth, qid).status_code == 200


def test_legacy_unkeyed_answer_cannot_overwrite_quiet_receipt(client, chat, auth, approval_run):
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  assert _quiet(client, chat, auth, qid).status_code == 200
  response = client.post(f'/api/chats/{chat.id}/question-answers', headers=auth,
      json={'answers': {QUIET['question']: 'Rewrite decision'}})
  assert response.status_code == 409
  assert _block(chat.id, qid)['answers'] == {QUIET['question']: 'Not now'}


def test_quiet_close_after_stop_does_not_revive_goal_or_release_queued_b(
    client, chat, auth, approval_run, monkeypatch):
  from app.chat_writer import FinishRun
  _goal(chat, approval_run)
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  get_writer().submit(AppendPending(chat_id=chat.id,
      user_msg={'role': 'user', 'content': 'B', 'cid': 'b'})).result(timeout=5)
  get_writer().submit(FinishRun(chat_id=chat.id, run_token=approval_run[0].run_token,
      terminal_status='stopped')).result(timeout=5)
  before = _row(chat.id)[2]
  scheduled = []
  monkeypatch.setattr(chats_stream, '_schedule_continuation', lambda **kw: scheduled.append(kw))
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == 200, response.text
  assert scheduled == [] and _row(chat.id)[2] == before
  with SessionLocal() as db:
    run = db.get(models.ChatRun, approval_run[0].run_token)
    assert run.status == 'stopped' and run.goal_plan_json['tasks'][0]['status'] == 'pending'


def test_latest_physical_goal_stop_not_original_author_owns_quiet_closure(
    client, chat, auth, approval_run):
  from datetime import datetime, timedelta, UTC
  from app.chat_writer import FinishRun
  _goal(chat, approval_run)
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  root_id = approval_run[0].run_token
  get_writer().submit(FinishRun(chat_id=chat.id, run_token=root_id,
      terminal_status='completed')).result(timeout=5)
  with SessionLocal() as db:
    db.add(models.ChatRun(id='later-stopped', chat_id=chat.id, root_run_id=root_id,
        goal_id=root_id, goal_objective='Finish the repair', status='stopped',
        provider='codex', started_at=datetime.now(UTC) + timedelta(seconds=1)))
    db.commit()
  response = _quiet(client, chat, auth, qid)
  assert response.status_code == 200, response.text


def test_legacy_actor_checks_latest_card_at_write_time(client, chat, approval_run):
  from app.chat_writer import AnswerQuestion
  from app.questions import AnswerConflict
  # The route's previous read cannot authorize the actor's unkeyed target.
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  with pytest.raises(AnswerConflict):
    get_writer().submit(AnswerQuestion(chat_id=chat.id, legacy_save_only=True,
        answers={QUIET['question']: 'Not now'})).result(timeout=5)
  assert _row(chat.id)[0] == qid
  assert 'answers' not in _block(chat.id, qid)


def test_stop_winning_quiet_admission_keeps_answer_unwritten(
    client, chat, auth, approval_run, monkeypatch):
  from contextlib import asynccontextmanager
  from app import chat_queue
  from app.chat_writer import FinishRun, await_ack
  qid = _ask(client, chat, approval_run, QUIET).json()['question_id']
  gate = chat_queue.get_transition_lock

  @asynccontextmanager
  async def stop_first(chat_id):
    async with gate(chat_id):
      chat_mod.bump_run_generation(chat_id)
      await await_ack(get_writer().submit(FinishRun(chat_id=chat_id,
          run_token=approval_run[0].run_token, terminal_status='stopped')))
      yield

  monkeypatch.setattr(chat_queue, 'get_transition_lock', stop_first)
  assert _quiet(client, chat, auth, qid).status_code == 410
  assert 'answers' not in _block(chat.id, qid)
  assert _row(chat.id)[2] == []
