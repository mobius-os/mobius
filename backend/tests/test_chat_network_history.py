"""Chat history stays confined while paging through all retained mail."""
from app.chat_writer import create_chat
from datetime import datetime
import pytest
from app import models
from app.agent_coordination import chat_message_history


def test_chat_history_pages_sent_received_and_old_goal_broadcasts_only(db):
  db.add_all([create_chat(id=name, title=name, messages=[]) for name in ['self', 'peer', 'other']])
  db.flush()
  db.add(models.ChatRun(id='run-old', chat_id='self', root_run_id='old-goal', status='completed'))
  db.add(models.ChatRun(id='run-new', chat_id='self', root_run_id='new-goal', status='completed'))
  def note(id, sender, recipient, room='workspace', room_id='owner'):
    return models.AgentCoordinationMessage(id=id, from_chat_id=sender, to_chat_id=recipient,
      room_kind=room, room_id=room_id, body=id, kind='note', created_at=datetime(2026, 1, 1))
  db.add_all([
    note('a', 'self', 'peer'), note('b', 'peer', 'self'),
    note('c', 'peer', None, 'delegation', 'old-goal'),
    note('d', 'self', None, 'delegation', 'old-goal'),
    note('hidden-direct', 'peer', 'other', 'delegation', 'old-goal'),
    note('hidden-broadcast', 'peer', None, 'delegation', 'unrelated'),
  ])
  db.commit()
  first = chat_message_history(db, 'self', limit=2)
  assert first['total'] == 4
  assert first['sent'] == 2
  assert first['received'] == 2
  assert [row['id'] for row in first['messages']] == ['d', 'c']
  second = chat_message_history(db, 'self', before=first['next_before'], limit=2)
  assert [row['id'] for row in second['messages']] == ['b', 'a']
  assert second['next_before'] is None
  with pytest.raises(ValueError, match='invisible'):
    chat_message_history(db, 'self', before='hidden-direct')


def test_empty_chat_history(db):
  db.add(create_chat(id='empty', messages=[]))
  db.commit()
  page = chat_message_history(db, 'empty')
  assert page['total'] == 0
  assert page['messages'] == []
  assert page['next_before'] is None


def test_history_route_requires_authorized_chat_and_rejects_bad_cursor(client, auth, db):
  db.add(create_chat(id='history-route', messages=[]))
  db.commit()
  path = '/api/agent-coordination/chats/history-route/history'
  assert client.get(path).status_code == 401
  response = client.get(path, headers=auth)
  assert response.status_code == 200
  assert response.json()['messages'] == []
  assert client.get(path + '?before=unrelated', headers=auth).status_code == 422
  assert client.get(path + '?limit=101', headers=auth).status_code == 422
  assert client.get('/api/agent-coordination/chats/missing/history', headers=auth).status_code == 404


def _long_notes_fixture(db):
  db.add_all([create_chat(id=name, title=name, messages=[]) for name in ['long-self', 'long-peer', 'long-other']])
  db.flush()
  huge = 'Long finding ' + 'evidence ' * 50_000 + 'END'
  at_cap = 'c' * 4000
  rows = [
    models.AgentCoordinationMessage(id=f'huge-{index}', from_chat_id='long-peer', to_chat_id='long-self',
      room_kind='workspace', room_id='owner', body=huge, kind='finding',
      created_at=datetime(2026, 1, 1, 0, 0, index))
    for index in range(3)
  ] + [models.AgentCoordinationMessage(id='at-cap', from_chat_id='long-self', to_chat_id='long-peer',
      room_kind='workspace', room_id='owner', body=at_cap, kind='note',
      created_at=datetime(2026, 1, 1, 0, 1)),
    models.AgentCoordinationMessage(id='elsewhere', from_chat_id='long-peer', to_chat_id='long-other',
      room_kind='workspace', room_id='owner', body=huge, kind='note',
      created_at=datetime(2026, 1, 1, 0, 2))]
  db.add_all(rows)
  db.commit()
  return huge, at_cap


def test_owner_views_carry_only_a_long_notes_head_and_read_it_whole(client, auth, db):
  from app.context_budget import PEER_NOTE_CHARS

  huge, at_cap = _long_notes_fixture(db)
  history = client.get('/api/agent-coordination/chats/long-self/history', headers=auth)
  assert history.status_code == 200
  by_id = {row['id']: row for row in history.json()['messages']}
  assert len(history.content) < 4 * PEER_NOTE_CHARS + 10_000
  for index in range(3):
    row = by_id[f'huge-{index}']
    assert row['body'] == huge[:PEER_NOTE_CHARS]
    assert (row['truncated'], row['body_chars']) == (True, len(huge))
  # A note within the display limit is exactly as before.
  assert by_id['at-cap']['body'] == at_cap
  assert 'truncated' not in by_id['at-cap'] and 'body_chars' not in by_id['at-cap']

  activity = client.get('/api/chats/long-self/activity', headers=auth)
  assert activity.status_code == 200
  peers = {event['message_id']: event for event in activity.json()['events'] if event['type'] == 'peer_message'}
  assert peers['huge-0']['truncated'] is True and len(peers['huge-0']['body']) == PEER_NOTE_CHARS
  assert peers['at-cap']['body'] == at_cap and 'truncated' not in peers['at-cap']

  full = client.get('/api/agent-coordination/chats/long-self/messages/huge-0', headers=auth)
  assert full.status_code == 200
  assert full.json()['body'] == huge and 'truncated' not in full.json()
  # The read follows the chat's history visibility.
  assert client.get('/api/agent-coordination/chats/long-self/messages/elsewhere',
                    headers=auth).status_code == 404
  assert client.get('/api/agent-coordination/chats/long-other/messages/elsewhere',
                    headers=auth).json()['body'] == huge
  assert client.get('/api/agent-coordination/chats/long-self/messages/huge-0').status_code == 401


def test_history_never_loads_a_whole_body(db, monkeypatch):
  """Lists select the head in SQL; the ORM never materializes a long body."""
  from sqlalchemy import event

  huge, _ = _long_notes_fixture(db)
  seen = []
  engine = db.get_bind()

  def capture(_conn, _cursor, statement, parameters, _context, _many):
    seen.append(statement)

  event.listen(engine, 'before_cursor_execute', capture)
  try:
    page = chat_message_history(db, 'long-self')
  finally:
    event.remove(engine, 'before_cursor_execute', capture)
  assert all(len(row['body']) <= 4000 for row in page['messages'])
  selects = [statement for statement in seen
             if 'agent_coordination_messages.body' in statement and 'count(' not in statement]
  assert selects and all('substr(CAST(coalesce(' in statement
                         for statement in selects)


def test_a_stored_note_with_u0000_is_shown_whole_or_marked_truncated(client, auth, db):
  """SQLite's text functions stop at U+0000; heads are read as bytes instead."""
  from app.context_budget import PEER_NOTE_CHARS

  db.add_all([create_chat(id=name, title=name, messages=[]) for name in ['nul-self', 'nul-peer']])
  db.flush()
  short = 'before\x00after the NUL'
  long = 'a\x00' + 'é' * 9_000
  db.add_all([
    models.AgentCoordinationMessage(id=note_id, from_chat_id='nul-peer', to_chat_id='nul-self',
      room_kind='workspace', room_id='owner', body=body, kind='note',
      created_at=datetime(2026, 1, 1, 0, 0, index))
    for index, (note_id, body) in enumerate([('nul-short', short), ('nul-long', long)])
  ])
  db.commit()
  rows = {row['id']: row for row in chat_message_history(db, 'nul-self')['messages']}
  assert rows['nul-short']['body'] == short and 'truncated' not in rows['nul-short']
  assert rows['nul-long']['body'] == long[:PEER_NOTE_CHARS]
  assert rows['nul-long']['truncated'] is True and rows['nul-long']['body_chars'] > PEER_NOTE_CHARS
  full = client.get('/api/agent-coordination/chats/nul-self/messages/nul-long', headers=auth)
  assert full.json()['body'] == long


def test_a_note_body_with_u0000_is_refused(client, db):
  from app.agent_coordination import _clean_message

  with pytest.raises(ValueError, match='U\\+0000'):
    _clean_message('note', 'one\x00two')
