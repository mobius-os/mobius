from app.chat_writer import create_chat
import asyncio
import re

import pytest
from app import agent_write_context,agent_write_tools,chat as chat_mod,models,schemas
from app.agent_write_channel import WriteIntent,frame
from app.agent_write_delivery import WriteOutcome
from app.broadcast import create_broadcast,remove_broadcast
from app.chat_writer import StartTurn,get_writer


def test_every_turn_gets_explicit_shared_delivery_without_a_provider_opt_in(db,chat):
  context=agent_write_context.prepare_write_context(db,chat_id=chat.id,run_id='r',
    top_level=True,coordination_enabled=True)
  assert context.eligible_tools==frozenset({'checkpoint_chat'})
  assert 'checkpoint_chat' in context.prompt and '"summary"' in context.prompt
  assert context.failure_receipts==()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider',['codex','claude','mobius'])
@pytest.mark.parametrize('helper',[False,True])
async def test_each_runtime_gets_shared_midtask_delivery_without_native_final_schema(
    chat,db,monkeypatch,provider,helper):
  effects=[];calls=[]
  class Provider:
    name=provider
    runtime_kind='claude_sdk' if provider=='claude' else 'codex_sdk'
    def check_auth(self,*_):return None
    async def ensure_auth(self,*_):pass
    def build_env(self,**kwargs):return kwargs['base_env']
  monkeypatch.setattr(chat_mod,'get_provider',lambda _:Provider())
  monkeypatch.setattr('app.claude_sdk_runner._resumable',lambda *a,**k:True)
  async def close(*args):pass
  monkeypatch.setattr(chat_mod,'_close_turn_browser',close)
  def dispatcher(**kwargs):
    assert kwargs['env']['CHAT_ID']==chat.id
    from app.auth import decode_access_token
    claims=decode_access_token(kwargs['env']['AGENT_TOKEN'])
    assert claims['agent_run']=='context-test' and claims['agent_chat']==chat.id
    if helper:
      assert claims['delegation_id']=='context-helper'
    async def dispatch(write):effects.append(write);return WriteOutcome('succeeded')
    return dispatch
  monkeypatch.setattr(agent_write_tools,'QuietToolDispatcher',dispatcher)
  async def runner(**kwargs):
    calls.append(kwargs)
    assert not kwargs.get('quiet_tools_enabled')
    nonce=re.search(r'<MOBIUS_WRITE ([^>]+)>',kwargs['user_message']).group(1)
    sink=kwargs['bc']
    sink.publish({'type':'text_final','text_item_id':'commentary',
      'content':'Working.'+frame(nonce,WriteIntent('capture','checkpoint_chat',{'summary':'synthetic'}))})
    async with asyncio.timeout(5):
      while not effects:await asyncio.sleep(0.001)
    sink.publish({'type':'text_final','text_item_id':'answer','content':'Finished.'})
    return {'session_id':'synthetic-session','cost_usd':0,'error':None}
  monkeypatch.setattr('app.codex_sdk_runner.run_codex_sdk_turn',runner)
  monkeypatch.setattr('app.claude_sdk_runner.run_claude_sdk_turn',runner)
  monkeypatch.setattr('app.claude_helper_host.run_claude_host_turn',runner)
  if helper:
    import hashlib
    db.add(create_chat(id='context-parent',title='Parent',messages=[],provider=provider))
    db.add(models.Delegation(id='context-helper',parent_chat_id='context-parent',
      parent_root_run_id='context-parent-root',task_key='quiet-context',child_chat_id=chat.id,
      provider=provider,model='synthetic-model',scope='write',cwd='/data',
      prompt_sha256=hashlib.sha256(b'Work').hexdigest()))
  chat.provider=provider;chat.agent_settings_json={'model':'synthetic-model'};db.commit()
  token='context-test'
  get_writer().submit(StartTurn(chat_id=chat.id,run_token=token,
    user_msg={'role':'user','content':'Work','ts':10},title_source='Work',default_provider=provider)).result(timeout=5)
  bc=create_broadcast(chat.id)
  try:
    await chat_mod._run_chat_impl(messages=[schemas.ChatMessage(role='user',content='Work')],
      chat_id=chat.id,session_id='synthetic-session',provider_id=provider,run_token=token)
    assert len(calls)==1 and len(effects)==1
    assert bool(calls[0].get('run_policy'))==helper
    if helper:assert calls[0]['helper_host_key'] is not None
    assert all('MOBIUS_WRITE' not in str(event) for event in bc.event_log)
    db.expire_all()
    assert db.get(models.AgentWriteIntent,(token,'capture')).status=='succeeded'
    # The per-run nonce does not mutate a chat's immutable constitution.
    assert 'MOBIUS_WRITE' not in calls[0].get('system_prompt',calls[0].get('skill_text',''))
  finally:remove_broadcast(chat.id)
