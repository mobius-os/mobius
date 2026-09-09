"""Streaming snapshots stay bounded without weakening transcript durability."""

from app import models
from app.chat_message_identity import assistant_message_index
from app.chat_transcript import materialized_messages
from app.chat_writer import (
  finalize_response_outcome,
  update_last_assistant_message,
  update_live_assistant,
)


def _chat(db, *, trailing_role="user"):
  messages = [
    {"role": "user", "content": "old", "ts": 1},
    {"role": "assistant", "blocks": [{"type": "text", "text": "done"}],
     "ts": 2},
    {"role": trailing_role, "content": "current", "ts": 3},
  ]
  chat = models.Chat(
    id="live-chat",
    title="Live",
    messages=messages,
    live_assistant={"role": "assistant", "blocks": [], "ts": 4},
  )
  db.add(chat)
  db.commit()
  return chat, messages


def test_assistant_message_index_is_one_shared_identity_contract():
  exact = {"id": "assistant-live", "role": "assistant"}
  legacy = {"role": "assistant"}
  hidden_answer = {"role": "user", "hidden": True}

  assert assistant_message_index([exact, hidden_answer], exact) == 0
  assert assistant_message_index([legacy], exact) == 0
  assert assistant_message_index(
    [exact, hidden_answer], {**exact, "id": "other"}
  ) == -1
  assert assistant_message_index([exact, hidden_answer], legacy) == -1


def test_stream_snapshot_updates_only_live_value(db):
  chat, history = _chat(db)
  history_version = chat.updated_at

  assert update_live_assistant(db, chat.id, {
    "id": "assistant-streaming",
    "role": "assistant",
    "blocks": [{"type": "text", "text": "streaming"}],
  }) is True

  db.refresh(chat)
  assert chat.messages == history
  assert chat.live_assistant["ts"] == 4
  assert chat.live_assistant["blocks"][0]["text"] == "streaming"
  assert chat.active_assistant_message_id == "assistant-streaming"
  assert chat.updated_at == history_version
  assert materialized_messages(chat)[-1] == chat.live_assistant


def test_finalize_merges_live_turn_once_and_clears_snapshot(db):
  chat, history = _chat(db)
  history_version = chat.updated_at
  update_live_assistant(db, chat.id, {
    "role": "assistant",
    "blocks": [{"type": "text", "text": "partial"}],
  })

  outcome = finalize_response_outcome(
    db,
    chat.id,
    {
      "id": "assistant-current",
      "role": "assistant",
      "blocks": [{"type": "text", "text": "complete"}],
    },
  )

  db.refresh(chat)
  assert outcome.value == "applied"
  assert chat.live_assistant is None
  assert len(chat.messages) == len(history) + 1
  assert chat.messages[-1]["id"] == "assistant-current"
  assert chat.messages[-1]["ts"] == 4
  assert chat.messages[-1]["blocks"][0]["text"] == "complete"
  assert chat.active_assistant_message_id == "assistant-current"
  assert chat.updated_at != history_version


def test_materialized_snapshot_replaces_question_barrier_row():
  class Row:
    messages = [{
      "role": "assistant",
      "blocks": [{"type": "question", "question_id": "q1"}],
      "ts": 7,
    }]
    live_assistant = {
      "role": "assistant",
      "blocks": [
        {"type": "question", "question_id": "q1", "answers": {"q": "a"}},
        {"type": "text", "text": "continuing"},
      ],
      "ts": 7,
    }

  projected = materialized_messages(Row())
  assert len(projected) == 1
  assert projected[0] == Row.live_assistant


def test_materialized_snapshot_updates_exact_row_before_hidden_same_turn_answer():
  class Row:
    messages = [
      {
        "id": "assistant-live",
        "role": "assistant",
        "blocks": [{"type": "question", "question_id": "q1"}],
        "ts": 7,
      },
      {"role": "user", "hidden": True, "content": "answer", "ts": 8},
    ]
    live_assistant = {
      "id": "assistant-live",
      "role": "assistant",
      "blocks": [
        {"type": "question", "question_id": "q1", "answers": {"q": "a"}},
        {"type": "text", "text": "continuing"},
      ],
      "ts": 7,
    }

  projected = materialized_messages(Row())
  assert len(projected) == 2
  assert projected[0] is Row.live_assistant
  assert projected[1]["hidden"] is True


def test_terminal_snapshot_updates_exact_assistant_before_hidden_answer(db):
  chat = models.Chat(
    id="same-turn-answer",
    title="Same turn",
    messages=[
      {"role": "user", "content": "request", "ts": 1},
      {
        "id": "assistant-live",
        "role": "assistant",
        "blocks": [{"type": "question", "question_id": "q1"}],
        "ts": 2,
      },
      {"role": "user", "hidden": True, "content": "answer", "ts": 3},
    ],
    live_assistant={
      "id": "assistant-live", "role": "assistant", "blocks": [], "ts": 2,
    },
  )
  db.add(chat)
  db.commit()

  assert update_last_assistant_message(db, chat.id, {
    "id": "assistant-live",
    "role": "assistant",
    "content": "continued",
    "blocks": [{"type": "text", "content": "continued"}],
  }) is True

  db.refresh(chat)
  assert len(chat.messages) == 3
  assert chat.messages[1]["id"] == "assistant-live"
  assert chat.messages[1]["content"] == "continued"
  assert chat.messages[2]["hidden"] is True


def test_finalize_preserves_identity_before_hidden_same_turn_answer(db):
  chat = models.Chat(
    id="same-turn-finalize",
    title="Same turn finalize",
    messages=[
      {"role": "user", "content": "request", "ts": 1},
      {
        "id": "assistant-live",
        "role": "assistant",
        "blocks": [{"type": "question", "question_id": "q1"}],
        "ts": 2,
      },
      {"role": "user", "hidden": True, "content": "answer", "ts": 3},
    ],
    live_assistant={
      "id": "assistant-live", "role": "assistant", "blocks": [], "ts": 2,
    },
  )
  db.add(chat)
  db.commit()

  outcome = finalize_response_outcome(db, chat.id, {
    "id": "assistant-live",
    "role": "assistant",
    "content": "continued",
    "blocks": [{"type": "text", "content": "continued"}],
  })

  db.refresh(chat)
  assert outcome.value == "applied"
  assert len(chat.messages) == 3
  assert chat.messages[1]["id"] == "assistant-live"
  assert chat.messages[1]["content"] == "continued"
  assert chat.messages[2]["hidden"] is True


def test_unknown_assistant_identity_appends_instead_of_rewriting_old_tail(db):
  chat = models.Chat(
    id="new-segment",
    title="New segment",
    messages=[
      {"role": "user", "content": "request", "ts": 1},
      {"id": "assistant-old", "role": "assistant", "content": "old", "ts": 2},
    ],
    live_assistant={
      "id": "assistant-new", "role": "assistant", "blocks": [], "ts": 3,
    },
  )
  db.add(chat)
  db.commit()

  assert update_last_assistant_message(db, chat.id, {
    "id": "assistant-new",
    "role": "assistant",
    "content": "new",
    "blocks": [{"type": "text", "content": "new"}],
  }) is True

  db.refresh(chat)
  assert [message.get("id") for message in chat.messages] == [
    None, "assistant-old", "assistant-new",
  ]
  assert chat.messages[1]["content"] == "old"


def test_identity_adopts_only_a_trailing_pre_identity_partial(db):
  chat = models.Chat(
    id="rolling-identity",
    title="Rolling identity",
    messages=[
      {"role": "user", "content": "request", "ts": 1},
      {"role": "assistant", "content": "partial", "ts": 2},
    ],
    live_assistant={
      "id": "assistant-current", "role": "assistant", "blocks": [], "ts": 2,
    },
  )
  db.add(chat)
  db.commit()

  assert update_last_assistant_message(db, chat.id, {
    "id": "assistant-current",
    "role": "assistant",
    "content": "complete",
    "blocks": [{"type": "text", "content": "complete"}],
  }) is True

  db.refresh(chat)
  assert len(chat.messages) == 2
  assert chat.messages[-1]["id"] == "assistant-current"
  assert chat.messages[-1]["content"] == "complete"


def test_codex_path_wrappers_are_normalized_before_json_storage(db):
  class ProviderPathWrapper:
    """Minimal stand-in for the Codex SDK's Pydantic root path wrapper."""

    def __init__(self, value):
      self.value = value

    def model_dump(self, **_kwargs):
      return self.value

  chat, _history = _chat(db)

  assert update_live_assistant(db, chat.id, {
    "role": "assistant",
    "blocks": [{
      "type": "tool",
      "name": "exec_command",
      "cwd": ProviderPathWrapper("/data"),
      "paths": [ProviderPathWrapper("/data/platform")],
    }],
  }) is True

  db.refresh(chat)
  block = chat.live_assistant["blocks"][0]
  assert block["cwd"] == "/data"
  assert block["paths"] == ["/data/platform"]

  outcome = finalize_response_outcome(db, chat.id, {
    "role": "assistant",
    "blocks": [{
      "type": "tool",
      "name": "exec_command",
      "cwd": ProviderPathWrapper("/data"),
    }],
  })

  db.refresh(chat)
  assert outcome.value == "applied"
  assert chat.messages[-1]["blocks"][0]["cwd"] == "/data"


def test_repeated_snapshots_never_update_the_historical_chat_row(db):
  from sqlalchemy import event

  chat, history = _chat(db)
  chat.active_assistant_message_id = 'assistant-live'
  chat.live_assistant = {
    'id': 'assistant-live', 'role': 'assistant', 'blocks': [], 'ts': 4,
  }
  db.commit()
  statements = []
  def record(_conn, _cursor, statement, _parameters, _context, _many):
    statements.append(statement.lower())
  engine = db.get_bind()
  chat_id = chat.id
  event.listen(engine, 'before_cursor_execute', record)
  try:
    for content in ('one', 'two', 'three'):
      assert update_live_assistant(db, chat_id, {
        'id': 'assistant-live', 'role': 'assistant',
        'blocks': [{'type': 'text', 'content': content}],
      })
  finally:
    event.remove(engine, 'before_cursor_execute', record)
  assert any('update chat_live_assistants' in sql for sql in statements)
  assert not any('update chats ' in sql for sql in statements)
  assert not any('chats.messages' in sql for sql in statements)
  db.refresh(chat)
  assert chat.messages == history
  assert chat.live_assistant['blocks'][0]['content'] == 'three'


def test_live_snapshot_wal_bytes_do_not_scale_with_history(tmp_path):
  from sqlalchemy import create_engine, text
  from sqlalchemy.orm import Session

  engine = create_engine(f'sqlite:///{tmp_path / "live-wal.db"}')
  models.Base.metadata.create_all(engine)
  with engine.connect() as connection:
    connection.execute(text('PRAGMA journal_mode=WAL'))
    connection.execute(text('PRAGMA wal_autocheckpoint=0'))
    with Session(bind=connection) as session:
      chat = models.Chat(
        id='large-history', messages=[{'role': 'user', 'content': 'x' * 2_000_000}],
        active_assistant_message_id='live',
        live_assistant={'id': 'live', 'role': 'assistant', 'blocks': [], 'ts': 2},
      )
      session.add(chat)
      session.commit()
      connection.commit()
      connection.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
      connection.commit()
      for size in (1000, 2000, 4000):
        assert update_live_assistant(session, 'large-history', {
          'id': 'live', 'role': 'assistant',
          'blocks': [{'type': 'text', 'content': 'y' * size}],
        })
        connection.commit()
      wal_bytes = (tmp_path / 'live-wal.db-wal').stat().st_size
      assert 0 < wal_bytes < 100_000, wal_bytes
  engine.dispose()


def test_live_snapshot_is_deleted_with_chat(db):
  chat, _ = _chat(db)
  assert db.get(models.ChatLiveAssistant, chat.id) is not None
  chat_id = chat.id
  db.delete(chat)
  db.commit()
  assert db.get(models.ChatLiveAssistant, chat_id) is None


def test_retention_reclaims_live_row_only_after_chat_recovery_window(db):
  from datetime import datetime, timedelta
  from app.chat_retention import purge_expired_chat_tombstones

  chat, _ = _chat(db)
  chat_id = chat.id
  chat.deleted_at = datetime.now() - timedelta(days=6)
  db.commit()
  purge_expired_chat_tombstones(db)
  assert db.get(models.ChatLiveAssistant, chat_id) is not None
  chat.deleted_at = datetime.now() - timedelta(days=8)
  db.commit()
  purge_expired_chat_tombstones(db)
  db.expire_all()
  assert db.get(models.ChatLiveAssistant, chat_id) is None


def test_deleted_chat_between_snapshot_read_and_write_cannot_leave_orphan(db):
  from sqlalchemy import event, text

  chat, _ = _chat(db)
  chat_id = chat.id
  # Simulate the external lifecycle winner at the last boundary before the
  # snapshot write. This intentionally exercises SQLite with FK checks off.
  engine = db.get_bind()
  deleted = False
  def remove_chat(connection, _cursor, statement, _parameters, _context, _many):
    nonlocal deleted
    if statement.lower().startswith('update chat_live_assistants') and not deleted:
      deleted = True
      connection.execute(text('DELETE FROM chat_live_assistants WHERE chat_id=:id'), {'id': chat_id})
      connection.execute(text('DELETE FROM chats WHERE id=:id'), {'id': chat_id})
  event.listen(engine, 'before_cursor_execute', remove_chat)
  try:
    assert update_live_assistant(db, chat_id, {
      'id': 'live', 'role': 'assistant', 'blocks': [], 'ts': 2,
    }, require_row=True) is None
  finally:
    event.remove(engine, 'before_cursor_execute', remove_chat)
  assert deleted
  # The failed must-persist path rolls back the complete transaction.
  db.expire_all()
  assert db.get(models.Chat, chat_id) is not None
  assert db.get(models.ChatLiveAssistant, chat_id).snapshot['blocks'] == []
