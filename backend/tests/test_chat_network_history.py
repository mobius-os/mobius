"""Chat history stays confined while paging through all retained mail."""
from datetime import datetime
import pytest
from app import models
from app.agent_coordination import chat_message_history


def test_chat_history_pages_sent_received_and_old_goal_broadcasts_only(db):
  db.add_all([models.Chat(id=name, title=name, messages=[]) for name in ['self', 'peer', 'other']])
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
  db.add(models.Chat(id='empty', messages=[]))
  db.commit()
  page = chat_message_history(db, 'empty')
  assert page['total'] == 0
  assert page['messages'] == []
  assert page['next_before'] is None


def test_history_route_requires_authorized_chat_and_rejects_bad_cursor(client, auth, db):
  db.add(models.Chat(id='history-route', messages=[]))
  db.commit()
  path = '/api/agent-coordination/chats/history-route/history'
  assert client.get(path).status_code == 401
  response = client.get(path, headers=auth)
  assert response.status_code == 200
  assert response.json()['messages'] == []
  assert client.get(path + '?before=unrelated', headers=auth).status_code == 422
  assert client.get(path + '?limit=101', headers=auth).status_code == 422
  assert client.get('/api/agent-coordination/chats/missing/history', headers=auth).status_code == 404
