"""Browser initiator attribution survives provider admission and queue boundaries."""
from app import transcript_rows
from app.chat_writer import create_chat

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import models
from app.browser_access import revoke_grant
from app.delegations import DelegationIntent, create_or_attach_delegation
from app.routes.delegations import _require_guest_child_lineage
from app.chat_writer import (
  _PersistFailed, _require_browser_grant, _root_browser_lineage,
)
from app.routes.chats_stream import _browser_may_steer_run
from app.schema_migrations import _add_chat_run_browser_lineage
from tests.browser_access_fixtures import link_grant


def test_chat_run_lineage_migration_is_idempotent_on_existing_table(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'lineage.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY)"))
    conn.execute(text("CREATE TABLE delegations (id VARCHAR(64) PRIMARY KEY)"))
    conn.execute(text("INSERT INTO chat_runs (id) VALUES ('old')"))
  _add_chat_run_browser_lineage(eng)
  _add_chat_run_browser_lineage(eng)
  columns = {c['name'] for c in inspect(eng).get_columns('chat_runs')}
  assert {'browser_grant_id', 'browser_grant_epoch'} <= columns
  assert {'browser_grant_id', 'browser_grant_epoch'} <= {
    c['name'] for c in inspect(eng).get_columns('delegations')
  }
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT browser_grant_id, browser_grant_epoch FROM chat_runs WHERE id='old'"
    )).one() == (None, None)


def test_run_lineage_revalidation_and_guest_steer_boundary(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'guest')
    chat = create_chat(id='chat')
    db.add(chat)
    db.add(models.ChatRun(
      id='run', chat_id='chat', root_run_id='run', status='running',
      browser_grant_id=grant.id,
    ))
    db.commit()
    _require_browser_grant(db, grant.id)
    assert _root_browser_lineage(db, 'run') == grant.id
    guest = SimpleNamespace(browser_grant_id=grant.id)
    other = SimpleNamespace(browser_grant_id='other')
    owner_principal = SimpleNamespace(browser_grant_id=None)
    assert _browser_may_steer_run(db, 'chat', guest)
    assert not _browser_may_steer_run(db, 'chat', other)
    assert _browser_may_steer_run(db, 'chat', owner_principal)
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(_PersistFailed):
      _require_browser_grant(db, grant.id)
    with pytest.raises(_PersistFailed):
      _root_browser_lineage(db, 'run')
    with pytest.raises(_PersistFailed):
      _root_browser_lineage(db, 'missing')


@pytest.mark.asyncio
async def test_revoke_stop_rechecks_current_run_and_preserves_queue(tmp_path, monkeypatch):
  from sqlalchemy.orm import sessionmaker
  import app.chat as chat_runtime
  import app.database as database

  eng = create_engine(f"sqlite:///{tmp_path / 'stop.db'}")
  models.Base.metadata.create_all(eng)
  factory = sessionmaker(bind=eng)
  monkeypatch.setattr(database, 'SessionLocal', factory)
  with factory() as db:
    db.add(create_chat(id='guest-chat'))
    db.add(create_chat(id='owner-chat'))
    db.add(models.ChatRun(
      id='guest-run', chat_id='guest-chat', status='running',
      browser_grant_id='grant',
    ))
    db.add(models.ChatRun(
      id='old-guest-run', chat_id='owner-chat', status='completed',
      browser_grant_id='grant',
    ))
    db.add(models.ChatRun(
      id='new-owner-run', chat_id='owner-chat', status='running',
    ))
    db.commit()
  # A stale candidate from a pre-lock snapshot must not stop the owner run.
  monkeypatch.setattr(chat_runtime, 'browser_grant_active_chat_ids',
                      lambda db, grant_id: ['guest-chat', 'owner-chat'])
  calls = []

  monkeypatch.setattr(chat_runtime, 'get_writer', lambda: SimpleNamespace(
    submit=lambda command: command,
  ))

  async def fake_ack(command):
    return {'rejected': 0}

  monkeypatch.setattr(chat_runtime, '_await_ack', fake_ack)
  # Simulate the writer's post-stop durable closure for the guest run.
  async def closing_stop(chat_id, db=None, **kwargs):
    calls.append((chat_id, kwargs))
    with factory() as session:
      session.get(models.ChatRun, 'guest-run').status = 'stopped'
      session.commit()
    return True, []

  monkeypatch.setattr(chat_runtime, '_stop_chat_for_locked', closing_stop)
  assert await chat_runtime.stop_browser_grant_runs('grant') == {
    'stopped_chat_ids': ['guest-chat'], 'rejected_messages': 0,
  }
  assert calls == [('guest-chat', {'preserve_pending': True})]
  with factory() as session:
    session.get(models.ChatRun, 'guest-run').status = 'running'
    session.commit()

  async def incomplete_stop(chat_id, db=None, **kwargs):
    return False, []

  monkeypatch.setattr(chat_runtime, '_stop_chat_for_locked', incomplete_stop)
  with pytest.raises(HTTPException) as error:
    await chat_runtime.stop_browser_grant_runs('grant')
  assert error.value.status_code == 503
  assert error.value.detail['code'] == 'browser_grant_stop_incomplete'


def test_revoked_guest_head_is_terminal_without_blocking_owner(tmp_path):
  from app.chat_writer import ChatWriterActor, PromotePending
  from app.chat import _pending_head_is_stale

  eng = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'guest')
    chat = create_chat(id='mixed-chat', provider='claude')
    guest = {
      'role': 'user', 'content': 'guest text', 'cid': 'guest-cid', 'ts': 1,
      '_browser_grant_id': grant.id
    }
    owner_message = {
      'role': 'user', 'content': 'owner text', 'cid': 'owner-cid', 'ts': 2,
    }
    chat.pending_messages = [guest, owner_message]
    db.add(chat)
    db.commit()
    revoke_grant(db, grant.id, owner.id)
    actor = ChatWriterActor(session_factory=lambda: db)
    result = actor._promote_pending(db, PromotePending(
      chat_id=chat.id, run_token='owner-run',
    ))
    assert result['promoted']['content'] == 'owner text'
    db.expire(chat)
    assert [row['content'] for row in list(transcript_rows.history(chat))] == ['owner text']
    assert len(chat.pending_messages) == 1
    rejected = chat.pending_messages[0]
    assert rejected['content'] == 'guest text'
    assert rejected['cid'] == 'guest-cid'
    assert rejected['delivery_status'] == 'rejected'
    assert rejected['delivery_error'] == 'browser_grant_unavailable'
    assert db.get(models.ChatRun, 'owner-run').browser_grant_id is None
    assert not _pending_head_is_stale(chat.pending_messages, 1_000_000)


def test_explicit_rejection_keeps_owner_queue_bytes(tmp_path):
  from app.chat_writer import ChatWriterActor, RejectBrowserPending

  eng = create_engine(f"sqlite:///{tmp_path / 'reject.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    guest = {'role': 'user', 'content': 'private attempt', 'cid': 'guest',
             'ts': 1, '_browser_grant_id': 'revoked-grant'}
    owner = {'role': 'user', 'content': 'owner must continue', 'cid': 'owner',
             'ts': 2}
    db.add(create_chat(id='chat', pending_messages=[guest, owner]))
    db.commit()
    actor = ChatWriterActor(session_factory=lambda: db)
    assert actor._reject_browser_pending(db, RejectBrowserPending(
      chat_id='chat', browser_grant_id='revoked-grant',
    )) == {'rejected': 1}
    db.expire_all()
    rows = db.get(models.Chat, 'chat').pending_messages
    assert rows[0]['content'] == 'private attempt'
    assert rows[0]['delivery_status'] == 'rejected'
    assert rows[1] == owner
    assert actor._reject_browser_pending(db, RejectBrowserPending(
      chat_id='chat', browser_grant_id='revoked-grant',
    )) == {'rejected': 0}


def test_revoked_grant_cannot_commit_start_turn_after_launch_race(tmp_path):
  from app.chat_writer import ChatWriterActor, StartTurn

  eng = create_engine(f"sqlite:///{tmp_path / 'launch.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.add(create_chat(id='chat'))
    db.commit()
    grant, _ = link_grant(db, owner, 'guest')
    revoke_grant(db, grant.id, owner.id)
    actor = ChatWriterActor(session_factory=lambda: db)
    with pytest.raises(_PersistFailed):
      actor._start_turn(db, StartTurn(
        chat_id='chat', run_token='late-run',
        user_msg={'role': 'user', 'content': 'late', 'ts': 1, 'cid': 'late'},
        browser_grant_id=grant.id,
      ))
    db.rollback()
    assert db.get(models.ChatRun, 'late-run') is None
    assert list(transcript_rows.history(db.get(models.Chat, 'chat'))) == []


def test_guest_submitted_child_under_owner_run_retains_guest_lineage(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'delegation.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add_all((owner, create_chat(id='parent')))
    db.add(models.ChatRun(id='owner-run', chat_id='parent', status='running',
                          root_run_id='owner-run'))
    db.commit()
    grant, _ = link_grant(db, owner, 'guest')
    intent = DelegationIntent(app_id=None, parent_chat_id='parent',
      parent_root_run_id='owner-run', task_key='guest-work', prompt='work',
      provider='codex', model='gpt-5', effort='medium', cwd='/data',
      browser_grant_id=grant.id)
    row, attached = create_or_attach_delegation(db, intent)
    assert not attached
    assert row.browser_grant_id == grant.id
    from dataclasses import replace
    observed, attached = create_or_attach_delegation(db, replace(
      intent, browser_grant_id=None))
    assert attached and observed.browser_grant_id == grant.id
    other, _ = link_grant(db, owner, 'other guest')
    with pytest.raises(ValueError, match='browser authority'):
      create_or_attach_delegation(db, replace(intent, browser_grant_id=other.id))
    revoke_grant(db, grant.id, owner.id)
    with pytest.raises(HTTPException):
      create_or_attach_delegation(db, intent)


def test_guest_cannot_restart_owner_or_other_guest_child():
  owner_child = SimpleNamespace(browser_grant_id=None)
  other_child = SimpleNamespace(browser_grant_id='other')
  own_child = SimpleNamespace(browser_grant_id='guest')
  guest = SimpleNamespace(browser_grant_id='guest')
  for child in (owner_child, other_child):
    with pytest.raises(HTTPException) as exc:
      _require_guest_child_lineage(child, guest)
    assert exc.value.status_code == 403
  _require_guest_child_lineage(own_child, guest)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_grant", [None, "other"])
@pytest.mark.parametrize("operation", ["messages", "retry"])
async def test_guest_followup_routes_reject_foreign_lineage_before_start(monkeypatch, child_grant, operation):
  from app.routes import delegations as routes
  row = SimpleNamespace(browser_grant_id=child_grant)
  principal = SimpleNamespace(browser_grant_id="guest")
  monkeypatch.setattr(routes, "_row_for_principal", lambda *args: row)
  with pytest.raises(HTTPException) as denied:
    if operation == "messages":
      await routes.message_delegation("child", routes.DelegationMessage(message="follow up"),
                                      principal=principal, db=None)
    else:
      await routes.retry_delegation("child", routes.DelegationRetry(run_token="parked"),
                                    principal=principal, db=None)
  assert denied.value.status_code == 403


@pytest.mark.parametrize('revoked', [False, True])
def test_exact_goal_resume_inherits_target_grant_not_intervening_owner_run(tmp_path, revoked):
  from datetime import UTC, datetime, timedelta
  from app.chat_writer import ChatWriterActor, StartTurn

  eng = create_engine(f"sqlite:///{tmp_path / 'goal-resume.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'guest')
    base = datetime.now(UTC)
    db.add(create_chat(id='chat', messages=[{'role': 'user', 'content': 'Work', 'ts': 1}]))
    db.add(models.ChatGoal(id='goal', chat_id='chat', objective='Finish work', status='stopped', revision=3))
    db.add(models.ChatRun(
      id='target', chat_id='chat', root_run_id='target', status='completed',
      goal_id='goal', goal_objective='Finish work', started_at=base,
      browser_grant_id=grant.id,
    ))
    db.add(models.ChatRun(
      id='intervening', chat_id='chat', root_run_id='intervening', status='completed',
      started_at=base + timedelta(seconds=1),
    ))
    db.commit()
    if revoked:
      revoke_grant(db, grant.id, owner.id)
    actor = ChatWriterActor(session_factory=lambda: db)
    command = StartTurn(chat_id='chat', run_token='resumed',
      user_msg={'kind': 'continuation', 'continuation_reason': 'manual', 'cid': 'resume'},
      resume_goal_id='goal', resume_goal_revision=3)
    if revoked:
      with pytest.raises(_PersistFailed):
        actor._start_turn(db, command)
      assert db.get(models.ChatRun, 'resumed') is None
      assert db.get(models.ChatGoal, 'goal').status == 'stopped'
    else:
      actor._start_turn(db, command)
      run = db.get(models.ChatRun, 'resumed')
      assert run.browser_grant_id == grant.id
      assert run.root_run_id == 'target' and run.goal_id == 'goal'


def test_guest_cannot_resume_retained_owner_goal_through_its_intervening_run(tmp_path):
  from datetime import UTC, datetime, timedelta
  from app.chat_writer import ChatWriterActor, StartTurn

  eng = create_engine(f"sqlite:///{tmp_path / 'foreign-goal.db'}")
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username='owner', hashed_password='unused')
    db.add(owner)
    db.commit()
    grant, _ = link_grant(db, owner, 'guest')
    base = datetime.now(UTC)
    db.add(create_chat(id='chat'))
    db.add(models.ChatGoal(id='goal', chat_id='chat', objective='Owner work', status='stopped', revision=3))
    db.add(models.ChatRun(id='target', chat_id='chat', root_run_id='target', status='completed',
      goal_id='goal', goal_objective='Owner work', started_at=base))
    db.add(models.ChatRun(id='intervening', chat_id='chat', status='completed',
      started_at=base + timedelta(seconds=1), browser_grant_id=grant.id))
    db.commit()
    actor = ChatWriterActor(session_factory=lambda: db)
    with pytest.raises(_PersistFailed, match='Browser grant cannot resume a foreign run'):
      actor._start_turn(db, StartTurn(chat_id='chat', run_token='resumed',
        user_msg={'kind': 'continuation', 'continuation_reason': 'manual', 'cid': 'resume'},
        browser_grant_id=grant.id,
        resume_goal_id='goal', resume_goal_revision=3))
    assert db.get(models.ChatRun, 'resumed') is None
    assert db.get(models.ChatGoal, 'goal').status == 'stopped'
