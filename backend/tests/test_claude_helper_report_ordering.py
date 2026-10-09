"""Report selection remains exact when hook callbacks lag the child stream."""

import pytest
from claude_agent_sdk.types import TextBlock

from tests.test_helper_result_delivery import _stream_report, _child, _child_result, _handback


@pytest.mark.parametrize('hook_first', [True, False])
@pytest.mark.parametrize('rejected', [True, False])
@pytest.mark.parametrize('stop_first', [True, False])
def test_handback_hook_stream_and_terminal_permutations_deliver_one_report(
  tmp_path, hook_first, rejected, stop_first,
):
  async def script(host):
    yield _child(TextBlock(text='Earlier progress'))
    hook, message = _handback(host, 'hb', {'message': 'Handback report'})
    if hook_first:
      await hook
    yield message
    if not hook_first:
      await hook
    yield _child_result('hb', is_error=rejected)
    # Rejection permits a revised final response; acceptance makes this closing text.
    yield _child(TextBlock(text='Revised report' if rejected else 'Done'))
    if not stop_first:
      host._on_task_end('agent-1', 'completed', 'Task summary', None)
    await host.subagent_stop({'agent_id': 'agent-1',
        'last_assistant_message': 'Revised report' if rejected else 'Done'}, None, None)
  assert _stream_report(tmp_path, script) == ['Revised report' if rejected else 'Handback report']


@pytest.mark.parametrize('hook_first', [True, False])
def test_plain_final_text_is_complete_before_a_late_stop_hook(tmp_path, hook_first):
  async def script(host):
    yield _child(TextBlock(text='Progress'))
    yield _child(TextBlock(text='Final report'))
    if not hook_first:
      host._on_task_end('agent-1', 'completed', 'Task summary', None)
    await host.subagent_stop({'agent_id': 'agent-1',
                             'last_assistant_message': 'Final report'}, None, None)
  assert _stream_report(tmp_path, script) == ['Final report']
