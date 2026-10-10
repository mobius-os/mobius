"""Recorded helper questions use the existing Codex clean-end boundary."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.codex_sdk_runner import ActiveCodexTurn
from app.owner_card_receipts import turn_end_receipt_id
from app.runner_registry import registry
from tests.test_codex_owner_card_hook import _hook
from tests.test_delegation_questions import _ask, _seed


def test_recorded_ask_parent_ends_a_delegated_codex_turn(client, db, owner_token):
  row = _seed(db, 'codex-question', provider='codex')
  sink = ChatEventSink(ChatBroadcast(row.child_chat_id), row.child_chat_id,
                       run_token='ask-codex-question')
  interrupts = []

  async def interrupt():
    interrupts.append('ack')

  async def scenario():
    active = ActiveCodexTurn(SimpleNamespace(id='thread'), object(), chat_id=row.child_chat_id)
    active.turn = SimpleNamespace(interrupt=interrupt)
    registry.register(active)
    register_active_sink(row.child_chat_id, sink)
    try:
      asked = _ask(client, db, row, 'ask-codex-question')
      assert asked.status_code == 200, asked.text
      receipt = {**asked.json(), 'state': 'turn_end'}
      assert sink.ends_turn(receipt['turn_end_id'])
      assert not active.owner_card_requested  # Saving cannot cut its in-flight tool.
      sink.publish({'type': 'tool_start', 'tool': 'mcp__mobius_control__ask_parent',
                    'tool_use_id': 'ask', 'input': {'question': 'Which database?'}})
      sink.publish({'type': 'tool_output', 'tool_use_id': 'ask',
                    'content': json.dumps(receipt), 'output_complete': True, 'output_exit_code': 0})
      # Ownership is synchronous: a queued interrupted terminal is a clean end.
      assert active.owner_card_requested
      await active.wait_for_owner_card_end()
      assert interrupts == ['ack']
      assert not active._interrupt_requested
    finally:
      registry.unregister(row.child_chat_id, active.kind)
      unregister_active_sink(row.child_chat_id, sink)

  asyncio.run(scenario())


@pytest.mark.parametrize('case', ['refused', 'foreign', 'incomplete', 'failed-command', 'derived-failure'])
def test_question_receipt_never_ends_a_refused_failed_or_foreign_turn(chat, case):
  sink = ChatEventSink(ChatBroadcast(chat.id), chat.id, run_token='receipt-test')
  receipt = {'state': 'turn_end', 'turn_end_id': sink.record_turn_end()}
  if case == 'foreign':
    receipt['turn_end_id'] = 'another-run'
  content = json.dumps(receipt)
  if case == 'refused':
    content = json.dumps({'isError': True, 'content': [{'text': content}]})
  if case == 'derived-failure':
    content = 'Exit code 1\n' + content
  claims = []
  sink._request_finish_turn_after_owner_card = claims.append
  sink.publish({'type': 'tool_start', 'tool': 'Bash', 'tool_use_id': 'ask', 'input': 'ask'})
  sink.publish({'type': 'tool_output', 'tool_use_id': 'ask', 'content': content,
                'output_complete': case != 'incomplete',
                'output_exit_code': 1 if case == 'failed-command' else None})
  assert claims == []


def test_failed_hook_keeps_the_completed_question_fallback(chat, monkeypatch, capsys):
  sink = ChatEventSink(ChatBroadcast(chat.id), chat.id, run_token='fallback-test')
  receipt = {'state': 'turn_end', 'turn_end_id': sink.record_turn_end()}
  hook = _hook()
  monkeypatch.setenv('CHAT_ID', chat.id)
  monkeypatch.delenv('MOBIUS_THREAD_ENV_LINKS', raising=False)
  def broken(*_args):
    raise RuntimeError('receipt validation unavailable')
  monkeypatch.setattr(hook, '_agent_api_call', broken)
  monkeypatch.setattr(hook.sys, 'stdin', io.StringIO(json.dumps({
    'hook_event_name': 'PostToolUse', 'tool_response': receipt})))
  hook.main()
  output = capsys.readouterr()
  assert json.loads(output.out) == {}
  assert 'Turn-end hook failed' in output.err
  claims = []
  sink._request_finish_turn_after_owner_card = claims.append
  sink.publish({'type': 'tool_start', 'tool': 'Bash', 'tool_use_id': 'ask', 'input': 'ask'})
  sink.publish({'type': 'tool_output', 'tool_use_id': 'ask',
                'content': json.dumps(receipt), 'output_complete': False})
  sink.publish({'type': 'tool_output', 'tool_use_id': 'ask',
                'content': '', 'output_complete': True, 'output_exit_code': 0})
  assert claims == [receipt['turn_end_id']]


@pytest.mark.parametrize('wrapper', [lambda r: {'isError': True, **r},
                                     lambda r: {'success': False, 'result': r},
                                     lambda r: {'interrupted': True, 'stdout': json.dumps(r)}])
def test_refused_envelopes_do_not_mint_turn_end_receipts(wrapper):
  assert turn_end_receipt_id(wrapper({'state': 'turn_end', 'turn_end_id': 'mine'})) is None


def test_turn_end_hook_uses_the_shared_helpers_thread_identity(tmp_path, monkeypatch):
  from app.helper_hosts import THREAD_ENV_LINKS_ENV, TurnEnvFile
  hook = _hook()
  links = tmp_path / 'links'
  links.mkdir()
  env = TurnEnvFile(tmp_path, 'helper', {'CHAT_ID': 'child', 'AGENT_TOKEN': 'test-token'})
  env.link_thread(links, 'thread-1')
  monkeypatch.setenv(THREAD_ENV_LINKS_ENV, str(links))
  monkeypatch.setenv('CHAT_ID', 'not-the-child')
  monkeypatch.setenv('AGENT_TOKEN', 'wrong-token')
  seen = []
  def api(*args):
    seen.append((args, hook.os.environ.get('AGENT_TOKEN')))
  monkeypatch.setattr(hook, '_agent_api_call', api)
  hook.finish_card_tool({'hook_event_name': 'PostToolUse', 'session_id': 'thread-1',
                        'tool_response': {'state': 'turn_end', 'turn_end_id': 'mine'}})
  assert seen == [(('POST', '/api/chats/child/turn-end', {'receipt_id': 'mine'}), 'test-token')]
  hook.finish_card_tool({'hook_event_name': 'PostToolUse', 'session_id': 'missing',
                        'tool_response': {'state': 'turn_end', 'turn_end_id': 'mine'}})
  assert len(seen) == 1
  env.remove()


@pytest.mark.parametrize('refused', [True, False])
def test_hook_never_claims_an_end_for_refused_or_server_rejected_foreign_receipts(
  monkeypatch, capsys, refused,
):
  hook = _hook()
  monkeypatch.setenv('CHAT_ID', 'this-chat')
  monkeypatch.delenv('MOBIUS_THREAD_ENV_LINKS', raising=False)
  receipt = {'state': 'turn_end', 'turn_end_id': 'foreign'}
  response = {'isError': True, 'result': receipt} if refused else receipt
  calls = []
  def reject(*args):
    calls.append(args)
    raise RuntimeError('409: this turn did not produce that receipt')
  monkeypatch.setattr(hook, '_agent_api_call', reject)
  monkeypatch.setattr(hook.sys, 'stdin', io.StringIO(json.dumps({
    'hook_event_name': 'PostToolUse', 'tool_response': response})))
  hook.main()
  captured = capsys.readouterr()
  assert json.loads(captured.out) == {}
  assert len(calls) == (0 if refused else 1)
  assert ('409' in captured.err) == (not refused)


def test_removed_closing_save_state_cannot_end_a_turn():
  assert turn_end_receipt_id({'state': 'saved_turn_ends', 'turn_end_id': 'legacy-schema'}) is None


def test_recorded_ask_parent_shape_names_the_exact_turn_end_receipt():
  receipt = {'question_id': 'q-1', 'status': 'asked', 'note': 'End your turn now.',
             'turn_end_id': 'end-1', 'state': 'turn_end'}
  assert turn_end_receipt_id(json.dumps(receipt)) == 'end-1'


def test_turn_end_receipt_parser_keeps_bounded_work():
  receipt = {'state': 'turn_end', 'turn_end_id': 'mine'}
  assert turn_end_receipt_id('x' * 32_769 + '\n' + json.dumps(receipt)) is None
  assert turn_end_receipt_id({'content': [None] * 12 + [receipt]}) is None
  assert turn_end_receipt_id('\n'.join(['status'] * 8 + [json.dumps(receipt)])) is None


def test_pretty_printed_refusal_cannot_bypass_its_envelope_via_a_receipt_line():
  receipt = {'state': 'turn_end', 'turn_end_id': 'mine'}
  result = '{"isError": true, "content": [\n' + json.dumps(receipt) + '\n]}'
  assert turn_end_receipt_id(result) is None


@pytest.mark.parametrize('kind', ['question', 'owner-card'])
def test_json_leading_log_and_standalone_receipt_are_both_framed_records(kind):
  receipt = ({'state': 'turn_end', 'turn_end_id': 'mine'} if kind == 'question' else
             {'state': 'waiting_for_owner', 'question_id': 'mine', 'next_action': 'End'})
  output = json.dumps({'log': 'question saved'}) + '\n' + json.dumps(receipt)
  assert turn_end_receipt_id(output) == 'mine'


def test_framed_pretty_refusal_never_exposes_its_nested_receipt():
  inner = {'state': 'turn_end', 'turn_end_id': 'mine'}
  refusal = '{"isError": true, "content": [\n' + json.dumps(inner) + '\n]}'
  assert turn_end_receipt_id(refusal + '\n' + json.dumps({'log': 'done'})) is None


def test_recorded_question_survives_a_failed_codex_terminal_without_hiding_failure(client, db, owner_token):
  from app import transcript_rows, codex_sdk_runner, models
  from app.chat_writer import FinishRun, get_writer
  from tests.test_codex_sdk_runner import _FakeTurnStatus
  row = _seed(db, 'codex-failed-question', provider='codex')
  run_id = 'ask-codex-failed-question'
  sink = ChatEventSink(ChatBroadcast(row.child_chat_id), row.child_chat_id, run_token=run_id)
  register_active_sink(row.child_chat_id, sink)
  try:
    asked = _ask(client, db, row, run_id)
    assert asked.status_code == 200, asked.text
    assert sink.ends_turn(asked.json()['turn_end_id'])
    error, status, phase = codex_sdk_runner._codex_terminal_error(
      SimpleNamespace(status=_FakeTurnStatus.failed, error=SimpleNamespace(message='Provider failed')),
      {'TurnStatus': _FakeTurnStatus}, interrupt_requested=True, completed_message_phases=[])
    assert error == 'Provider failed' and status == _FakeTurnStatus.failed.value and phase is None
    sink.publish({'type': 'error', 'message': error})
    asyncio.run(sink.finalize())
    get_writer().submit(FinishRun(chat_id=row.child_chat_id, run_token=run_id,
                                 terminal_status='failed')).result(timeout=5)
    db.expire_all()
    assert db.get(models.ChatRun, run_id).status == 'failed'
    question = db.get(models.DelegationQuestion, asked.json()['question_id'])
    assert question.asking_run_id == run_id and question.question == 'Which database?'
    assert 'Provider failed' in json.dumps(list(transcript_rows.history(db.get(models.Chat, row.child_chat_id))))
  finally:
    unregister_active_sink(row.child_chat_id, sink)


def test_ask_parent_tool_output_is_the_receipt_that_ends_the_turn(monkeypatch):
  # The control tool that records a question and the provider hook that ends
  # the turn live in different modules; one receipt shape must connect them.
  from tests.test_platform_tools import _control_module

  control = _control_module()
  monkeypatch.setenv("MOBIUS_DELEGATION_ID", "helper-1")
  monkeypatch.setattr(control, "_agent_api_call", lambda method, path, payload=None: {
    "question_id": "q-1",
    "helper_id": "helper-1",
    "status": "asked",
    "note": "Question recorded for your parent.",
    "turn_end_id": "receipt-1",
  })

  output = control._call_ask_parent({"question": "Which file?", "options": ["a", "b"]})

  assert turn_end_receipt_id(output) == "receipt-1"
