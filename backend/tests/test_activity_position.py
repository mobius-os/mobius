"""An activity's saved display position cannot chase a growing response."""

from app import models
from app.memory_recall import EMPTY_RECALL_BINDING
from app.activity_position import attach_activity_positions, record_activity_position
from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink


def test_saved_position_is_exact_chat_utf16_and_does_not_move_on_retry(db):
  db.add_all([models.Chat(id='anchor-a', messages=[]), models.Chat(id='anchor-b', messages=[])])
  db.commit()
  sink = ChatEventSink(ChatBroadcast('anchor-a'), chat_id='anchor-a', recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink('anchor-a', sink)
  try:
    sink.assistant_blocks = [
      {'type': 'tool', 'name': 'AskUserQuestion'},
      {'type': 'text_boundary'},
      {'type': 'text', 'content': 'a😀'},
    ]
    sink._publish_activity_frontier()
    record_activity_position(db, 'anchor-a', 'peer:one')
    db.commit()
    sink.assistant_blocks[-1]['content'] += ' later streaming response'
    sink._publish_activity_frontier()
    record_activity_position(db, 'anchor-a', 'peer:one')
    db.commit()
    own = [{'id': 'peer:one'}]
    attach_activity_positions(db, 'anchor-a', own)
    assert own[0]['display_position'] == {
      'assistant_message_id': sink.assistant_message_id,
      'block_index': 1, 'text_offset': 3,
    }
    foreign = [{'id': 'peer:one'}]
    attach_activity_positions(db, 'anchor-b', foreign)
    assert foreign[0]['display_position'] is None
    assert db.get(models.Chat, 'anchor-a').messages == []
  finally:
    unregister_active_sink('anchor-a', sink)


def test_absence_is_saved_not_backfilled_from_a_later_turn(db):
  db.add(models.Chat(id='anchor-quiet', messages=[]))
  db.commit()
  record_activity_position(db, 'anchor-quiet', 'delegation:one:completed')
  db.commit()
  sink = ChatEventSink(ChatBroadcast('anchor-quiet'), chat_id='anchor-quiet', recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink('anchor-quiet', sink)
  try:
    record_activity_position(db, 'anchor-quiet', 'delegation:one:completed')
    db.commit()
    events = [{'id': 'delegation:one:completed'}, {'id': 'peer:historical'}]
    attach_activity_positions(db, 'anchor-quiet', events)
    assert all(event['display_position'] is None for event in events)
  finally:
    unregister_active_sink('anchor-quiet', sink)


def test_capture_is_rolled_back_with_event_transaction(db):
  db.add(models.Chat(id='anchor-rollback', messages=[]))
  db.commit()
  record_activity_position(db, 'anchor-rollback', 'peer:rollback')
  db.rollback()
  assert db.get(models.ChatActivityPosition, ('anchor-rollback', 'peer:rollback')) is None


def test_peer_history_and_activity_share_the_creation_anchor(db):
  from app.agent_coordination import CoordinationScope, _persist_send, chat_message_history
  from app.chat_activity import chat_activity_page

  db.add_all([models.Chat(id='anchor-send', messages=[]), models.Chat(id='anchor-receive', messages=[])])
  db.commit()
  sink = ChatEventSink(ChatBroadcast('anchor-receive'), chat_id='anchor-receive', recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink('anchor-receive', sink)
  try:
    sink.publish({'type': 'text', 'content': 'before 😀'})
    rows = _persist_send(
      db, CoordinationScope('workspace', 'owner', 'anchor-send'), sender_chat_id='anchor-send',
      sender_run_id='sender-run', recipients=['anchor-receive'], broadcast=False,
      kind='note', delivery='next_turn', body='hello', send_id='once',
    )
    sink.publish({'type': 'text', 'content': ' after answer'})
    history = chat_message_history(db, 'anchor-receive')['messages'][0]
    activity = chat_activity_page(db, 'anchor-receive')['events'][0]
    assert history['id'] == rows[0]['id']
    assert history['display_position'] == activity['display_position'] == {
      'assistant_message_id': sink.assistant_message_id,
      'block_index': 0, 'text_offset': 9,
    }
    # Sending-side projection cannot borrow the receiving chat's position.
    assert chat_message_history(db, 'anchor-send')['messages'][0]['display_position'] is None
  finally:
    unregister_active_sink('anchor-receive', sink)


def test_position_migration_upgrades_without_inventing_history():
  from sqlalchemy import create_engine, inspect, text
  from app.schema_migrations import _add_chat_activity_positions

  engine = create_engine('sqlite:///:memory:')
  with engine.begin() as connection:
    connection.execute(text('CREATE TABLE chats (id VARCHAR(64) PRIMARY KEY)'))
  _add_chat_activity_positions(engine)
  _add_chat_activity_positions(engine)
  assert {item['name'] for item in inspect(engine).get_columns('chat_activity_positions')} == {'chat_id', 'event_id', 'position'}
  with engine.connect() as connection:
    assert connection.execute(text('SELECT COUNT(*) FROM chat_activity_positions')).scalar() == 0


def test_question_identity_resolves_position_when_live_hides_question_tool():
  from app.chat_event_sink import active_sink_activity_position

  sink = ChatEventSink(ChatBroadcast('question-anchor'), chat_id='question-anchor',
                       recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink('question-anchor', sink)
  try:
    sink.assistant_blocks = [
      {'type': 'tool', 'tool_use_id': 'ask-tool'},
      {'type': 'question', 'question_id': 'saved-question'},
      {'type': 'text_boundary'},
      {'type': 'text', 'content': 'continued'},
    ]
    sink._publish_activity_frontier()
    assert active_sink_activity_position('question-anchor') == {
      'assistant_message_id': sink.assistant_message_id,
      'block_index': 2, 'text_offset': 9,
      'block_key': 'question:saved-question', 'block_distance': 1,
    }
  finally:
    unregister_active_sink('question-anchor', sink)


def test_empty_and_whitespace_segments_never_advertise_phantom_identity(db):
  from app.chat_event_sink import active_sink_activity_position

  db.add(models.Chat(id='unsealed-anchor', messages=[]))
  db.commit()
  sink = ChatEventSink(ChatBroadcast('unsealed-anchor'), chat_id='unsealed-anchor',
                       recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink('unsealed-anchor', sink)
  try:
    assert active_sink_activity_position('unsealed-anchor') is None
    for text in ('', ' ', '\n\t'):
      sink.publish({'type': 'text', 'content': text})
      assert active_sink_activity_position('unsealed-anchor') is None
    record_activity_position(db, 'unsealed-anchor', 'peer:before-steer')
    db.commit()
    sink.publish({'type': 'text', 'content': 'real content'})
    assert active_sink_activity_position('unsealed-anchor') is not None
    record_activity_position(db, 'unsealed-anchor', 'peer:before-steer')
    db.commit()
    events = [{'id': 'peer:before-steer'}]
    attach_activity_positions(db, 'unsealed-anchor', events)
    assert events[0]['display_position'] is None
  finally:
    unregister_active_sink('unsealed-anchor', sink)
