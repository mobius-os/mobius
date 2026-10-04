"""The real shared sink and SDK normalizers, with synthetic external effects."""
import asyncio
import json
import importlib.util
from pathlib import Path

import pytest
from claude_agent_sdk.types import AssistantMessage, StreamEvent, TextBlock

from app import agent_write_tools, models
from app.agent_write_channel import WriteIntent, frame
from app.agent_write_delivery import WriteOutcome
from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink
from app.chat_writer import ReadAgentWriteOutcomes, StartTurn, get_writer
from app.claude_events import dispatch_sdk_message
from app.codex_events import _tool_completed_events
from app.codex_sdk_runner import _sdk_imports

NONCE='sink_fixture_nonce_123456'
WRITE=WriteIntent('capture','checkpoint_chat',{'summary':'private synthetic fact'})


async def saved_outcomes(sink):
  delivery = sink._write_delivery
  return await delivery._command(ReadAgentWriteOutcomes(**delivery.owner))


def test_quiet_checkpoint_uses_existing_title_digest_summary_handler_once(
    client,chat,db,monkeypatch):
  from app.auth import create_agent_token
  from app.chat_continuity import note_path,recovery_source
  from app.chat_notes import extract_cumulative_summary,extract_section
  from app.config import get_settings
  path=Path(__file__).resolve().parents[1]/'scripts'/'mobius_control_mcp.py'
  spec=importlib.util.spec_from_file_location('quiet_checkpoint_control',path)
  control=importlib.util.module_from_spec(spec)
  spec.loader.exec_module(control)
  calls=[]
  async def scenario():
    sink=sink_for(chat,monkeypatch,[])
    token=create_agent_token(chat.id,'test',0,run_id='sink-write-test')
    def api(method,path,body=None):
      calls.append(body)
      response=client.request(method,path,json=body,
        headers={'Authorization':f'Bearer {token}'})
      response.raise_for_status()
    monkeypatch.setattr(control,'_agent_api_call',api)
    async def dispatch(write):
      result=await asyncio.to_thread(control._call_tool,
        {'name':write['tool'],'arguments':write['arguments']})
      assert result['isError'] is False
      return WriteOutcome('succeeded')
    sink._write_delivery.dispatch=dispatch
    args={'title':'Synthetic quiet note','digest':'Initial digest.',
          'summary':'A detailed decision that must appear once.'}
    event={'type':'text_final','text_item_id':'save','content':frame(NONCE,
      WriteIntent('all-fields','checkpoint_chat',args))}
    sink.publish(event);sink.publish(event)  # Repeated authoritative delivery.
    sink.publish({'type':'text_final','text_item_id':'digest','content':frame(NONCE,
      WriteIntent('digest-only','checkpoint_chat',{'digest':'Current digest.'}))})
    await sink.finish_write_delivery()
    db.expire_all()
    assert db.get(models.Chat,chat.id).title==args['title']
    note=note_path(get_settings().data_dir,chat.id).read_text()
    assert extract_section(note,'Digest')=='Current digest.'
    assert extract_cumulative_summary(note).count(args['summary'])==1
    # The ordinary checkpoint route still binds the cumulative handoff, not
    # the short digest, to its exact source history for recovery.
    summary,_tail=recovery_source(note,list(db.get(models.Chat,chat.id).messages or []))
    assert args['summary'] in summary and 'Current digest.' not in summary
    assert calls==[args,{'digest':'Current digest.'}]
  asyncio.run(scenario())


def sink_for(chat,monkeypatch,effects):
  token='sink-write-test'
  get_writer().submit(StartTurn(chat_id=chat.id,run_token=token,
    user_msg={'role':'user','content':'Test','ts':10},title_source='Test')).result(timeout=5)
  def dispatcher(**kwargs):
    assert kwargs['env']=={'CHAT_ID':chat.id,'AGENT_TOKEN':'synthetic'}
    async def dispatch(write):effects.append(write);return WriteOutcome('succeeded')
    return dispatch
  monkeypatch.setattr(agent_write_tools,'QuietToolDispatcher',dispatcher)
  sink=ChatEventSink(ChatBroadcast(chat.id),chat.id,run_token=token)
  sink.attach_write_delivery(nonce=NONCE,env={'CHAT_ID':chat.id,'AGENT_TOKEN':'synthetic'},
    eligible_tools=frozenset({'checkpoint_chat'}))
  return sink


def claude_messages(text,parent=None):
  yield StreamEvent(uuid='s',session_id='fixture',parent_tool_use_id=parent,
    event={'type':'message_start','message':{'id':'message-one'}})
  yield StreamEvent(uuid='b',session_id='fixture',parent_tool_use_id=parent,
    event={'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
  for i in range(0,len(text),7):
    yield StreamEvent(uuid=f'd{i}',session_id='fixture',parent_tool_use_id=parent,
      event={'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text[i:i+7]}})
  yield AssistantMessage(content=[TextBlock(text=text)],model='synthetic',
    message_id='message-one',session_id='fixture',parent_tool_use_id=parent)


@pytest.mark.parametrize('provider',['codex','claude','claude-shared'])
def test_provider_normalization_enters_one_private_sink_before_broadcast_and_persistence(
    chat,db,tmp_path,monkeypatch,provider):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    text='Before.'+frame(NONCE,WRITE)+'After.'
    if provider=='codex':
      sdk=_sdk_imports()
      item=sdk['AgentMessageThreadItem'](type='agentMessage',id='i',text=text,phase='commentary')
      for event in _tool_completed_events(item,sdk):sink.publish(event)
    elif provider=='claude':
      for msg in claude_messages(text):dispatch_sdk_message(msg,sink,None)
    else:
      from app.claude_helper_host import ClaudeHelperHost,HelperTurn
      from app.helper_hosts import HostKey
      host=ClaudeHelperHost(HostKey('parent','claude',str(tmp_path),'fixture'),
        options_factory=None,session_file=tmp_path/'unused.json')
      child=HelperTurn(chat.id,'spawn',{},sink,None,False)
      host._turn_by_tool_use['exact-child']=child
      host._closed=True
      class Stream:
        async def receive_messages(self):
          for msg in claude_messages(text,'different-child'):yield msg
          for msg in claude_messages(text,'exact-child'):yield msg
      host._client=Stream()
      await host._read()
    assert not effects  # No synchronous effect on the event loop.
    await sink.finalize()
    assert len(effects)==1 and effects[0]['arguments']==WRITE.arguments
    db.expire_all()
    saved=db.get(models.Chat,chat.id)
    for surface in (sink.bc.event_log,sink.assistant_blocks,saved.messages,saved.live_assistant):
      data=json.dumps(surface)
      assert 'MOBIUS_WRITE' not in data and 'private synthetic fact' not in data
    assert 'Before.After.' in json.dumps(sink.assistant_blocks)
  asyncio.run(scenario())


def test_stop_fences_late_provider_events_synchronously_before_any_await(chat,monkeypatch):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    sink.interrupt_write_delivery()
    sink.publish({'type':'text_final','text_item_id':'late','content':frame(NONCE,WRITE)})
    await sink.finish_write_delivery()
    result=await saved_outcomes(sink)
    assert not effects and result['writes']==[]
    assert 'MOBIUS_WRITE' not in json.dumps(sink.bc.event_log)
  asyncio.run(scenario())


def test_admission_failure_is_visible_without_mislabeling_provider_execution(chat,monkeypatch):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    bad=WriteIntent('not-quiet','request_restart',{})
    sink.publish({'type':'text_final','text_item_id':'bad','content':frame(NONCE,bad)})
    await sink.finish_write_delivery()
    result=await saved_outcomes(sink)
    assert result['diagnostics'] and not effects
    assert sink._last_error is None
    failures=[block for block in sink.assistant_blocks if block.get('output_exit_code')==1]
    assert failures and all(block['status']=='done' for block in failures)
    starts=[event for event in sink.bc.event_log if event['type']=='tool_start']
    assert starts and all(isinstance(event['input'],str) for event in starts)
    assert all(json.loads(event['input'])=={} for event in starts)
  asyncio.run(scenario())


def test_stop_can_reach_a_write_after_provider_end_while_steering_is_closed(chat,db,monkeypatch):
  from app import chat as chat_mod
  from app.chat_event_sink import register_active_sink,get_active_sink,get_owned_sink
  from app.runner_registry import registry
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    entered=asyncio.Event();cleaned=asyncio.Event()
    async def dispatch(write):
      entered.set()
      try:await asyncio.Future()
      finally:cleaned.set()
    sink._write_delivery.dispatch=dispatch
    register_active_sink(chat.id,sink)
    sink.publish({'type':'text_final','text_item_id':'i','content':'Working.'+frame(NONCE,WRITE)})
    await entered.wait()
    gen=chat_mod.current_run_generation(chat.id)
    ending=asyncio.create_task(chat_mod._complete_turn(bc=sink.bc,sink=sink,db=db,
      chat_id=chat.id,run_gen=gen,provider_id='codex',cost_usd=0,close_browser=False))
    await asyncio.sleep(0)
    assert get_active_sink(chat.id) is None  # Cannot steer after provider end.
    assert get_owned_sink(chat.id) is sink  # Stop still owns the draining write.
    await asyncio.wait_for(chat_mod.stop_chat_for(chat.id,db=db),5)
    await asyncio.wait_for(ending,5)
    assert cleaned.is_set() and get_owned_sink(chat.id) is None
    db.expire_all()
    assert db.get(models.AgentWriteIntent,('sink-write-test','capture')).status=='unknown'
  try:asyncio.run(scenario())
  finally:registry.forget(chat.id)


def test_saved_owner_card_closes_admissions_but_joins_previously_accepted_writes(chat,db,monkeypatch):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    entered=asyncio.Event();release=asyncio.Event()
    async def dispatch(write):
      entered.set();await release.wait();effects.append(write)
      return WriteOutcome('succeeded')
    sink._write_delivery.dispatch=dispatch
    sink.publish({'type':'text_final','text_item_id':'before','content':frame(NONCE,WRITE)})
    await asyncio.wait_for(entered.wait(),5)
    await sink.publish_question({'type':'question','question_id':'owner-card',
      'response_mode':'continuation','questions':[{'id':'q','question':'Continue?'}]})
    late=WriteIntent('late','checkpoint_chat',{'summary':'Must not execute'})
    sink.publish({'type':'text_final','text_item_id':'after','content':frame(NONCE,late)})
    release.set();await sink.finish_write_delivery()
    assert [effect['id'] for effect in effects]==['capture']
    db.expire_all()
    assert db.get(models.Chat,chat.id).pending_question_id=='owner-card'
    assert db.get(models.AgentWriteIntent,('sink-write-test','late')) is None
    assert 'Must not execute' not in json.dumps(sink.bc.event_log)
  asyncio.run(scenario())


@pytest.mark.parametrize('private_suffix', [False, True])
def test_saved_card_keeps_completed_screenshot_before_card_without_admitting_late_writes(
    chat,db,monkeypatch,private_suffix):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    item='screenshot-before-card'
    full='![Mobile](/api/chats/fixture/media/mobile-preview.png)'
    sink.publish({'type':'text','text_item_id':item,'content':full[:-15]})
    await sink.publish_question({'type':'question','question_id':'owner-card',
      'response_mode':'continuation','questions':[{'id':'q','question':'Continue?'}]})
    late=WriteIntent('late','checkpoint_chat',{'summary':'Must not execute'})
    completion=full+frame(NONCE,late) if private_suffix else full
    sdk=_sdk_imports()
    message=sdk['AgentMessageThreadItem'](type='agentMessage',id=item,
      text=completion,phase='commentary')
    for event in _tool_completed_events(message,sdk):sink.publish(event)
    for event in _tool_completed_events(message,sdk):sink.publish(event)
    # Neither a fresh item nor another delta may narrate beyond the saved card.
    sink.publish({'type':'text_final','text_item_id':'new-after-card','content':'Do not show.'})
    sink.publish({'type':'text','text_item_id':item,'content':'Do not append.'})
    await sink.finalize()
    db.expire_all()
    saved=db.get(models.Chat,chat.id)
    reply=saved.messages[-1]
    assert reply['content']==full
    assert [block['type'] for block in reply['blocks'] if block['type']!='tool']==['text','question']
    assert saved.pending_question_id=='owner-card'
    assert not effects and db.get(models.AgentWriteIntent,('sink-write-test','late')) is None
    surfaces=json.dumps([reply,sink.bc.event_log])
    assert 'MOBIUS_WRITE' not in surfaces and 'Must not execute' not in surfaces
    assert 'Do not show.' not in surfaces and 'Do not append.' not in surfaces
  asyncio.run(scenario())


def test_stop_does_not_complete_a_reply_that_started_before_the_saved_card(chat,db,monkeypatch):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    sink.publish({'type':'text','text_item_id':'before','content':'Partial reply'})
    await sink.publish_question({'type':'question','question_id':'owner-card',
      'response_mode':'continuation','questions':[{'id':'q','question':'Continue?'}]})
    sink.interrupt_write_delivery()
    sink.publish({'type':'text_final','text_item_id':'before','content':'Partial reply completed.'})
    await sink.finalize()
    db.expire_all()
    assert db.get(models.Chat,chat.id).messages[-1]['content']=='Partial reply'
    assert not effects
  asyncio.run(scenario())


def test_claude_missing_identity_preserves_prose_but_never_authorizes_a_write(chat,monkeypatch):
  async def scenario():
    effects=[];sink=sink_for(chat,monkeypatch,effects)
    # This legacy/partial SDK shape is intentionally handled positionally by
    # the normalizer. Do not invent an identity just to execute a write.
    dispatch_sdk_message(AssistantMessage(content=[TextBlock(text='Visible answer.')],
      model='synthetic'),sink,None)
    assert 'Visible answer.' in json.dumps(sink.assistant_blocks)
    for chunk in frame(NONCE,WRITE):
      sink.publish({'type':'text','content':chunk})
    dispatch_sdk_message(AssistantMessage(content=[TextBlock(text=frame(NONCE,WRITE))],
      model='synthetic'),sink,None)
    await sink.finish_write_delivery()
    state=await saved_outcomes(sink)
    assert not effects and state['diagnostics']
    assert 'private synthetic fact' not in json.dumps(sink.bc.event_log)
  asyncio.run(scenario())
