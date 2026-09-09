"""Live-state relocation preserves in-flight recovery and historical bytes."""

import json

from sqlalchemy import create_engine, inspect, text

from app.schema_migrations import _separate_chat_live_assistants


def test_live_migration_preserves_exact_bytes_and_is_retryable(tmp_path):
  engine = create_engine(f'sqlite:///{tmp_path / "upgrade.db"}')
  history = json.dumps([{'role': 'user', 'content': 'keep history'}])
  live = json.dumps({'id': 'run', 'role': 'assistant', 'blocks': [
    {'type': 'question', 'question_id': 'q', 'answers': {'choice': 'yes'}},
    {'type': 'text', 'content': 'saved partial'},
  ]})
  with engine.begin() as connection:
    connection.execute(text('CREATE TABLE chats (id TEXT PRIMARY KEY, messages JSON, live_assistant JSON)'))
    connection.execute(text('INSERT INTO chats VALUES (:id, :messages, :live)'), [
      {'id': 'active', 'messages': history, 'live': live},
      {'id': 'idle', 'messages': history, 'live': None},
    ])
  _separate_chat_live_assistants(engine)
  _separate_chat_live_assistants(engine)
  assert 'live_assistant' not in {c['name'] for c in inspect(engine).get_columns('chats')}
  with engine.connect() as connection:
    assert connection.execute(text('SELECT messages FROM chats ORDER BY id')).scalars().all() == [history, history]
    assert connection.execute(text('SELECT chat_id, snapshot FROM chat_live_assistants')).all() == [('active', live)]
  engine.dispose()


def test_fresh_live_schema_needs_no_legacy_column(tmp_path):
  engine = create_engine(f'sqlite:///{tmp_path / "fresh.db"}')
  with engine.begin() as connection:
    connection.execute(text('CREATE TABLE chats (id TEXT PRIMARY KEY)'))
  _separate_chat_live_assistants(engine)
  _separate_chat_live_assistants(engine)
  assert 'chat_live_assistants' in inspect(engine).get_table_names()
  engine.dispose()


def test_interrupted_live_migration_retries_without_losing_partial(tmp_path):
  import pytest
  from sqlalchemy import event

  engine = create_engine(f'sqlite:///{tmp_path / "interrupted.db"}')
  with engine.begin() as connection:
    connection.execute(text('CREATE TABLE chats (id TEXT PRIMARY KEY, live_assistant JSON)'))
    connection.execute(text("INSERT INTO chats VALUES ('active', '{\"content\":\"safe\"}')"))
  def interrupt(_conn, _cursor, statement, _params, _context, _many):
    if statement == 'ALTER TABLE chats DROP COLUMN live_assistant':
      raise RuntimeError('simulated interruption')
  event.listen(engine, 'before_cursor_execute', interrupt)
  try:
    with pytest.raises(RuntimeError, match='simulated interruption'):
      _separate_chat_live_assistants(engine)
  finally:
    event.remove(engine, 'before_cursor_execute', interrupt)
  with engine.connect() as connection:
    assert connection.execute(text('SELECT live_assistant FROM chats')).scalar_one() == '{"content":"safe"}'
  _separate_chat_live_assistants(engine)
  with engine.connect() as connection:
    assert connection.execute(text('SELECT snapshot FROM chat_live_assistants')).scalar_one() == '{"content":"safe"}'
  engine.dispose()


def test_conflicting_partial_migration_preserves_both_copies(tmp_path):
  import pytest

  engine = create_engine(f'sqlite:///{tmp_path / "conflict.db"}')
  with engine.begin() as connection:
    connection.execute(text('CREATE TABLE chats (id TEXT PRIMARY KEY, live_assistant JSON)'))
    connection.execute(text('CREATE TABLE chat_live_assistants (chat_id TEXT PRIMARY KEY, snapshot JSON)'))
    connection.execute(text("INSERT INTO chats VALUES ('active', '{\"content\":\"original\"}')"))
    connection.execute(text("INSERT INTO chat_live_assistants VALUES ('active', '{\"content\":\"different\"}')"))
  with pytest.raises(RuntimeError, match='both copies preserved'):
    _separate_chat_live_assistants(engine)
  with engine.connect() as connection:
    assert connection.execute(text('SELECT live_assistant FROM chats')).scalar_one() == '{"content":"original"}'
    assert connection.execute(text('SELECT snapshot FROM chat_live_assistants')).scalar_one() == '{"content":"different"}'
  engine.dispose()
