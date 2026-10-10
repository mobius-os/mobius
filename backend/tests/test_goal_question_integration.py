"""Goal briefs and helper questions share exact ownership and result delivery."""
import asyncio

from app import chat_steering, delegations, models
import app.chat as chat_mod
from tests.test_delegation_questions import _seed, _ask, _settle, _helper_token


def test_parallel_questions_share_one_live_carrier_without_duplicate_steers(
  client, owner_token, db, monkeypatch,
):
  first = _seed(db, 'parallel-a', provider='codex', parent_status='running')
  second = _seed(db, 'parallel-b', provider='codex', parent_id=first.parent_chat_id,
                 parent_root=first.parent_root_run_id)
  questions = []
  for row, suffix in ((first, 'parallel-a'), (second, 'parallel-b')):
    receipt = _ask(client, db, row, f'ask-{suffix}', question=f'Decision for {suffix}?')
    assert receipt.status_code == 200
    questions.append(receipt.json()['question_id'])
    _settle(db, row.child_chat_id, f'ask-{suffix}')
  monkeypatch.setattr(chat_mod, 'is_chat_running', lambda cid: cid == first.parent_chat_id)
  monkeypatch.setattr(chat_steering, 'has_live_steerable_turn', lambda *_: True)
  steers = []

  async def capture(*args):
    steers.append(args)
    return True

  monkeypatch.setattr(chat_steering, 'steer_into_active_turn', capture)
  assert asyncio.run(delegations.steer_results_into_running_parent(
    first.parent_chat_id, first.parent_root_run_id)) is True
  assert asyncio.run(delegations.steer_results_into_running_parent(
    first.parent_chat_id, first.parent_root_run_id)) is False
  assert len(steers) == 1
  content = steers[0][2]
  assert all(qid in content for qid in questions)
  assert content.count('"status":"needs_input"') == 2
  db.expire_all()
  queued = db.get(models.Chat, first.parent_chat_id).pending_messages
  assert len(queued) == 1 and queued[0]['hidden'] is True
  # Queued is not delivered: a failed steer must not lose the question.
  assert all(db.get(models.Delegation, row.id).delivered_run_id is None
             for row in (first, second))


def test_nested_read_goal_keeps_assignment_and_exact_child_question_together(
  client, owner_token, db,
):
  parent = _seed(db, 'brief-parent')
  root = db.get(models.ChatRun, parent.parent_root_run_id)
  goal = models.ChatGoal(id='question-goal', chat_id=parent.parent_chat_id,
      status='open', objective='Finish both branches', plan_json={'tasks': [
        {'id': 'build', 'title': 'Build', 'status': 'running', 'depends_on': []}]})
  db.add(goal)
  root.goal_id = goal.id
  # Spawned helpers retain immutable Goal ownership, not the run's mutable link.
  parent.goal_id = goal.id
  parent.goal_task_id = 'build'
  db.commit()
  child = _seed(db, 'brief-child', parent_id=parent.child_chat_id,
                parent_root='ask-brief-parent')
  child.goal_id = goal.id
  db.commit()
  receipt = _ask(client, db, child, 'ask-brief-child', question='Which format?')
  _settle(db, child.child_chat_id, 'ask-brief-child')
  token = _helper_token(db, parent, 'ask-brief-parent')
  response = client.get(f'/api/chats/{parent.child_chat_id}/goal-brief',
      headers={'Authorization': f'Bearer {token}'})
  assert response.status_code == 200, response.text
  brief = response.json()
  assert brief['goal']['id'] == goal.id
  assert brief['goal']['assignment']['plan_task'] == 'build'
  assert brief['helpers'] == [{'id': child.id, 'task_key': child.task_key,
      'status': 'needs_input', 'question': {'id': receipt.json()['question_id'],
      'question': 'Which format?', 'options': []}}]
  assert 'outcome_contract' not in brief['goal']
