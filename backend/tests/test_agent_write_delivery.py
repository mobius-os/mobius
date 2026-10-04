"""Real writer admission with fake effects; no provider or live tool calls."""
import asyncio
import pytest
from app.agent_write_channel import WriteIntent, frame
from app.agent_write_delivery import AgentWriteDelivery, WriteOutcome
from app.chat_writer import StartTurn, get_writer
from app.chat_writer import ClaimAgentWrite, SealAgentWrites, RecordAgentWriteFailure, ReadAgentWriteOutcomes

NONCE="delivery_test_nonce_123456"
WRITE=WriteIntent("one","checkpoint_chat",{"summary":"Synthetic note"})


def setup(chat, dispatch, errors):
  token="delivery-test"
  get_writer().submit(StartTurn(chat_id=chat.id,run_token=token,
    user_msg={"role":"user","content":"Test","ts":10},title_source="Test")).result(timeout=5)
  return AgentWriteDelivery(chat_id=chat.id,run_token=token,nonce=NONCE,
    eligible_tools=frozenset({"checkpoint_chat"}),dispatch=dispatch,on_failure=errors.append)


async def outcomes(delivery):
  return await delivery._command(ReadAgentWriteOutcomes(**delivery.owner))


def final(item="message1",write=WRITE):
  return {"type":"text_final","text_item_id":item,"content":"Visible."+frame(NONCE,write)}


def test_slow_effect_does_not_pause_public_stream_and_success_has_no_feedback(chat):
  async def scenario():
    entered, release=asyncio.Event(),asyncio.Event();errors=[];effects=[]
    async def dispatch(write):
      entered.set();await release.wait();effects.append(write["id"])
      return WriteOutcome("succeeded")
    delivery=setup(chat,dispatch,errors)
    assert delivery.filter(final())["content"]=="Visible."
    await asyncio.wait_for(entered.wait(),5)
    assert delivery.filter({"type":"text","text_item_id":"next","content":"Still working."})["content"]=="Still working."
    release.set()
    await delivery.finish()
    result=await outcomes(delivery)
    assert effects==["one"] and not errors
    assert result["writes"][0]["status"]=="succeeded"
    assert delivery.filter(final()) is None
  asyncio.run(scenario())


def test_final_replay_before_and_after_ack_never_repeats_the_effect(chat):
  async def scenario():
    errors=[];effects=[]
    async def dispatch(write):effects.append(write["id"]);return WriteOutcome("succeeded")
    delivery=setup(chat,dispatch,errors)
    delivery.filter(final());delivery.filter(final())
    await asyncio.gather(*delivery.admissions)
    delivery.filter(final())
    await delivery.finish()
    assert effects==["one"] and not errors
  asyncio.run(scenario())


def test_provisional_commands_and_ordinary_tool_output_never_execute(chat):
  async def scenario():
    effects=[];errors=[]
    async def dispatch(write):effects.append(write);return WriteOutcome("succeeded")
    delivery=setup(chat,dispatch,errors)
    text={"type":"text","text_item_id":"m","content":frame(NONCE,WRITE)}
    assert delivery.filter(text) is None
    result={"type":"tool_output","content":frame(NONCE,WRITE)}
    assert delivery.filter(result) is result
    await delivery.finish(interrupted=True)
    state=await outcomes(delivery)
    assert not effects and not state["writes"]
    assert errors and state["diagnostics"]
  asyncio.run(scenario())


def test_ineligible_frames_are_private_and_rejection_is_durable(chat):
  async def scenario():
    errors=[];effects=[]
    async def dispatch(write):effects.append(write);return WriteOutcome("succeeded")
    delivery=setup(chat,dispatch,errors)
    assert delivery.filter(final(write=WriteIntent("x","screenshot",{}))) is None
    await delivery.finish()
    state=await outcomes(delivery)
    assert not effects and errors
    assert state["diagnostics"][0]["reason"]=="invalid_or_ineligible_write_frame"
  asyncio.run(scenario())


def test_external_exception_is_unknown_not_retryable_success(chat):
  async def scenario():
    errors=[];calls=[]
    async def dispatch(write):calls.append(write);raise OSError("secret must not be copied")
    delivery=setup(chat,dispatch,errors)
    delivery.filter(final())
    await delivery.finish()
    state=await outcomes(delivery)
    assert len(calls)==1 and state["writes"][0]["status"]=="unknown"
    assert errors==[{"id":"one","stage":"completion","reason":"dispatch_outcome_unknown","outcome":"unknown"}]
    assert "secret" not in str(state)
  asyncio.run(scenario())


def test_interruption_before_admission_ack_never_starts_queued_effect(chat):
  async def scenario():
    errors=[];effects=[]
    async def dispatch(write):effects.append(write);return WriteOutcome("succeeded")
    delivery=setup(chat,dispatch,errors)
    delivery.filter(final())
    await delivery.finish(interrupted=True)
    state=await outcomes(delivery)
    assert not effects and state["writes"][0]["status"]=="cancelled"
  asyncio.run(scenario())


def test_cancelled_teardown_owns_worker_until_ambiguous_effect_is_recorded(chat):
  async def scenario():
    entered=asyncio.Event();cleaned=asyncio.Event();errors=[]
    async def dispatch(write):
      entered.set()
      try:await asyncio.Future()
      finally:cleaned.set()
    delivery=setup(chat,dispatch,errors)
    delivery.filter(final());await asyncio.wait_for(entered.wait(),5)
    ending=asyncio.create_task(delivery.finish());await asyncio.sleep(0)
    ending.cancel()
    with pytest.raises(asyncio.CancelledError):await ending
    assert cleaned.is_set() and delivery.finish_task.done()
    await delivery.finish()
    state=await outcomes(delivery)
    assert state["writes"][0]["status"]=="unknown"
  asyncio.run(scenario())


def test_stop_while_claim_ack_is_outstanding_cannot_start_an_effect(chat):
  async def scenario():
    claim_started=asyncio.Event();release_claim=asyncio.Event();effects=[]
    async def dispatch(write):effects.append(write);return WriteOutcome("succeeded")
    delivery=setup(chat,dispatch,[])
    command=delivery._command
    async def controlled(cmd):
      result=await command(cmd)
      if isinstance(cmd,ClaimAgentWrite) and result["status"]=="claimed":
        claim_started.set();await release_claim.wait()
      return result
    delivery._command=controlled
    delivery.filter(final());await claim_started.wait()
    ending=asyncio.create_task(delivery.finish(interrupted=True))
    await asyncio.sleep(0);release_claim.set()
    await ending
    state=await outcomes(delivery)
    assert not effects and state["writes"][0]["status"]=="unknown"
  asyncio.run(scenario())


@pytest.mark.parametrize('failed_command',[SealAgentWrites,RecordAgentWriteFailure])
def test_persistence_failure_still_cancels_and_joins_owned_worker(chat,failed_command):
  async def scenario():
    entered=asyncio.Event();cleaned=asyncio.Event()
    async def dispatch(write):
      entered.set()
      try:await asyncio.Future()
      finally:cleaned.set()
    delivery=setup(chat,dispatch,[])
    command=delivery._command
    async def controlled(cmd):
      if isinstance(cmd,failed_command):raise OSError("synthetic persistence failure")
      return await command(cmd)
    delivery._command=controlled
    delivery.filter(final());await entered.wait()
    if failed_command is RecordAgentWriteFailure:
      delivery.filter({"type":"text","text_item_id":"unfinished","content":frame(NONCE,WRITE)})
    with pytest.raises(OSError):await delivery.finish()
    assert cleaned.is_set() and delivery.worker.done()
  asyncio.run(scenario())


def test_stop_escalates_an_already_draining_finish_without_waiting_for_tool(chat):
  async def scenario():
    entered=asyncio.Event();sealed=asyncio.Event();cleaned=asyncio.Event()
    async def dispatch(write):
      entered.set()
      try:await asyncio.Future()
      finally:cleaned.set()
    delivery=setup(chat,dispatch,[]);command=delivery._command
    async def controlled(cmd):
      result=await command(cmd)
      if isinstance(cmd,SealAgentWrites):sealed.set()
      return result
    delivery._command=controlled
    delivery.filter(final());await entered.wait()
    normal=asyncio.create_task(delivery.finish());await sealed.wait()
    await asyncio.wait_for(delivery.finish(interrupted=True),5)
    state=await outcomes(delivery)
    assert (await normal) is None and cleaned.is_set()
    assert state["writes"][0]["status"]=="unknown"
  asyncio.run(scenario())


def test_channel_capacity_cannot_erase_ordinary_answers_or_leak_rejected_writes(chat):
  async def scenario():
    effects=[]
    async def dispatch(write):effects.append(write);return WriteOutcome('succeeded')
    delivery=setup(chat,dispatch,[])
    for index in range(256):
      delivery.filter({'type':'text_final','text_item_id':str(index),'content':'Ordinary.'})
    answer=delivery.filter({'type':'text_final','text_item_id':'overflow',
      'content':'Still visible.'+frame(NONCE,WRITE)})
    assert answer['content']=='Still visible.'
    await delivery.finish()
    state=await outcomes(delivery)
    assert not effects and state['diagnostics']
    assert sum(state.admitted_fingerprint is not None for state in delivery.channel.items.values())==256
  asyncio.run(scenario())


def test_plain_capacity_overflow_cannot_create_an_avoidable_repair_turn(chat):
  async def scenario():
    async def dispatch(write):raise AssertionError('No write')
    delivery=setup(chat,dispatch,[])
    for index in range(300):
      result=delivery.filter({'type':'text_final','text_item_id':str(index),'content':'Ordinary.'})
      assert result['content']=='Ordinary.'
    await delivery.finish()
    state=await outcomes(delivery)
    assert state['diagnostics']==[] and state['writes']==[]
  asyncio.run(scenario())


@pytest.mark.parametrize('direction',['attributed-first','anonymous-first'])
def test_uncertain_attribution_cannot_leak_any_split_private_frame(chat,direction):
  async def scenario():
    async def dispatch(write):return WriteOutcome('succeeded')
    # Private frame starts at the item boundary, including every marker split.
    text=frame(NONCE,WRITE).lstrip('\n')
    for split in range(1,len(text)):
      delivery=AgentWriteDelivery(chat_id=chat.id,run_token='probe',nonce=NONCE,
        eligible_tools=frozenset({'checkpoint_chat'}),dispatch=dispatch,on_failure=lambda _:None)
      delivery._failure=lambda *a,**k:None  # Pure presentation probe, no persistence.
      first='item' if direction=='attributed-first' else None
      second=None if first else 'item'
      outputs=[delivery.filter({'type':'text','text_item_id':first,'content':text[:split]}),
               delivery.filter({'type':'text','text_item_id':second,'content':text[split:]})]
      assert not any(out and out.get('content') for out in outputs)
      final=delivery.filter({'type':'text_final','content':'Visible.'+frame(NONCE,WRITE)})
      assert final['content']=='Visible.'
      assert not delivery.admissions and delivery.worker is None
  asyncio.run(scenario())


def test_ambiguous_oversized_deltas_have_no_retained_parser_buffer(chat):
  async def scenario():
    async def dispatch(write):raise AssertionError('No write')
    delivery=setup(chat,dispatch,[])
    for chunk in ('<MOBIUS_WRITE '+NONCE+'>\n','x'*70000,'x'*70000):
      assert delivery.filter({'type':'text','content':chunk}) is None
    assert not delivery.channel.items
    assert delivery.filter({'type':'text_final','content':'Recovered plain text.'})['content']=='Recovered plain text.'
    await delivery.finish()
    state=await outcomes(delivery)
    assert not state['diagnostics'] and not state['writes']
  asyncio.run(scenario())


def test_result_projection_strips_private_frames_without_authorizing_writes(chat):
  async def scenario():
    effects, errors = [], []
    async def dispatch(write):
      effects.append(write)
      return WriteOutcome('succeeded')
    delivery = setup(chat, dispatch, errors)
    event = {'type': 'assistant_result', 'content': 'Report.' + frame(NONCE, WRITE)}
    assert delivery.filter(event) == {'type': 'assistant_result', 'content': 'Report.'}
    assert delivery.filter({'type': 'assistant_result',
      'content': f'Hidden\n<MOBIUS_WRITE {NONCE}>\nunfinished'}) is None
    await delivery.finish()
    state = await outcomes(delivery)
    assert not effects and not errors and not state['writes']
    assert delivery.filter(event) is None
  asyncio.run(scenario())
