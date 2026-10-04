"""Helper verdicts are distinct from narration; transcripts remain full evidence."""
import asyncio
from types import SimpleNamespace

import pytest

from app import models
from app.agent_write_channel import frame
from app.chat_writer import FinishRun, get_writer
from app.delegations import _assistant_result, _result_with_write_repair, _compose_wake_notice
from tests.test_agent_write_sink import sink_for, NONCE, WRITE
from tests.test_helper_hosts import _claude_host, _turn


@pytest.mark.parametrize('source', ['content', 'reference'])
@pytest.mark.parametrize('terminal_status', ['completed', 'failed', 'stopped'])
def test_result_is_persisted_without_replaying_prose_or_private_writes(
    chat, db, monkeypatch, source, terminal_status):
  async def scenario():
    effects = []
    sink = sink_for(chat, monkeypatch, effects)
    narration = 'Looking at another source. ' * 300
    sink.publish({'type': 'text_final', 'text_item_id': 'progress', 'content': narration})
    report = 'Verdict: fix the delivery boundary.'
    sink.publish({'type': 'text_final', 'text_item_id': 'answer',
                  'content': report + frame(NONCE, WRITE)})
    event = {'type': 'assistant_result', **(
      {'content': report + frame(NONCE, WRITE)} if source == 'content'
      else {'text_item_id': 'answer'})}
    sink.publish(event)
    sink.publish(event)
    await sink.finalize()
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    message = saved.messages[-1]
    assert message['result'] == report
    assert narration in message['content']
    assert message['content'].count(report) == 1
    assert 'MOBIUS_WRITE' not in str(message)
    assert len(effects) == 1
    assert _assistant_result(saved, run_ids={'sink-write-test'}) == report
    get_writer().submit(FinishRun(chat_id=chat.id, run_token='sink-write-test',
                                 terminal_status=terminal_status)).result(timeout=5)
    row = models.Delegation(id='result-helper', parent_chat_id='parent',
                            child_chat_id=chat.id, task_key='inspect')
    notice = _compose_wake_notice(db, [row], {row.id: 'sink-write-test'})
    assert report in notice and 'Looking at another' not in notice
    assert f'"status":"{terminal_status}"' in notice
    assert '"result_truncated":false' in notice
  asyncio.run(scenario())


def test_result_never_bleeds_into_a_new_steered_segment(chat, monkeypatch):
  async def scenario():
    sink = sink_for(chat, monkeypatch, [])
    sink.publish({'type': 'assistant_result', 'content': 'Old verdict'})
    sink.assistant_message_id = 'sink-write-test:assistant:1'
    sink.assistant_blocks = [{'type': 'text', 'content': 'New work'}]
    snapshot, _ = sink._deferred_snapshot(sink.assistant_blocks)
    assert 'result' not in snapshot
    await sink.finish_write_delivery()
  asyncio.run(scenario())


@pytest.mark.parametrize('status', ['failed', 'stopped', 'completed'])
def test_new_attempt_never_returns_previous_success(db, status):
  chat = SimpleNamespace(messages=[{'id': 'old', 'role': 'assistant',
                                  'content': 'Old progress', 'result': 'Old success'}])
  run = SimpleNamespace(id='new', continuation_json={}, status=status)
  assert _result_with_write_repair(db, chat, run) == ''
  chat.messages.append({'id': 'new', 'role': 'assistant', 'blocks': [
    {'type': 'text', 'content': 'Progress ' * 1000},
    {'type': 'text', 'content': 'Partial finding'},
    {'type': 'error', 'message': 'Provider stopped'},
  ]})
  assert _result_with_write_repair(db, chat, run) == 'Partial finding\n\nProvider stopped'


def test_legacy_result_uses_latest_text_but_keeps_content_only_history_readable():
  chat = SimpleNamespace(messages=[{'role': 'assistant', 'content': 'old narration + report',
    'blocks': [{'type': 'text', 'content': 'old narration'},
               {'type': 'text', 'content': 'report'}]}])
  assert _assistant_result(chat) == 'report'
  chat.messages = [{'role': 'assistant', 'content': 'Legacy content-only report'}]
  assert _assistant_result(chat) == 'Legacy content-only report'


@pytest.mark.parametrize('handback', [False, True])
@pytest.mark.parametrize('summary', [None, 'Inspection complete, not the report'])
def test_claude_result_comes_from_exact_report_not_task_summary(tmp_path, handback, summary):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  turn.started.set()
  host._turn_by_agent['agent-1'] = turn
  if handback:
    asyncio.run(host.pre_tool_use({'agent_id': 'agent-1', 'tool_name': 'SubagentHandback',
      'tool_input': {'message': 'The actual report'}}, 'handback', None))
    asyncio.run(host.post_tool_use({'agent_id': 'agent-1', 'tool_name': 'SubagentHandback',
      'tool_input': {'message': 'The actual report'}}, 'handback', None))
  asyncio.run(host.subagent_stop({'agent_id': 'agent-1',
    'last_assistant_message': 'Closing text' if handback else 'The actual report'}, None, None))
  host._on_task_end('agent-1', 'completed', summary, None)
  host._on_task_end('agent-1', 'completed', 'Duplicate end', None)
  assert events == [{'type': 'assistant_result', 'content': 'The actual report'}]
  assert turn.summary == summary
  followup = _turn(tmp_path, dispatch_id='followup')
  followup.sink = turn.sink
  host._turn_by_agent['agent-1'] = followup
  asyncio.run(host.subagent_stop({'agent_id': 'agent-1',
    'last_assistant_message': 'Late old closing text'}, None, None))
  assert followup.result is None  # No completion before this attempt starts.
  followup.started.set()
  asyncio.run(host.subagent_stop({'agent_id': 'agent-1',
    'last_assistant_message': 'Follow-up report'}, None, None))
  host._on_task_end('agent-1', 'completed', None, None)
  assert events[-1]['content'] == 'Follow-up report'


def test_handback_report_is_retained_even_when_later_closing_text_differs(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  host._turn_by_agent['agent-1'] = turn
  asyncio.run(host.pre_tool_use({'agent_id': 'agent-1', 'tool_name': 'SubagentHandback',
    'tool_input': {'message': 'Unsent report'}}, 'handback', None))
  asyncio.run(host.subagent_stop({'agent_id': 'agent-1',
    'last_assistant_message': 'Done'}, None, None))
  assert turn.result == 'Unsent report'
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  host._on_task_end('agent-1', 'failed', 'Native handback failed', None)
  assert turn.status == 'failed'
  assert events == [{'type': 'assistant_result', 'content': 'Unsent report'}]


def test_blank_followup_over_unattributed_legacy_history_has_no_result(db):
  chat = SimpleNamespace(messages=[{'role': 'assistant', 'content': 'Old success'}])
  run = SimpleNamespace(id='new', continuation_json={})
  assert _result_with_write_repair(db, chat, run) == ''
  assert _result_with_write_repair(db, chat, None) == 'Old success'


def test_claude_task_end_before_stop_hook_uses_ordered_child_response(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  turn.last_response = 'Full child report before terminal notification'
  host._turn_by_agent['agent-1'] = turn
  host._on_task_end('agent-1', 'completed', 'Lifecycle summary', None)
  asyncio.run(host.subagent_stop({'agent_id': 'agent-1',
    'last_assistant_message': 'Full child report before terminal notification'}, None, None))
  assert events == [{'type': 'assistant_result', 'content': turn.last_response}]


@pytest.mark.parametrize('handback', [False, True])
def test_claude_forwarded_stream_retains_report_when_terminal_precedes_hook(tmp_path, handback):
  from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, TaskNotificationMessage
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  host._turn_by_agent['agent-1'] = turn
  host._turn_by_tool_use['launch'] = turn
  host._closed = True
  class Stream:
    async def receive_messages(self):
      yield AssistantMessage(content=[TextBlock(text='Dispatcher narration')], model='claude')
      yield AssistantMessage(content=[TextBlock(text='Progress ' * 1000)], model='claude', parent_tool_use_id='launch')
      yield AssistantMessage(content=[
        ToolUseBlock(id='handback', name='SubagentHandback', input={'message': 'Full report'})
        if handback else TextBlock(text='Full report')
      ], model='claude', parent_tool_use_id='launch')
      if handback:
        yield AssistantMessage(content=[TextBlock(text='Closing text')], model='claude', parent_tool_use_id='launch')
      yield TaskNotificationMessage(subtype='task_notification', data={}, task_id='agent-1',
        status='completed', output_file='', summary='Lifecycle summary', uuid='end', session_id='host')
      await host.subagent_stop({'agent_id': 'agent-1', 'last_assistant_message': 'Late closing text'}, None, None)
  host._client = Stream()
  asyncio.run(host._read())
  assert [e for e in events if e['type'] == 'assistant_result'] == [
    {'type': 'assistant_result', 'content': 'Full report'}]


def test_explicit_result_keeps_a_later_failure_actionable():
  chat = SimpleNamespace(messages=[{'id': 'run', 'role': 'assistant',
    'result': 'Substantive findings', 'blocks': [
      {'type': 'text', 'content': 'Narration'},
      {'type': 'error', 'message': 'DELEGATION_WRITE_REVIEW_REQUIRED: Inspect before retry'},
    ]}])
  assert _assistant_result(chat, run_ids={'run'}) == (
    'Substantive findings\n\nDELEGATION_WRITE_REVIEW_REQUIRED: Inspect before retry')


def test_report_only_terminal_does_not_hide_recorded_failure(chat, db, monkeypatch):
  async def scenario():
    sink = sink_for(chat, monkeypatch, [])
    sink.publish({'type': 'assistant_result', 'content': 'Useful report'})
    sink._last_error = 'Transport ended unexpectedly'
    await sink.finalize()
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    assert saved.messages[-1]['blocks'] == [
      {'type': 'error', 'message': 'Transport ended unexpectedly'}]
    assert _assistant_result(saved) == 'Useful report\n\nTransport ended unexpectedly'
  asyncio.run(scenario())
