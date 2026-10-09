"""A helper's delivered result is its report, not its progress narration."""
from app import transcript_rows
import asyncio
from types import SimpleNamespace

import pytest

from app import models
from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink
from app.chat_writer import FinishRun, StartTurn, get_writer
from app.delegations import _assistant_result, _compose_wake_notice

TOKEN = 'helper-result-test'
NARRATION = 'Looking at another source. ' * 300
REPORT = 'Verdict: fix the delivery boundary.'


def _narration_then_report(separator):
  if separator == 'codex_items':
    # Codex: each agent message item becomes its own text block.
    return [
      {'type': 'text_final', 'text_item_id': 'progress', 'content': NARRATION},
      {'type': 'text_final', 'text_item_id': 'answer', 'content': REPORT},
    ]
  return [
    {'type': 'text', 'content': NARRATION},
    {'type': 'tool_start', 'tool': 'Bash', 'tool_use_id': 'tool-1', 'input': 'ls'},
    {'type': 'tool_end', 'tool_use_id': 'tool-1'},
    {'type': 'text', 'content': REPORT},
  ]


@pytest.mark.parametrize('separator', ['codex_items', 'tool'])
@pytest.mark.parametrize('terminal_status', ['completed', 'failed', 'stopped'])
def test_wake_notice_delivers_report_not_narration(chat, db, separator, terminal_status):
  async def scenario():
    get_writer().submit(StartTurn(chat_id=chat.id, run_token=TOKEN,
      user_msg={'role': 'user', 'content': 'Test', 'ts': 10},
      title_source='Test')).result(timeout=5)
    sink = ChatEventSink(ChatBroadcast(chat.id), chat.id, run_token=TOKEN)
    for event in _narration_then_report(separator):
      sink.publish(event)
    if terminal_status == 'failed':
      sink.publish({'type': 'error', 'message': 'Provider ended unexpectedly'})
    await sink.finalize()
    get_writer().submit(FinishRun(chat_id=chat.id, run_token=TOKEN,
                                 terminal_status=terminal_status)).result(timeout=5)
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    # The child transcript keeps the full narration as evidence.
    assert NARRATION.strip() in transcript_rows.at(db, saved, -1)['content']
    row = models.Delegation(id='result-helper', parent_chat_id='parent',
                            child_chat_id=chat.id, task_key='inspect')
    notice = _compose_wake_notice(db, [row], {row.id: TOKEN})
    assert REPORT in notice and 'Looking at another' not in notice
    assert f'"status":"{terminal_status}"' in notice
    assert '"result_truncated":false' in notice
    assert ('Provider ended unexpectedly' in notice) == (terminal_status == 'failed')
  asyncio.run(scenario())


def test_result_keeps_latest_text_and_error_with_content_only_fallback(db):
  from app.chat_writer import create_chat
  chat = create_chat(id='result-fallback', title='helper', messages=[
    {'role': 'assistant', 'content': 'old narration + report',
     'blocks': [{'type': 'text', 'content': 'old narration'},
                {'type': 'text', 'content': 'report'},
                {'type': 'error', 'message': 'Provider stopped'}]}])
  db.add(chat)
  db.commit()
  assert _assistant_result(chat) == 'report\n\nProvider stopped'
  transcript_rows.replace_all(db, chat, [{'role': 'assistant', 'content': 'Legacy content-only report'}])
  db.commit()
  assert _assistant_result(chat) == 'Legacy content-only report'