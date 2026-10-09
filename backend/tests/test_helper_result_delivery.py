"""Helper verdicts are distinct from narration; transcripts remain full evidence."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import models, transcript_rows
from app.chat_writer import create_chat
import uuid
from app.chat_writer import FinishRun, get_writer
from app.delegations import _assistant_result, _attempt_result, _compose_wake_notice
from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink
from app.chat_writer import StartTurn
from tests.test_helper_hosts import _claude_host, _turn


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


@pytest.mark.parametrize('status', ['failed', 'stopped', 'completed'])
def test_new_attempt_never_returns_previous_success(db, status):
  chat = _test_chat(db, [{'id': 'old', 'role': 'assistant',
                                  'content': 'Old progress', 'result': 'Old success'}])
  run = SimpleNamespace(id='new', continuation_json={}, status=status)
  assert _attempt_result(db, chat, run) == ''
  transcript_rows.append(db, chat, {'id': 'new', 'role': 'assistant', 'blocks': [
    {'type': 'text', 'content': 'Progress ' * 1000},
    {'type': 'text', 'content': 'Partial finding'},
    {'type': 'error', 'message': 'Provider stopped'},
  ]})
  assert _attempt_result(db, chat, run) == 'Partial finding\n\nProvider stopped'


def test_legacy_result_uses_latest_text_but_keeps_content_only_history_readable(db):
  chat = _test_chat(db, [{'role': 'assistant', 'content': 'old narration + report',
    'blocks': [{'type': 'text', 'content': 'old narration'},
               {'type': 'text', 'content': 'report'}]}])
  assert _assistant_result(chat) == 'report'
  transcript_rows.replace_all(db, chat, [{'role': 'assistant', 'content': 'Legacy content-only report'}])
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


def test_blank_followup_over_unattributed_legacy_history_has_no_result(db):
  chat = _test_chat(db, [{'role': 'assistant', 'content': 'Old success'}])
  run = SimpleNamespace(id='new', continuation_json={})
  assert _attempt_result(db, chat, run) == ''
  assert _attempt_result(db, chat, None) == 'Old success'


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


def test_explicit_result_keeps_a_later_failure_actionable(db):
  chat = _test_chat(db, [{'id': 'run', 'role': 'assistant',
    'result': 'Substantive findings', 'blocks': [
      {'type': 'text', 'content': 'Narration'},
      {'type': 'error', 'message': 'DELEGATION_WRITE_REVIEW_REQUIRED: Inspect before retry'},
    ]}])
  assert _assistant_result(chat, run_ids={'run'}) == (
    'Substantive findings\n\nDELEGATION_WRITE_REVIEW_REQUIRED: Inspect before retry')


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


def _lineage(db, chat, reason, *, mutate=None):
  from datetime import UTC, datetime, timedelta
  now = datetime.now(UTC)
  first = models.ChatRun(id='first', chat_id=chat.id, root_run_id='first',
                         provider='claude', status='interrupted', started_at=now)
  resumed = models.ChatRun(id='resumed', chat_id=chat.id, root_run_id='first',
                           provider='claude', status='completed',
                           started_at=now + timedelta(seconds=1),
                           continuation_json=({'reason': reason, 'supersedes_run_token': 'first'}
                                              if reason else None))
  db.add_all([first, resumed]); db.flush()
  if mutate is not None:
    mutate(db, first, resumed)
  db.commit()
  return resumed


@pytest.mark.parametrize('reason', ['restart', 'usage_limit', 'memory', 'storage', 'model_capacity', 'compaction'])
def test_resource_or_restart_continuation_keeps_prior_report_when_resumed_run_is_blank(chat, db, reason):
  resumed = _lineage(db, chat, reason)
  transcript_rows.replace_all(db, chat, [
    {'id': 'first:assistant:1', 'role': 'assistant', 'blocks': [
      {'type': 'text', 'content': 'Progress ' * 50},
      {'type': 'text', 'content': 'Findings before the restart'}]},
    {'id': 'resumed:assistant:1', 'role': 'assistant', 'blocks': []},
  ])
  assert _attempt_result(db, chat, resumed) == 'Findings before the restart'
  # The resumed run's own report supersedes the predecessor's.
  transcript_rows.append(db, chat, {'id': 'resumed:assistant:2', 'role': 'assistant', 'result': 'Final report'})
  assert _attempt_result(db, chat, resumed) == 'Final report'


def test_failed_resumed_run_keeps_prior_report_and_its_own_error(chat, db):
  resumed = _lineage(db, chat, 'usage_limit')
  transcript_rows.replace_all(db, chat, [
    {'id': 'first:assistant:1', 'role': 'assistant', 'blocks': [
      {'type': 'text', 'content': 'Partial findings'},
      {'type': 'error', 'message': 'Usage limit reached'}]},
    {'id': 'resumed:assistant:1', 'role': 'assistant', 'blocks': [
      {'type': 'error', 'message': 'Usage limit reached again'}]},
  ])
  assert _attempt_result(db, chat, resumed) == (
    'Partial findings\n\nUsage limit reached again')


def _foreign_chat(db, first, resumed):
  db.add(create_chat(id='foreign-chat', title='Foreign', messages=[])); db.flush()
  first.chat_id = 'foreign-chat'


@pytest.mark.parametrize('case,reason,mutate', [
  ('fresh-followup', None, None),
  ('manual-resume', 'manual', None),
  ('goal-settlement', 'goal_settlement', None),
  ('different-root', 'restart', lambda db, first, resumed: setattr(first, 'root_run_id', 'other')),
  ('different-app', 'restart', lambda db, first, resumed: setattr(first, 'initiated_by_app_id', 7)),
  ('foreign-chat', 'restart', _foreign_chat),
])
def test_continuation_never_borrows_prior_report_outside_exact_lineage(chat, db, case, reason, mutate):
  resumed = _lineage(db, chat, reason, mutate=mutate)
  transcript_rows.replace_all(db, chat, [
    {'id': 'first:assistant:1', 'role': 'assistant', 'result': 'Stale report'},
    {'id': 'resumed:assistant:1', 'role': 'assistant', 'blocks': []},
  ])
  assert _attempt_result(db, chat, resumed) == ''


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


def test_wake_notice_delivers_full_result_without_a_truncation_pointer(chat, db):
  report = ('Full findings beyond the former cap. ' * 200).strip()
  transcript_rows.replace_all(db, chat, [{'id': 'long-report', 'role': 'assistant', 'result': report}])
  db.add(models.ChatRun(id='long-report', chat_id=chat.id, status='completed'))
  db.commit()
  row = models.Delegation(id='wake-helper', parent_chat_id='parent',
                          child_chat_id=chat.id, task_key='inspect')
  notice = _compose_wake_notice(db, [row], {row.id: 'long-report'})
  items = json.loads(notice.rsplit('<delegation_results>', 1)[1].split('</delegation_results>')[0])
  assert items[0]['result'] == report
  assert items[0]['run_id'] == 'long-report'
  assert 'result_truncated' not in items[0]
  assert 'include_history=true when a truncated result' not in notice


def test_non_host_claude_success_report_is_the_final_text_not_narration(chat, db, monkeypatch):
  """Without the helper host, Claude's final message is the segment's last text.

  ResultMessage.result carries that same final text on success, so the runner
  needs no second report source; narration earlier in the turn never wins.
  """
  from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
  from app.claude_events import dispatch_sdk_message
  async def scenario():
    sink = sink_for(chat)
    sid = None
    for message in [
      AssistantMessage(content=[TextBlock(text='Looking at another source. ' * 100)], model='claude'),
      AssistantMessage(content=[ToolUseBlock(id='read', name='Read', input={'file_path': '/x'})], model='claude'),
      AssistantMessage(content=[TextBlock(text='Verdict: fix the boundary.')], model='claude'),
      ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                    num_turns=2, session_id='s', result='Verdict: fix the boundary.'),
    ]:
      sid, _ = dispatch_sdk_message(message, sink, sid)
    await sink.finalize()
  asyncio.run(scenario())
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert _assistant_result(saved, run_ids={'sink-write-test'}) == 'Verdict: fix the boundary.'


# Mixed hook/stream ordering. Supported ordering (claude_agent_sdk Query: one
# stdout reader buffers messages and spawns hook handlers): a PreToolUse hook
# may run before _read drains messages emitted earlier, but never before a
# message emitted after it, because the CLI blocks on the hook's answer.
def _tool(tool_use_id, name='Read', **tool_input):
  from claude_agent_sdk.types import ToolUseBlock
  return ToolUseBlock(id=tool_use_id, name=name, input=tool_input)


def _report():
  from app.claude_helper_host import HelperReport
  return HelperReport()


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


# Final-message rule from upstream #1662: the report is the last text block
# plus the latest error; earlier narration is never replayed to the parent.
UPSTREAM_TOKEN = 'helper-result-test'
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
    get_writer().submit(StartTurn(chat_id=chat.id, run_token=UPSTREAM_TOKEN,
      user_msg={'role': 'user', 'content': 'Test', 'ts': 10},
      title_source='Test')).result(timeout=5)
    sink = ChatEventSink(ChatBroadcast(chat.id), chat.id, run_token=UPSTREAM_TOKEN)
    for event in _narration_then_report(separator):
      sink.publish(event)
    if terminal_status == 'failed':
      sink.publish({'type': 'error', 'message': 'Provider ended unexpectedly'})
    await sink.finalize()
    get_writer().submit(FinishRun(chat_id=chat.id, run_token=UPSTREAM_TOKEN,
                                 terminal_status=terminal_status)).result(timeout=5)
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    # The child transcript keeps the full narration as evidence.
    assert NARRATION.strip() in transcript_rows.at(db, saved, -1)['content']
    row = models.Delegation(id='result-helper', parent_chat_id='parent',
                            child_chat_id=chat.id, task_key='inspect')
    notice = _compose_wake_notice(db, [row], {row.id: UPSTREAM_TOKEN})
    assert REPORT in notice and 'Looking at another' not in notice
    assert f'"status":"{terminal_status}"' in notice
    items = json.loads(notice.rsplit('<delegation_results>', 1)[1].split('</delegation_results>')[0])
    expected = REPORT + ('\n\nProvider ended unexpectedly' if terminal_status == 'failed' else '')
    assert items[0]['result'] == expected
    assert ('Provider ended unexpectedly' in notice) == (terminal_status == 'failed')
  asyncio.run(scenario())


def test_result_keeps_latest_text_and_error_with_content_only_fallback(db):
  chat = _test_chat(db, [{'role': 'assistant', 'content': 'old narration + report',
    'blocks': [{'type': 'text', 'content': 'old narration'},
               {'type': 'text', 'content': 'report'},
               {'type': 'error', 'message': 'Provider stopped'}]}])
  assert _assistant_result(chat) == 'report\n\nProvider stopped'
  transcript_rows.replace_all(db, chat, [{'role': 'assistant', 'content': 'Legacy content-only report'}])
  assert _assistant_result(chat) == 'Legacy content-only report'
