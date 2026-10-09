"""Follow-ups retain immutable task ownership and must have a delivery owner."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app import chat_queue, delegations, models, transcript_rows
from app.chat_writer import create_chat
from app.deps import Principal
from app.routes.delegations import DelegationMessage, message_delegation
from app.timeutil import now_naive_utc
from tests.test_delegation_questions import _answer, _seed, _settle


def _finished_helper(db, *, goal_status=None, legacy=False, nested=False):
  row = _seed(db, suffix='followup-owner')
  _settle(db, row.child_chat_id, 'ask-followup-owner', text='First result.')
  row.delivered_run_id = 'ask-followup-owner'
  row.incorporated_run_id = 'ask-followup-owner'
  row.notify_parent_on_complete = False
  goal = None
  if goal_status is not None:
    owner = row.parent_chat_id
    if nested:
      owner = 'coordinator'
      db.add(create_chat(id=owner, title='Coordinator', provider='claude', messages=[]))
      db.flush()
    goal = models.ChatGoal(
      id=row.parent_root_run_id, chat_id=owner, objective='Original task',
      status=goal_status,
    )
    db.add(goal)
    db.get(models.ChatRun, row.parent_root_run_id).goal_id = goal.id
    row.goal_id = None if legacy else goal.id
  db.commit()
  return row, goal


@pytest.mark.parametrize('goal_status', ['completed', 'cannot_complete', 'cancelled', 'stopped'])
@pytest.mark.parametrize('legacy', [False, True])
def test_followup_cannot_start_under_closed_original_goal(client, owner_token, db, monkeypatch, goal_status, legacy):
  row, goal = _finished_helper(db, goal_status=goal_status, legacy=legacy)
  # A new active parent turn must not lend its authority to the closed task.
  db.add(models.ChatRun(id='fresh-cleanup', root_run_id='fresh-cleanup',
                       chat_id=row.parent_chat_id, status='running', provider='claude'))
  db.commit()
  original = list(transcript_rows.history(db.get(models.Chat, row.child_chat_id)))
  launches = []

  async def launch(**kwargs):
    launches.append(kwargs)
    return True

  monkeypatch.setattr('app.chat_start.start_programmatic_chat_turn', launch)
  response = _answer(client, owner_token, row, 'New cleanup task.')
  assert response.status_code == 409, response.text
  assert response.json()['detail']['reason'] == 'goal_closed'
  assert 'spawn_agent' in response.json()['detail']['message']
  assert launches == []
  db.expire_all()
  row = db.get(models.Delegation, row.id)
  assert row.goal_id == (None if legacy else goal.id)
  assert row.parent_root_run_id == goal.id
  assert not row.notify_parent_on_complete
  assert row.delivered_run_id == row.incorporated_run_id == 'ask-followup-owner'
  assert list(transcript_rows.history(db.get(models.Chat, row.child_chat_id))) == original
  assert db.query(models.ChatRun).filter_by(chat_id=row.child_chat_id).count() == 1


@pytest.mark.parametrize('source_status', ['stopped', 'failed', 'interrupted'])
def test_followup_cannot_bypass_parent_stop_or_failure(client, owner_token, db, monkeypatch, source_status):
  row, _ = _finished_helper(db)
  db.get(models.ChatRun, row.parent_root_run_id).status = source_status
  db.commit()
  launches = []

  async def launch(**kwargs):
    launches.append(kwargs)
    return True

  monkeypatch.setattr('app.chat_start.start_programmatic_chat_turn', launch)
  response = _answer(client, owner_token, row, 'Continue.')
  assert response.status_code == 409
  assert response.json()['detail']['reason'] == 'parent_not_waiting'
  assert not launches


def test_followup_cannot_attach_stopped_plain_work_to_a_new_owner_turn(client, owner_token, db):
  row, _ = _finished_helper(db)
  db.get(models.ChatRun, row.parent_root_run_id).status = 'stopped'
  db.add(models.ChatRun(id='fresh-owner', root_run_id='fresh-owner', chat_id=row.parent_chat_id,
                       status='running', provider='claude'))
  db.commit()
  response = _answer(client, owner_token, row, 'A new task.')
  assert response.status_code == 409
  assert response.json()['detail']['reason'] == 'source_stopped'


@pytest.mark.parametrize('goal_status,nested', [(None, False), ('open', False), ('open', True)])
def test_followup_of_owned_work_keeps_session_and_delivers_failure(client, owner_token, db, monkeypatch, goal_status, nested):
  row, _ = _finished_helper(db, goal_status=goal_status, nested=nested)
  old_session = db.get(models.Chat, row.child_chat_id).session_id
  launches = []

  async def run_chat(*args, **kwargs):
    launches.append(kwargs)

  monkeypatch.setattr('app.chat_start.run_chat', run_chat)
  response = _answer(client, owner_token, row, 'Review the same work again.')
  assert response.status_code == 202, response.text
  assert len(launches) == 1
  db.expire_all()
  assert db.get(models.Chat, row.child_chat_id).session_id == old_session
  assert delegations.serialize_background_helpers(db, row.parent_chat_id)['count'] == 1
  _settle(db, row.child_chat_id, launches[0]['run_token'], status='failed',
          text='DELEGATION_WRITE_REVIEW_REQUIRED: dispatch failed')
  db.expire_all()
  row = db.get(models.Delegation, row.id)
  assert delegations.derived_status(db, row)[0] == 'needs_review'
  assert delegations.serialize_background_helpers(db, row.parent_chat_id)['count'] == 1
  source = delegations.delegation_source_work_id(row)
  root = delegations._parent_wake_continuation_root(db, row.parent_chat_id, source)
  assert delegations.parent_wake_blocker(db, row.parent_chat_id, source, root)[0] is None
  assert not delegations.current_result_delivered(db, row)


@pytest.mark.parametrize('change', ['goal', 'parent'])
def test_followup_rechecks_owner_after_waiting_for_ancestor_lock(client, owner_token, db, monkeypatch, change):
  row, goal = _finished_helper(db, goal_status='open', nested=True)
  seen = []
  launches = []

  @asynccontextmanager
  async def transition(chat_id):
    seen.append(chat_id)
    if chat_id == row.child_chat_id:
      if change == 'goal':
        db.get(models.ChatGoal, goal.id).status = 'completed'
      else:
        db.get(models.ChatRun, row.parent_root_run_id).status = 'stopped'
      db.commit()
    yield

  async def launch(**kwargs):
    launches.append(kwargs)
    return True

  monkeypatch.setattr(chat_queue, 'get_transition_lock', transition)
  monkeypatch.setattr('app.chat_start.start_programmatic_chat_turn', launch)
  response = _answer(client, owner_token, row, 'Do more.')
  assert response.status_code == 409, response.text
  assert not launches
  assert seen == ['coordinator', row.parent_chat_id, row.child_chat_id]


def test_followup_requires_a_recoverable_original_source(client, owner_token, db):
  row, _ = _finished_helper(db)
  db.delete(db.get(models.ChatRun, row.parent_root_run_id))
  db.commit()
  response = _answer(client, owner_token, row, 'More work.')
  assert response.status_code == 409
  assert response.json()['detail']['reason'] == 'source_missing'


@pytest.mark.parametrize('parent_state', ['cancelled', 'interrupted'])
def test_nested_followup_cannot_bypass_its_parent_delegation(client, owner_token, db, parent_state):
  row, _ = _finished_helper(db, goal_status='open', nested=True)
  db.add(models.Delegation(
    id='ancestor', parent_chat_id='coordinator', parent_root_run_id='coordinator-root',
    goal_id=row.goal_id, child_chat_id=row.parent_chat_id, task_key='ancestor',
    provider='claude', scope='write', cwd='/data', prompt_sha256='fixture',
    cancelled_at=now_naive_utc() if parent_state == 'cancelled' else None,
    interrupted_at=now_naive_utc() if parent_state == 'interrupted' else None,
  ))
  db.commit()
  response = _answer(client, owner_token, row, 'More work.')
  assert response.status_code == 409
  assert response.json()['detail']['reason'] == (
    'parent_cancelled' if parent_state == 'cancelled' else 'legacy_parent_interrupted'
  )


def test_app_can_follow_up_its_own_helper_while_parent_is_idle(client, owner_token, db, monkeypatch):
  from test_app_fixtures import create_local_app

  row, _ = _finished_helper(db)
  app_id = create_local_app(client, {'Authorization': f'Bearer {owner_token}'}, name='Task caller')['id']
  row.app_id = app_id
  db.get(models.Chat, row.child_chat_id).created_by_app_id = app_id
  db.commit()
  launches = []

  async def launch(**kwargs):
    launches.append(kwargs)
    return True

  monkeypatch.setattr('app.chat_start.start_programmatic_chat_turn', launch)
  app_token = client.post('/api/auth/app-token', json={'app_id': app_id},
                          headers={'Authorization': f'Bearer {owner_token}'}).json()['token']
  response = _answer(client, app_token, row, 'Review the same app task.')
  assert response.status_code == 202, response.text
  assert launches[0]['initiated_by_app_id'] == app_id


def test_followup_serializes_admission_with_goal_owner_and_parent(client, owner_token, db, monkeypatch):
  from app.database import SessionLocal

  row, _ = _finished_helper(db, goal_status='open', nested=True)
  principal = Principal(owner=db.query(models.Owner).one(), app_id=None, scope='owner')

  async def race():
    starting = asyncio.Event()
    release = asyncio.Event()
    acquired = []

    async def launch(**kwargs):
      starting.set()
      await release.wait()
      return True

    monkeypatch.setattr('app.chat_start.start_programmatic_chat_turn', launch)

    async def followup():
      with SessionLocal() as session:
        return await message_delegation(row.id, DelegationMessage(message='Review again.'),
                                        principal=principal, db=session)

    async def owner_change(chat_id):
      async with chat_queue.get_transition_lock(chat_id):
        acquired.append(chat_id)

    work = asyncio.create_task(followup())
    await starting.wait()
    changes = [asyncio.create_task(owner_change(chat_id))
               for chat_id in ('coordinator', row.parent_chat_id)]
    await asyncio.sleep(0)
    assert acquired == []
    release.set()
    await work
    await asyncio.gather(*changes)
    assert set(acquired) == {'coordinator', row.parent_chat_id}

  asyncio.run(asyncio.wait_for(race(), timeout=5))


def test_owner_stop_winning_admission_race_prevents_provider_launch(client, owner_token, db, monkeypatch):
  from fastapi import HTTPException
  from app import chat
  from app.database import SessionLocal

  row, _ = _finished_helper(db)
  db.get(models.ChatRun, row.parent_root_run_id).status = 'running'
  db.commit()
  principal = Principal(owner=db.query(models.Owner).one(), app_id=None, scope='owner')
  original_stop = chat._stop_chat_for_locked
  launches = []

  async def race():
    stopping = asyncio.Event()
    release = asyncio.Event()

    async def held_stop(*args, **kwargs):
      stopping.set()
      await release.wait()
      return await original_stop(*args, **kwargs)

    async def launch(**kwargs):
      launches.append(kwargs)
      return True

    monkeypatch.setattr(chat, '_stop_chat_for_locked', held_stop)
    monkeypatch.setattr('app.chat_start.start_programmatic_chat_turn', launch)

    async def followup():
      with SessionLocal() as session:
        return await message_delegation(row.id, DelegationMessage(message='Review again.'),
                                        principal=principal, db=session)

    stop = asyncio.create_task(chat.stop_chat(row.parent_chat_id, actor='owner'))
    await stopping.wait()
    work = asyncio.create_task(followup())
    await asyncio.sleep(0)
    assert not work.done()
    assert launches == []
    release.set()
    assert (await stop)[0] is True
    with pytest.raises(HTTPException) as error:
      await work
    assert error.value.status_code == 409
    assert error.value.detail['reason'] == 'parent_not_waiting'

  asyncio.run(asyncio.wait_for(race(), timeout=10))
  db.expire_all()
  assert db.get(models.ChatRun, row.parent_root_run_id).status == 'stopped'
  assert db.query(models.ChatRun).filter_by(chat_id=row.child_chat_id).count() == 1
  assert launches == []
