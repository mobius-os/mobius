"""Content-only saves preserve independent fields and recovery invariants."""
import importlib.util
from pathlib import Path
import pytest
from app import models
from app.chat_writer import AdmitProviderExecution, RecordDeliveredInput, get_writer
from app.chat_continuity import delivered_input_prefix, render_projection
from app.compaction import build_continuity_source
from tests.test_chat_continuity import _start


def save(client, agent, identity, **fields):
  r=client.post('/api/chat/continuity/checkpoints',headers=agent,json={'checkpoint_id':identity,**fields})
  assert r.status_code==200,r.text
  return r.json()


def test_independent_fields_and_no_synthetic_digest(client,auth,chat,db):
  agent=_start(chat)
  assert save(client,agent,'noop')['status']=='unchanged'
  db.expire_all();assert db.get(models.ChatContinuity,chat.id) is None
  save(client,agent,'name',title='Rehearsal planning')
  save(client,agent,'summary',summary='Tuesday accepted. Roster unchecked.')
  state=client.get(f'/api/chats/{chat.id}/continuity?full=true',headers=auth).json()
  assert state['title']=='Rehearsal planning'
  assert state['summary']=='Tuesday accepted. Roster unchecked.'
  assert state['entries']==[]
  save(client,agent,'delta',digest='Tuesday rehearsal accepted; roster remains unchecked.')
  state=client.get(f'/api/chats/{chat.id}/continuity?full=true',headers=auth).json()
  assert len(state['entries'])==1
  assert state['summary']=='Tuesday accepted. Roster unchecked.'
  db.expire_all();projection=render_projection(db,chat.id)
  assert 'Revision 1' not in projection and 'Revision 2' not in projection
  assert 'Revision 3' in projection
  source=build_continuity_source(db,chat.id,list(db.get(models.Chat,chat.id).messages))
  assert 'REVISION 1:' not in source
  assert 'REVISION 3:' in source


def test_name_only_never_acknowledges_content(client,chat,db):
  agent=_start(chat);db.expire_all()
  rows=list(db.get(models.Chat,chat.id).messages)
  count,proof=delivered_input_prefix(rows,'continuity-run','continue')
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id,run_token='continuity-run')).result(5)
  get_writer().submit(RecordDeliveredInput(chat_id=chat.id,run_token='continuity-run',message_count=count,prefix_hash=proof)).result(5)
  assert save(client,agent,'name',title='Current scope')['coverage']['message_count']==0
  assert save(client,agent,'summary',summary='Continue the current scope.')['coverage']['message_count']==count


def test_compact_context_is_current_and_bounded_to_state(client,chat,db):
  agent=_start(chat)
  empty=client.get('/api/chat/continuity/context',headers=agent)
  assert empty.status_code==200 and 'no_saved_summary' in empty.json()['context']
  save(client,agent,'first',summary='Latest accepted decision',digest='JOURNAL_ONLY_DETAIL')
  r=client.get('/api/chat/continuity/context',headers=agent)
  assert 'Latest accepted decision' in r.json()['context']
  assert 'JOURNAL_ONLY_DETAIL' not in r.json()['context']
  assert 'full=true' in r.json()['context']


def test_hook_reports_unavailable_not_empty(monkeypatch,capsys):
  import sys
  script=Path(__file__).parents[1]/'scripts/chat_continuity_hook.py'
  monkeypatch.syspath_prepend(str(script.parent))
  spec=importlib.util.spec_from_file_location('continuity_hook_test',script)
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  def fail(*a, **kw):raise OSError('private diagnostic')
  monkeypatch.setattr(module,'_agent_api_call',fail)
  result=module.hook_output()
  assert 'UNAVAILABLE, not empty' in result['hookSpecificOutput']['additionalContext']
  assert 'private diagnostic' not in str(result)
  assert 'could not load' in capsys.readouterr().err


def test_custom_model_connections_use_same_runner():
  from app.providers import MobiusProvider,AppModelProvider
  assert MobiusProvider.runtime_kind==AppModelProvider.runtime_kind=='codex_sdk'


def test_native_hook_contract_and_exact_trust():
  import hashlib,json,tomllib
  from app.platform_tools import continuity_start_hooks,codex_continuity_overrides
  hooks=continuity_start_hooks()
  assert hooks[0]['matcher']=='startup|resume|compact|clear|fork'
  config={}
  for override in codex_continuity_overrides():
    parsed=tomllib.loads(override)
    config.update(parsed.get('hooks',{}))
  assert config['SessionStart']==hooks
  identity={'event_name':'session_start',**hooks[0]}
  identity['hooks']=[{**hooks[0]['hooks'][0],'async':False}]
  expected='sha256:'+hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
  assert list(config['state'].values())==[{'trusted_hash':expected}]
  assert len(config['state'])==1
  assert 'AGENT_TOKEN' not in str(config)
