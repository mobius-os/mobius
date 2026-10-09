"""Helper identity and consumed dispatches settle on lifecycle signals."""

import asyncio
import json

import pytest

from tests.test_helper_hosts import _claude_host, _turn, _Started


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
  assert turn.started.is_set() == failed
  assert turn.dispatch_error == (('launch_failed' if tool == 'Agent' else 'unreachable') if failed else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_channel', ['post-tool', 'post-tool-failure', 'stream-result'])
async def test_failed_launch_settles_dispatch_without_waiting_or_replay(tmp_path, failure_channel):
  from claude_agent_sdk.types import UserMessage, ToolResultBlock
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
      if failure_channel == 'stream-result':
        await host._read()
      else:
        await host.post_tool_use({'tool_name': 'Agent', 'tool_response': {'success': False},
          'hook_event_name': 'PostToolUseFailure' if failure_channel == 'post-tool-failure' else 'PostToolUse'},
          'launch', None)
    async def receive_messages(self):
      yield UserMessage(content=[ToolResultBlock(tool_use_id='launch', content='failed', is_error=True)])
  host._client = Dispatcher()
  await host.run_turn(turn)
  assert turn.dispatch_error == 'launch_failed'
  assert queries == ['SPAWN d1']
  await host.close()
  assert host._dispatcher_reply is None


@pytest.mark.asyncio
async def test_consumed_dispatch_never_replays_after_an_ambiguous_reply(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      host._dispatcher_call('Agent', {'description': turn.dispatch_id}, 'launch')
      host._dispatcher_replied()  # No start or definite failure yet.
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
async def test_failed_consumed_dispatch_cannot_admit_a_late_identity(tmp_path):
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  host._turn_by_tool_use['launch'] = turn
  host._fail_dispatch('launch', 'Agent')
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
  host._dispatcher_replied()
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
  host._dispatcher_replied()
  assert await submitted.get() == 'SPAWN b'
  assert not a.done.is_set()  # B starts while A's helper is still working.
  await asyncio.sleep(0)
  assert not host._dispatcher_reply.done()  # A's reply cannot release B.
  host._dispatcher_replied()
  a.finish('completed'); b.finish('completed')
  await asyncio.gather(arun, brun)
  assert queries == ['SPAWN a', 'SPAWN b']
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
  host._dispatcher_replied()  # Drain only cancelled A.
  assert await submitted.get() == 'SPAWN b'
  assert not host._dispatcher_reply.done()
  host._dispatcher_replied()
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
      host._dispatcher_replied(SimpleNamespace(is_error=True))
  host._client = Dispatcher()
  assert (await host.run_turn(turn)).dispatch_error == 'launch_failed'
  assert turn.done.is_set()
  await host.close()


@pytest.mark.asyncio
async def test_query_error_retires_admissions_so_a_late_reply_cannot_complete_another_request(tmp_path):
  host = _claude_host(tmp_path)
  a, b = _turn(tmp_path, dispatch_id='a'), _turn(tmp_path, dispatch_id='b')
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      host._dispatcher_call('Agent', {'description': 'a'}, 'launch-a')
      raise RuntimeError('transport failed after admission')
  host._client = Dispatcher()
  assert (await host.run_turn(a)).dispatch_error == 'launch_failed'
  host._dispatcher_replied()  # An ambiguous late A result cannot reopen this stream.
  assert (await host.run_turn(b)).dispatch_error == 'launch_failed'
  assert queries == ['SPAWN a'] and not b.dispatch_consumed
  assert host._dispatcher_reply is None
  await host.close()


@pytest.mark.asyncio
async def test_pre_submission_query_error_fails_queued_admissions_without_stopping_active_siblings(tmp_path):
  host = _claude_host(tmp_path)
  host._leases = 1  # An active sibling owns this host until its turn settles.
  sibling = _turn(tmp_path, dispatch_id='sibling')
  sibling.started.set()
  host._turn_by_agent['sibling'] = sibling
  a, b, c = [_turn(tmp_path, dispatch_id=key) for key in ['a', 'b', 'c']]
  entered, fail = asyncio.Event(), asyncio.Event()
  queries = []
  class Dispatcher:
    async def query(self, line):
      queries.append(line)
      entered.set()
      await fail.wait()
      raise RuntimeError('failed before submission')
  host._client = Dispatcher()
  arun = asyncio.create_task(host.run_turn(a))
  await entered.wait()
  brun = asyncio.create_task(host.run_turn(b))
  await asyncio.sleep(0)
  fail.set()
  assert (await arun).dispatch_error == 'launch_failed'
  assert (await brun).dispatch_error == 'launch_failed'
  assert (await host.run_turn(c)).dispatch_error == 'launch_failed'
  assert queries == ['SPAWN a'] and not any(t.dispatch_consumed for t in [a, b, c])
  assert not host._specs and not sibling.done.is_set() and host.alive
  assert await host._turn_for_agent('sibling') is sibling
  host._leases = 0
  assert not host.alive  # The next lease replaces it through the existing manager.
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
  host._dispatcher_replied()
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
