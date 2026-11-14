"""Helper identity and consumed dispatches settle on lifecycle signals."""

import asyncio
import json

import pytest

from app import claude_helper_host, helper_hosts
from tests.test_helper_hosts import _claude_host, _turn, _Started


def _reply(host, response=None):
  from claude_agent_sdk.types import UserMessage
  if host._dispatcher_turn is not None:
    turn = host._dispatcher_turn
    host._on_dispatcher_user_message(UserMessage(
      content=f"{'SPAWN' if turn.kind == 'spawn' else 'MESSAGE'} {turn.dispatch_id}",
      origin=None))
  host._dispatcher_replied(response)


@pytest.mark.parametrize('origin,matched', [
  (None, True), ({'kind': 'human'}, True),
  ({'kind': 'task-notification'}, False),
])
def test_dispatcher_echo_origin_matches_real_cli_shape(tmp_path, origin, matched):
  from claude_agent_sdk.types import UserMessage
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._dispatcher_turn = turn
  host._on_dispatcher_user_message(UserMessage(
    content=f'SPAWN {turn.dispatch_id}', origin=origin))
  assert (host._result_dispatch_id == turn.dispatch_id) is matched


@pytest.mark.asyncio
async def test_first_tool_waits_for_identity_even_after_two_seconds(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._turn_by_tool_use['launch'] = turn
  call = asyncio.create_task(host.pre_tool_use({
    'agent_id': 'agent', 'tool_name': 'Bash', 'tool_input': {'command': 'echo hi'}}, 'bash', None))
  await asyncio.sleep(2.05)
  assert not call.done()
  host._on_task_started(_Started('agent', 'launch'))
  decision = (await call)['hookSpecificOutput']
  assert decision['permissionDecision'] == 'allow'
  assert str(turn.env_file.path) in decision['updatedInput']['command']
  assert not host._agent_identity


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['close', 'stream-failure'])
async def test_shutdown_cancels_pending_identity_and_denies_tools(tmp_path, ending):
  host = _claude_host(tmp_path)
  call = asyncio.create_task(host.pre_tool_use({
    'agent_id': 'unknown', 'tool_name': 'Write', 'tool_input': {}}, 'write', None))
  await asyncio.sleep(0)
  identity = host._agent_identity['unknown']
  if ending == 'close':
    await host.close()
  else:
    host._fail_open_turns()
  assert identity.cancelled()
  assert (await call)['hookSpecificOutput']['permissionDecision'] == 'deny'
  assert not host._agent_identity
  assert (await host.pre_tool_use({'agent_id': 'unknown', 'tool_name': 'Bash'}, 'late', None))[
    'hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.asyncio
async def test_cancelling_one_hook_does_not_cancel_another_waiting_for_identity(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._turn_by_tool_use['launch'] = turn
  a = asyncio.create_task(host._turn_for_agent('agent'))
  b = asyncio.create_task(host._turn_for_agent('agent'))
  await asyncio.sleep(0)
  a.cancel()
  with pytest.raises(asyncio.CancelledError):
    await a
  host._on_task_started(_Started('agent', 'launch'))
  assert await b is turn


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['Agent', 'SendMessage'])
@pytest.mark.parametrize('response', [ {'success': False}, '{"success":false}',
  {'isError': True}, {'success': True, 'text': 'Example: "success": false'},
  {'success': True, 'result': {'success': False}}])
async def test_dispatch_failure_is_structural_not_a_text_search(tmp_path, tool, response):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.kind = 'spawn' if tool == 'Agent' else 'message'
  host._turn_by_tool_use['launch'] = turn
  await host.post_tool_use({'tool_name': tool, 'tool_response': response}, 'launch', None)
  failed = json.loads(response).get('success') is False if isinstance(response, str) else (
    response.get('success') is False or response.get('isError') is True)
  assert not turn.started.is_set()
  assert turn.dispatch_tool_error is failed
  assert turn.dispatch_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_channel', ['post-tool', 'post-tool-failure', 'stream-result'])
async def test_errored_dispatch_reasks_then_starts_helper(tmp_path, failure_channel):
  from claude_agent_sdk.types import UserMessage, ToolResultBlock
  from types import SimpleNamespace
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      tool_id = f'launch-{len(queries)}'
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, tool_id)
      if len(queries) == 2:
        host._on_task_started(_Started('agent', tool_id))
        host._on_task_end('agent', 'completed', 'done', None)
      elif failure_channel == 'stream-result':
        host._on_dispatcher_user_message(UserMessage(content=[
          ToolResultBlock(tool_use_id=tool_id, content='failed', is_error=True)]))
      else:
        await host.post_tool_use({'tool_name': 'Agent', 'tool_response': {'success': False},
          'hook_event_name': 'PostToolUseFailure' if failure_channel == 'post-tool-failure' else 'PostToolUse'},
          tool_id, None)
      _reply(host, SimpleNamespace(is_error=True) if len(queries) == 1 else None)
  host._client = Dispatcher()
  await host.run_turn(turn)
  assert turn.status == 'completed' and turn.dispatch_error is None
  assert queries == ['SPAWN d1'] * 2
  await host.close()
  assert host._dispatcher_reply is None


@pytest.mark.asyncio
async def test_cancelling_reask_does_not_stop_idle_resumable_helper(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.kind = 'message'
  turn.agent_id = 'idle-agent'
  host._turn_by_dispatch[turn.dispatch_id] = turn
  host._specs[turn.dispatch_id] = turn.spec
  host._dispatcher_call('SendMessage', {'to': turn.dispatch_id}, 'send')
  host._fail_dispatch('send', 'SendMessage')
  assert turn.dispatch_id in host._specs
  assert not turn.dispatch_consumed
  stopped = []
  class Client:
    async def stop_task(self, agent_id):
      stopped.append(agent_id)
  host._client = Client()
  await host.stop(turn)
  assert stopped == []
  await host.close()


@pytest.mark.asyncio
async def test_errored_dispatch_exhausts_same_reask_budget(tmp_path):
  from claude_agent_sdk.types import UserMessage, ToolResultBlock
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      tool_id = f'launch-{len(queries)}'
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, tool_id)
      host._on_dispatcher_user_message(UserMessage(content=[
        ToolResultBlock(tool_use_id=tool_id, content='transient permission denial', is_error=True)]))
      _reply(host)
  host._client = Dispatcher()
  await host.run_turn(turn)
  assert queries == ['SPAWN d1'] * 3
  assert turn.dispatch_error == 'launch_failed'
  assert 'retry budget was exhausted' in turn.summary
  assert turn.dispatch_id not in host._specs
  host._on_task_started(_Started('late', 'launch-3'))
  assert 'late' not in host._turn_by_agent
  await host.close()


@pytest.mark.asyncio
async def test_consumed_dispatch_never_replays_after_an_ambiguous_reply(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
      _reply(host)  # No start or definite failure yet.
  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  for _ in range(4):
    await asyncio.sleep(0)
  assert not running.done()
  assert queries == ['SPAWN d1']
  denied = host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'again')
  assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'
  host._fail_open_turns()
  assert (await running).host_lost
  assert queries == ['SPAWN d1']
  await host.close()
  assert host._dispatcher_reply is None


@pytest.mark.asyncio
async def test_legacy_resumed_handback_still_has_report_ordering_evidence(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  host._turn_by_agent['agent'] = turn
  decision = await host.pre_tool_use({'agent_id': 'agent', 'tool_name': 'SubagentHandback',
                                      'tool_input': {'message': 'Report'}}, 'hb', None)
  assert decision == {}
  assert turn.report.final() == 'Report'


@pytest.mark.asyncio
async def test_failure_hook_uses_its_actual_event_name_for_goal_brief(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  class Refresh:
    async def take(self):
      return 'Refreshed Goal brief'
  turn.goal_brief_refresh = Refresh()
  host._turn_by_agent['agent'] = turn
  result = await host.post_tool_use({
    'agent_id': 'agent', 'tool_name': 'Read',
    'hook_event_name': 'PostToolUseFailure',
  }, 'failed-read', None)
  assert result['hookSpecificOutput'] == {
    'hookEventName': 'PostToolUseFailure',
    'additionalContext': 'Refreshed Goal brief',
  }


@pytest.mark.asyncio
async def test_failed_consumed_dispatch_cannot_admit_a_late_identity(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._turn_by_tool_use['launch'] = turn
  host._fail_dispatch('launch', 'Agent')
  turn.finish('failed', 'Retries exhausted')
  host._on_task_started(_Started('agent', 'launch'))
  assert turn.done.is_set()
  assert 'agent' not in host._turn_by_agent


@pytest.mark.asyncio
async def test_query_exception_never_retries_consumed_work_or_leaks_a_reply(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
      raise RuntimeError('ambiguous transport failure')
  host._client = Dispatcher()
  await host.run_turn(turn)
  assert turn.dispatch_error == 'launch_failed'
  assert 'will not replay' in turn.summary
  assert queries == ['SPAWN d1']
  _reply(host)
  await asyncio.gather(*host._host_tasks)
  assert host._dispatcher_reply is None
  assert turn.dispatch_id not in host._specs
  host._on_task_started(_Started('agent', 'launch'))
  assert turn.done.is_set() and 'agent' not in host._turn_by_agent
  await host.close()


@pytest.mark.asyncio
async def test_rejected_late_start_denies_pending_and_later_identity_without_closing_siblings(tmp_path):
  host = _claude_host(tmp_path)
  failed = _turn(tmp_path, dispatch_id='failed')
  sibling = _turn(tmp_path, dispatch_id='sibling')
  sibling.started.set()
  host._turn_by_agent['sibling'] = sibling
  host._turn_by_tool_use['failed-launch'] = failed
  pending = asyncio.create_task(host.pre_tool_use({'agent_id': 'late-agent',
    'tool_name': 'Write', 'tool_input': {}}, 'write', None))
  await asyncio.sleep(0)
  host._fail_dispatch('failed-launch', 'Agent')
  failed.finish('failed', 'Retries exhausted')
  host._on_task_started(_Started('late-agent', 'failed-launch'))
  assert (await pending)['hookSpecificOutput']['permissionDecision'] == 'deny'
  assert await host._turn_for_agent('late-agent') is None
  assert not host._identity_closed and not sibling.done.is_set()
  assert await host.pre_tool_use({'agent_id': 'sibling', 'tool_name': 'Write'}, 'good', None) == {}


@pytest.mark.asyncio
async def test_dispatcher_cycles_serialize_replies_but_not_helper_lifetimes(tmp_path):
  host = _claude_host(tmp_path)
  a, b = _turn(tmp_path, dispatch_id='a'), _turn(tmp_path, dispatch_id='b')
  submitted = asyncio.Queue()
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      await submitted.put(line)
      key = line.split()[1]
      host._dispatcher_call('Agent', {'description': key}, 'launch-'+key)
      host._on_task_started(_Started('agent-'+key, 'launch-'+key))
  host._client = Dispatcher()
  arun, brun = asyncio.create_task(host.run_turn(a)), asyncio.create_task(host.run_turn(b))
  assert await submitted.get() == 'SPAWN a'
  await asyncio.sleep(0)
  assert queries == ['SPAWN a']  # B cannot register a reply before its query.
  _reply(host)
  assert await submitted.get() == 'SPAWN b'
  assert not a.done.is_set()  # B starts while A's helper is still working.
  await asyncio.sleep(0)
  assert not host._dispatcher_reply.done()  # A's reply cannot release B.
  _reply(host)
  a.finish('completed'); b.finish('completed')
  await asyncio.gather(arun, brun)
  assert queries == ['SPAWN a', 'SPAWN b']
  await host.close()


@pytest.mark.asyncio
async def test_missing_echo_times_out_without_failing_started_helper_or_blocking_next_spawn(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(claude_helper_host, 'DISPATCH_REPLY_TIMEOUT_SECONDS', 0.03)
  host = _claude_host(tmp_path)
  first, second = _turn(tmp_path, dispatch_id='a'), _turn(tmp_path, dispatch_id='b')
  queries = []

  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      key = line.split()[1]
      host._dispatcher_call('Agent', {'description': key}, 'launch-'+key)
      host._on_task_started(_Started('agent-'+key, 'launch-'+key))
      if key == 'b':
        _reply(host)
      else:
        # A Result without its echoed user message cannot match the request.
        host._dispatcher_replied()

  host._client = Dispatcher()
  arun = asyncio.create_task(host.run_turn(first))
  await asyncio.wait_for(first.started.wait(), 1)
  brun = asyncio.create_task(host.run_turn(second))
  await asyncio.wait_for(second.started.wait(), 1)
  assert queries == ['SPAWN a', 'SPAWN b']
  assert first.dispatch_error is None and not first.done.is_set()
  first.finish('completed'); second.finish('completed')
  assert (await arun).status == (await brun).status == 'completed'
  assert host.alive
  await host.close()


@pytest.mark.asyncio
async def test_missing_echo_fails_unstarted_request_visibly_and_releases_host(tmp_path, monkeypatch):
  monkeypatch.setattr(claude_helper_host, 'DISPATCH_REPLY_TIMEOUT_SECONDS', 0.03)
  host = _claude_host(tmp_path)

  class Dispatcher:
    async def query(self, line):
      host._dispatcher_replied()

  host._client = Dispatcher()
  turn = await asyncio.wait_for(host.run_turn(_turn(tmp_path)), 1)
  assert turn.dispatch_error == 'reply_timeout'
  assert turn.status == 'failed' and 'did not confirm' in turn.summary
  assert not host._query_lock.locked() and host.alive
  await host.close()


@pytest.mark.asyncio
async def test_notice_result_before_requested_call_does_not_consume_reply(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  sent = asyncio.Queue()
  class Dispatcher:
    async def query(self, line):
      await sent.put(line)
  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  assert await sent.get() == f'SPAWN {turn.dispatch_id}'
  from claude_agent_sdk.types import UserMessage
  host._on_dispatcher_user_message(UserMessage(
    content='NOTICE unrelated', origin={'kind': 'task-notification'}))
  host._dispatcher_replied()  # Independent task-notification turn.
  await asyncio.sleep(0)
  assert not host._dispatcher_reply.done()
  assert turn.dispatch_id in host._specs
  host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
  host._on_task_started(_Started('agent', 'launch'))
  _reply(host)  # Result of the requested call.
  turn.finish('completed')
  assert (await running).status == 'completed'
  await host.close()


@pytest.mark.asyncio
async def test_task_notice_between_command_echo_and_reply_keeps_dispatch_correlation(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  sent = asyncio.Queue()

  class Dispatcher:
    async def query(self, line):
      await sent.put(line)

  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  assert await sent.get() == f'SPAWN {turn.dispatch_id}'
  from claude_agent_sdk.types import UserMessage
  host._on_dispatcher_user_message(UserMessage(
    content=f'SPAWN {turn.dispatch_id}', origin=None))
  host._on_dispatcher_user_message(UserMessage(
    content='Task completed', origin={'kind': 'task-notification'}))
  host._dispatcher_replied()
  assert await asyncio.wait_for(sent.get(), 1) == f'SPAWN {turn.dispatch_id}'
  host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
  host._on_task_started(_Started('agent', 'launch'))
  _reply(host)
  assert turn.dispatch_error is None
  turn.finish('completed')
  assert (await running).status == 'completed'
  await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['query', 'reply', 'queued'])
async def test_stop_withdraws_an_unconsumed_dispatch_even_while_query_is_pending(tmp_path, phase):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  submitted, finish_query = asyncio.Event(), asyncio.Event()
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      submitted.set()
      await finish_query.wait()
  host._client = Dispatcher()
  if phase == 'queued':
    await host._query_lock.acquire()
  running = asyncio.create_task(host.run_turn(turn))
  if phase == 'queued':
    await asyncio.sleep(0)
  else:
    await submitted.wait()
  if phase == 'reply':
    finish_query.set()
    await asyncio.sleep(0)
  await host.stop(turn)
  assert (await running).status == 'stopped'
  assert turn.dispatch_id not in host._specs
  assert host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'late')[
    'hookSpecificOutput']['permissionDecision'] == 'deny'
  if phase == 'queued':
    host._query_lock.release()
    assert not queries
  await host.close()
  assert not host._host_tasks


@pytest.mark.asyncio
async def test_cancelled_query_drains_its_reply_before_the_next_dispatch(tmp_path):
  host = _claude_host(tmp_path)
  a, b = _turn(tmp_path, dispatch_id='a'), _turn(tmp_path, dispatch_id='b')
  submitted = asyncio.Queue()
  finish_query = asyncio.Event()
  class Dispatcher:
    async def query(self, line):
      await submitted.put(line)
      if line == 'SPAWN a':
        await finish_query.wait()
      else:
        host._dispatcher_call('Agent', {'description': 'b'}, 'launch-b')
        host._on_task_started(_Started('agent-b', 'launch-b'))
  host._client = Dispatcher()
  arun = asyncio.create_task(host.run_turn(a))
  assert await submitted.get() == 'SPAWN a'
  arun.cancel()
  with pytest.raises(asyncio.CancelledError):
    await arun
  brun = asyncio.create_task(host.run_turn(b))
  finish_query.set()
  await asyncio.sleep(0)
  assert submitted.empty()
  _reply(host)  # Drain only cancelled A.
  assert await submitted.get() == 'SPAWN b'
  assert not host._dispatcher_reply.done()
  _reply(host)
  b.finish('completed')
  await brun
  await host.close()


@pytest.mark.asyncio
async def test_host_closure_settles_a_query_that_never_completes(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  submitted = asyncio.Event()
  class Dispatcher:
    async def query(self, line):
      submitted.set()
      await asyncio.Future()
  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  await submitted.wait()
  await host.close()
  assert (await running).status == 'failed'
  assert host._dispatcher_reply is None and not host._host_tasks


@pytest.mark.asyncio
async def test_provider_dispatch_completion_error_settles_consumed_admission(tmp_path):
  from types import SimpleNamespace
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  class Dispatcher:
    async def query(self, line):
      host._dispatcher_call('Agent', {'description': 'd1'}, 'launch')
      _reply(host, SimpleNamespace(is_error=True))
  host._client = Dispatcher()
  assert (await host.run_turn(turn)).dispatch_error == 'launch_failed'
  assert turn.done.is_set()
  await host.close()


@pytest.mark.asyncio
async def test_no_call_reply_is_reasked_twice_then_fails_promptly(tmp_path):
  host = _claude_host(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      _reply(host)
  host._client = Dispatcher()
  turn = await host.run_turn(_turn(tmp_path))
  assert turn.dispatch_error == 'not_called'
  assert queries == ['SPAWN d1'] * 3
  assert host.alive
  await host.close()


@pytest.mark.asyncio
async def test_query_error_preserves_session_and_next_request_and_started_sibling(tmp_path):
  host = _claude_host(tmp_path)
  host._save_session('saved-session')
  sibling = _turn(tmp_path, dispatch_id='sibling')
  sibling.started.set()
  sibling.agent_id = 'sibling-agent'
  host._turn_by_agent['sibling-agent'] = sibling
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      if line == 'SPAWN a':
        raise RuntimeError('query failed')
      host._dispatcher_call('SendMessage', {'to': 'b'}, 'message-b')
      host._on_task_started(_Started('sibling-agent', 'message-b'))
      _reply(host)
  host._client = Dispatcher()
  assert (await host.run_turn(_turn(tmp_path, dispatch_id='a'))).dispatch_error == 'launch_failed'
  assert host._saved_session() == 'saved-session'
  assert not sibling.done.is_set() and host.alive
  b = _turn(tmp_path, dispatch_id='b')
  b.kind = 'message'
  b.agent_id = 'sibling-agent'
  b.spec = {'to': 'sibling-agent', 'summary': 'b', 'message': 'follow up'}
  running = asyncio.create_task(host.run_turn(b))
  await b.started.wait()
  b.finish('completed')
  assert (await running).status == 'completed'
  assert queries == ['SPAWN a', 'MESSAGE b']
  await host.close()


@pytest.mark.asyncio
async def test_started_helper_survives_late_dispatcher_reply(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  class Dispatcher:
    async def query(self, line):
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
      host._on_task_started(_Started('agent', 'launch'))
  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  await turn.started.wait()
  await asyncio.sleep(0)
  assert not turn.done.is_set() and turn.dispatch_error is None and host.alive
  _reply(host)
  turn.finish('completed')
  assert (await running).status == 'completed'
  await host.close()


@pytest.mark.asyncio
async def test_followup_clears_prior_rejected_identity_before_first_tool(tmp_path):
  host = _claude_host(tmp_path)
  host._reject_identity('agent')
  turn = _turn(tmp_path)
  turn.kind = 'message'
  turn.agent_id = 'agent'
  class Dispatcher:
    async def query(self, line):
      host._dispatcher_call('SendMessage', {'to': turn.dispatch_id}, 'send')
      _reply(host)
  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  await asyncio.sleep(0)
  first_tool = asyncio.create_task(host.pre_tool_use(
    {'agent_id': 'agent', 'tool_name': 'Write', 'tool_input': {}}, 'write', None))
  await asyncio.sleep(0)
  assert not first_tool.done()
  host._on_task_started(_Started('agent', 'send'))
  assert await first_tool == {}
  turn.finish('completed')
  assert (await running).status == 'completed'
  await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['stop', 'query-error'])
async def test_consumed_admission_is_stopped_when_a_late_start_supplies_identity(tmp_path, failure):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  sibling = _turn(tmp_path, dispatch_id='sibling')
  sibling.started.set()
  host._turn_by_agent['sibling'] = sibling
  admitted, stopped = asyncio.Event(), asyncio.Event()
  stop_ids = []
  class Dispatcher:
    async def query(self, line):
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
      admitted.set()
      if failure == 'query-error':
        raise RuntimeError('transport failed after admission')
    async def stop_task(self, agent_id):
      stop_ids.append(agent_id)
      stopped.set()
  host._client = Dispatcher()
  running = asyncio.create_task(host.run_turn(turn))
  await admitted.wait()
  if failure == 'stop':
    await host.stop(turn)
  await running
  assert turn.dispatch_consumed
  pending = asyncio.create_task(host.pre_tool_use({'agent_id': 'late-agent',
    'tool_name': 'Write'}, 'write', None))
  await asyncio.sleep(0)
  host._on_task_started(_Started('late-agent', 'launch'))
  assert (await pending)['hookSpecificOutput']['permissionDecision'] == 'deny'
  await stopped.wait()
  assert stop_ids == ['late-agent']
  assert not sibling.done.is_set() and not host._closed
  _reply(host)
  await host.close()


@pytest.mark.asyncio
async def test_late_followup_start_after_reply_timeout_is_not_stopped(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path, dispatch_id='followup')
  turn.kind = 'message'
  turn.agent_id = 'agent'
  turn.dispatch_consumed = True
  turn.dispatch_error = 'reply_timeout'
  turn.finish('failed')
  host._turn_by_tool_use['send'] = turn
  stopped = []

  class Dispatcher:
    async def stop_task(self, agent_id):
      stopped.append(agent_id)

  host._client = Dispatcher()
  host._on_task_started(_Started('agent', 'send'))
  await asyncio.sleep(0)
  assert stopped == []
  assert turn.agent_id == 'agent'  # The later follow-up may still resume it.
  await host.close()


@pytest.mark.asyncio
async def test_a_failed_followup_owns_rejection_even_when_start_uses_original_launch_id(tmp_path):
  host = _claude_host(tmp_path)
  first = _turn(tmp_path, dispatch_id='first')
  first.agent_id = 'agent'
  first.finish('completed')
  host._turn_by_tool_use['original-launch'] = first
  followup = _turn(tmp_path, dispatch_id='followup')
  followup.agent_id = 'agent'
  followup.dispatch_consumed = True
  followup.dispatch_error = 'launch_failed'
  followup.finish('failed')
  host._turn_by_agent['agent'] = followup
  stopped = asyncio.Event()
  stop_ids = []
  class Client:
    async def stop_task(self, agent_id):
      stop_ids.append(agent_id)
      stopped.set()
  host._client = Client()
  host._on_task_started(_Started('agent', 'original-launch'))
  await stopped.wait()
  assert stop_ids == ['agent']
  assert await host._turn_for_agent('agent') is None
  await host.close()


@pytest.mark.asyncio
async def test_a_closed_host_cannot_leave_a_new_admission_waiting_forever(tmp_path):
  host = _claude_host(tmp_path)
  await host.close()
  turn = _turn(tmp_path)
  assert (await host.run_turn(turn)).host_lost and turn.status == 'failed'


@pytest.mark.parametrize('exit_code,oom_kills_after,expected', [
  (-9, 4, claude_helper_host.HOST_OUT_OF_MEMORY),
  (-9, 3, 'The helper host stopped unexpectedly.'),
  (1, 4, 'The helper host stopped unexpectedly.'),
])
def test_host_death_names_memory_only_with_kill_evidence(
  tmp_path, monkeypatch, exit_code, oom_kills_after, expected,
):
  """A memory kill ends every helper in the shared host; the failure must say
  so, while any other death keeps the generic wording."""
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._turn_by_dispatch[turn.dispatch_id] = turn
  host._oom_kills_at_start = 3
  host._exit_code = exit_code
  monkeypatch.setattr(
    'app.memory_observability.cgroup_oom_kill_count', lambda **_: oom_kills_after,
  )
  host._fail_open_turns()
  assert turn.host_lost and turn.status == 'failed'
  assert turn.summary == expected
