"""A helper's delivered result is its report, not its progress narration."""
from app import transcript_rows
from app.chat_writer import create_chat
import json
import uuid
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

from tests.test_helper_hosts import _claude_host, _turn


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
  assert followup.report.stop_message is None  # No completion before this attempt starts.
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
  assert turn.report.final() == 'Unsent report'
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  host._on_task_end('agent-1', 'failed', 'Native handback failed', None)
  assert turn.status == 'failed'
  assert events == [{'type': 'assistant_result', 'content': 'Unsent report'}]


def test_claude_task_end_before_stop_hook_uses_ordered_child_response(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  report = 'Full child report before terminal notification'
  turn.report.observe_assistant(report, [])
  host._turn_by_agent['agent-1'] = turn
  host._on_task_end('agent-1', 'completed', 'Lifecycle summary', None)
  asyncio.run(host.subagent_stop({'agent_id': 'agent-1',
    'last_assistant_message': report}, None, None))
  assert events == [{'type': 'assistant_result', 'content': report}]


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


def _stream_report(tmp_path, script):
  """Drive the Claude host's ordered child stream; return published reports.

  ``script(host)`` is an async generator yielding SDK messages and awaiting
  hooks in the order Claude Code delivers them.
  """
  from claude_agent_sdk.types import TaskNotificationMessage
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
      async for message in script(host):
        yield message
      yield TaskNotificationMessage(subtype='task_notification', data={}, task_id='agent-1',
        status='completed', output_file='', summary='Lifecycle summary', uuid='end', session_id='host')
  host._client = Stream()
  asyncio.run(host._read())
  return [e['content'] for e in events if e['type'] == 'assistant_result']


def _child(*blocks):
  from claude_agent_sdk.types import AssistantMessage
  return AssistantMessage(content=list(blocks), model='claude', parent_tool_use_id='launch')


def _child_result(tool_use_id, *, is_error):
  from claude_agent_sdk.types import ToolResultBlock, UserMessage
  return UserMessage(content=[ToolResultBlock(tool_use_id=tool_use_id, content='x', is_error=is_error)],
                     parent_tool_use_id='launch')


def _handback(host, tool_use_id, tool_input):
  from claude_agent_sdk.types import ToolUseBlock
  hook = host.pre_tool_use({'agent_id': 'agent-1', 'tool_name': 'SubagentHandback',
                            'tool_input': tool_input}, tool_use_id, None)
  return hook, _child(ToolUseBlock(id=tool_use_id, name='SubagentHandback', input=tool_input))


@pytest.mark.parametrize('rejected', [False, True])
def test_early_handback_followed_by_more_work_yields_revised_final_report(tmp_path, rejected):
  from claude_agent_sdk.types import TextBlock, ToolUseBlock
  async def script(host):
    yield _child(TextBlock(text='Progress ' * 200))
    hook, message = _handback(host, 'hb-early', {'message': 'Early report'})
    await hook
    yield message
    yield _child_result('hb-early', is_error=rejected)
    yield _child(TextBlock(text='Let me check one more source.'))
    yield _child(ToolUseBlock(id='read-1', name='Read', input={'file_path': '/x'}))
    yield _child_result('read-1', is_error=False)
    yield _child(TextBlock(text='Revised final report'))
    await host.subagent_stop({'agent_id': 'agent-1',
                              'last_assistant_message': 'Revised final report'}, None, None)
  assert _stream_report(tmp_path, script) == ['Revised final report']


def test_rejected_handback_then_revised_text_answer_replaces_the_early_report(tmp_path):
  from claude_agent_sdk.types import TextBlock
  async def script(host):
    hook, message = _handback(host, 'hb-early', {'message': 'Early report'})
    await hook
    yield message
    yield _child_result('hb-early', is_error=True)
    yield _child(TextBlock(text='Revised final report'))
  assert _stream_report(tmp_path, script) == ['Revised final report']


def test_second_handback_after_more_work_wins_and_its_closing_text_does_not(tmp_path):
  from claude_agent_sdk.types import TextBlock, ToolUseBlock
  async def script(host):
    hook, message = _handback(host, 'hb-1', {'message': 'Early report'})
    await hook
    yield message
    yield _child_result('hb-1', is_error=True)
    yield _child(ToolUseBlock(id='read-1', name='Read', input={'file_path': '/x'}))
    yield _child_result('read-1', is_error=False)
    hook, message = _handback(host, 'hb-2', {'message': 'Revised report'})
    yield message  # The forwarded block may precede its hook callback.
    await hook
    yield _child_result('hb-2', is_error=False)
    yield _child(TextBlock(text='Done.'))
    await host.subagent_stop({'agent_id': 'agent-1', 'last_assistant_message': 'Done.'}, None, None)
  assert _stream_report(tmp_path, script) == ['Revised report']


def test_rejected_handback_with_no_later_content_keeps_its_report(tmp_path):
  async def script(host):
    hook, message = _handback(host, 'hb-early', {'message': 'Unsent report'})
    await hook
    yield message
    yield _child_result('hb-early', is_error=True)
  assert _stream_report(tmp_path, script) == ['Unsent report']


def test_handback_without_message_uses_text_before_it_never_closing_text(tmp_path):
  from claude_agent_sdk.types import TextBlock
  async def script(host):
    yield _child(TextBlock(text='Findings written before the handback'))
    hook, message = _handback(host, 'hb', {})
    await hook
    yield message
    yield _child_result('hb', is_error=False)
    yield _child(TextBlock(text='Closing text'))
    await host.subagent_stop({'agent_id': 'agent-1', 'last_assistant_message': 'Closing text'}, None, None)
  assert _stream_report(tmp_path, script) == ['Findings written before the handback']


def test_hook_only_handback_superseded_by_later_stream_tool_call():
  report = _report()
  report.observe_hook_tool('early', 'SubagentHandback', {'message': 'Early report'})
  report.observe_assistant('Working more', [_tool('read')])  # Its hook has not run: later.
  report.observe_assistant('Corrected final', [])
  report.stop_message = 'Corrected final'
  assert report.final() == 'Corrected final'


def test_hook_only_handback_superseded_in_exact_hook_order_while_stream_lags():
  report = _report()
  report.observe_hook_tool('early', 'SubagentHandback', {'message': 'Early report'})
  report.observe_hook_tool('read', 'Read', {})
  report.stop_message = 'Corrected final'
  assert report.final() == 'Corrected final'


def test_lagged_earlier_stream_work_never_supersedes_a_hook_handback():
  report = _report()
  report.observe_hook_tool('read', 'Read', {})
  report.observe_hook_tool('hb', 'SubagentHandback', {'message': 'Handback report'})
  report.observe_assistant('Reading first', [_tool('read')])  # Drained after the hook.
  report.observe_assistant('Done.', [])
  report.stop_message = 'Done.'
  assert report.final() == 'Handback report'


@pytest.mark.parametrize('hook_at', ['before-block', 'after-block', 'after-everything'])
def test_delayed_handback_hook_never_trumps_newer_stream_state(hook_at):
  report = _report()
  hook = lambda: report.observe_hook_tool('hb', 'SubagentHandback', {'message': 'Early report'})
  if hook_at == 'before-block':
    hook()
  report.observe_assistant('', [_tool('hb', 'SubagentHandback', message='Early report')])
  if hook_at == 'after-block':
    hook()
  report.observe_tool_result('hb', True)
  report.observe_hook_tool('read', 'Read', {})
  report.observe_assistant('', [_tool('read')])
  report.observe_assistant('Corrected final', [])
  if hook_at == 'after-everything':
    hook()
  report.stop_message = 'Corrected final'
  assert report.final() == 'Corrected final'


def test_rejected_hook_only_handback_yields_to_revised_text():
  report = _report()
  report.observe_hook_tool('hb', 'SubagentHandback', {'message': 'Early report'})
  report.observe_tool_result('hb', True)
  report.observe_assistant('Revised final', [])
  assert report.final() == 'Revised final'


def test_stop_hook_final_response_wins_over_stream_narration_without_handback():
  report = _report()
  report.observe_assistant('Progress narration', [])
  report.stop_message = 'Actual report only in stop hook'
  assert report.final() == 'Actual report only in stop hook'


@pytest.mark.parametrize('stop', [None, 'Done.'])
def test_live_handback_suppresses_closing_text_in_stream_and_stop_hook(stop):
  report = _report()
  report.observe_assistant('Narration', [])
  report.observe_assistant('', [_tool('hb', 'SubagentHandback', message='Report')])
  report.observe_assistant('Done.', [])
  report.stop_message = stop
  assert report.final() == 'Report'


def test_rejected_handback_without_later_report_beats_stop_hook_text():
  report = _report()
  report.observe_assistant('Handing back now', [])
  report.observe_assistant('', [_tool('hb', 'SubagentHandback', message='Unsent report')])
  report.observe_tool_result('hb', True)
  report.stop_message = 'Handing back now'
  assert report.final() == 'Unsent report'


# Hook/stream ordering probes use Claude SDK tool-use blocks.
from claude_agent_sdk.types import ToolUseBlock

def _tool(tool_use_id, name="Read", **tool_input):
  return ToolUseBlock(id=tool_use_id, name=name, input=tool_input)

def _report():
  from app.claude_helper_host import HelperReport
  return HelperReport()


def _test_chat(db, messages):
  chat = create_chat(id=str(uuid.uuid4()), title="test helper", messages=messages)
  db.add(chat)
  db.flush()
  return chat


def sink_for(chat):
  get_writer().submit(StartTurn(chat_id=chat.id, run_token='sink-write-test',
    user_msg={'role': 'user', 'content': 'Test', 'ts': 10},
    title_source='Test')).result(timeout=5)
  return ChatEventSink(ChatBroadcast(chat.id), chat.id, run_token='sink-write-test')


@pytest.mark.parametrize('source', ['content', 'reference'])
@pytest.mark.parametrize('terminal_status', ['completed', 'failed', 'stopped'])
def test_result_is_persisted_without_replaying_prose(
    chat, db, monkeypatch, source, terminal_status):
  async def scenario():
    sink = sink_for(chat)
    narration = 'Looking at another source. ' * 300
    sink.publish({'type': 'text_final', 'text_item_id': 'progress', 'content': narration})
    report = 'Verdict: fix the delivery boundary.'
    sink.publish({'type': 'text_final', 'text_item_id': 'answer',
                  'content': report})
    event = {'type': 'assistant_result', **(
      {'content': report} if source == 'content'
      else {'text_item_id': 'answer'})}
    sink.publish(event)
    sink.publish(event)
    await sink.finalize()
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    message = transcript_rows.at(db, saved, -1)
    # Equal to the segment's last text, so the report is stored only once.
    assert 'result' not in message
    assert narration in message['content']
    assert message['content'].count(report) == 1
    assert _assistant_result(saved, run_ids={'sink-write-test'}) == report
    get_writer().submit(FinishRun(chat_id=chat.id, run_token='sink-write-test',
                                 terminal_status=terminal_status)).result(timeout=5)
    row = models.Delegation(id='result-helper', parent_chat_id='parent',
                            child_chat_id=chat.id, task_key='inspect')
    notice = _compose_wake_notice(db, [row], {row.id: 'sink-write-test'})
    assert report in notice and 'Looking at another' not in notice
    assert f'"status":"{terminal_status}"' in notice
    items = json.loads(notice.rsplit('<delegation_results>', 1)[1].split('</delegation_results>')[0])
    assert items[0]['result'] == report
  asyncio.run(scenario())


def test_result_never_bleeds_into_a_new_steered_segment(chat, monkeypatch):
  async def scenario():
    sink = sink_for(chat)
    sink.publish({'type': 'assistant_result', 'content': 'Old verdict'})
    sink.assistant_message_id = 'sink-write-test:assistant:1'
    sink.assistant_blocks = [{'type': 'text', 'content': 'New work'}]
    snapshot, _ = sink._deferred_snapshot(sink.assistant_blocks)
    assert 'result' not in snapshot
  asyncio.run(scenario())


def test_report_only_terminal_does_not_hide_recorded_failure(chat, db, monkeypatch):
  async def scenario():
    sink = sink_for(chat)
    sink.publish({'type': 'assistant_result', 'content': 'Useful report'})
    sink._last_error = 'Transport ended unexpectedly'
    await sink.finalize()
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    assert transcript_rows.at(db, saved, -1)['blocks'] == [
      {'type': 'error', 'message': 'Transport ended unexpectedly'}]
    assert _assistant_result(saved) == 'Useful report\n\nTransport ended unexpectedly'
  asyncio.run(scenario())


def test_handback_without_message_or_prior_text_is_an_explicit_empty_report(tmp_path, chat, monkeypatch):
  from claude_agent_sdk.types import TextBlock
  async def script(host):
    hook, message = _handback(host, 'hb', {})
    await hook
    yield message
    yield _child(TextBlock(text='Closing text'))
  assert _stream_report(tmp_path, script) == ['']
  # The empty report is deliberate: projection must not fall back to closing text.
  async def persist():
    sink = sink_for(chat)
    sink.publish({'type': 'text_final', 'text_item_id': 'closing', 'content': 'Closing text'})
    sink.publish({'type': 'assistant_result', 'content': ''})
    await sink.finalize()
  asyncio.run(persist())
  from app.database import SessionLocal
  with SessionLocal() as session:
    saved = session.get(models.Chat, chat.id)
    assert transcript_rows.at(session, saved, -1)['content'] == 'Closing text'
    assert _assistant_result(saved, run_ids={'sink-write-test'}) == ''


def test_unchanged_final_text_is_stored_once(chat, db, monkeypatch):
  async def scenario():
    sink = sink_for(chat)
    sink.publish({'type': 'text_final', 'text_item_id': 'progress', 'content': 'Looking around.'})
    sink.publish({'type': 'text_final', 'text_item_id': 'answer', 'content': 'Verdict: ship it.'})
    sink.publish({'type': 'assistant_result', 'text_item_id': 'answer'})
    await sink.finalize()
  asyncio.run(scenario())
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert 'result' not in transcript_rows.at(db, saved, -1)
  assert _assistant_result(saved, run_ids={'sink-write-test'}) == 'Verdict: ship it.'


@pytest.mark.parametrize('trailing', [
  [{'type': 'text', 'content': '   \n'}],
  [{'type': 'error', 'message': 'Provider stopped'}],
  [{'type': 'text', 'content': ' '}, {'type': 'error', 'message': 'Provider stopped'}],
])
def test_explicit_empty_report_is_kept_over_earlier_narration_and_blank_tail(chat, db, trailing):
  """Dedupe compares with projection's last NONEMPTY text: an empty report
  must still be stored, or earlier narration would resurface as the report."""
  sink = ChatEventSink.__new__(ChatEventSink)
  sink.chat_id = chat.id
  sink.assistant_message_id = 'run:assistant:1'
  sink._assistant_result = ('run:assistant:1', '')
  blocks = [{'type': 'text', 'content': 'Earlier narration'}, *trailing]
  snapshot, _ = sink._deferred_snapshot(blocks)
  assert snapshot['result'] == ''
  error = [b['message'] for b in trailing if b['type'] == 'error']
  assert _assistant_result(_test_chat(db, [{**snapshot, 'role': 'assistant'}]),
                           run_ids={'run'}) == '\n\n'.join(error)
