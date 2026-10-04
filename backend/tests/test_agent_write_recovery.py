"""Failure-only continuation uses existing actor authority and owner barriers."""
from sqlalchemy.orm import object_session
from app import transcript_rows
from app.chat_writer import create_chat
import pytest
from app import models
from app.agent_write_channel import WriteIntent
from app.agent_write_journal import pending_failure_reports
from app.chat_writer import (get_writer,StartTurn,AdmitProviderExecution,
  AdmitAgentWrites,ClaimAgentWrite,SettleAgentWrite,SealAgentWrites,PromotePending,
  AcknowledgeAgentWriteFailures,FinishRun)
from app.chat_writer import RecordAgentWriteFailure


def submit(cmd):return get_writer().submit(cmd).result(timeout=5)


def attempt(chat,token='write-run',*,app_id=None):
  owner={'chat_id':chat.id,'run_token':token}
  submit(StartTurn(**owner,user_msg={'role':'user','content':'Test','ts':10},title_source='Test',
    initiated_by_app_id=app_id,default_provider=chat.provider or 'claude'))
  submit(AdmitProviderExecution(**owner))
  return owner


def outcome(owner,status='failed',*,seal=True):
  submit(AdmitAgentWrites(**owner,item_id='item',fingerprint='a'*64,
    writes=(WriteIntent(owner['run_token'],'checkpoint_chat',{'summary':'private contents'}),)))
  submit(ClaimAgentWrite(**owner))
  submit(SettleAgentWrite(**owner,operation_id=owner['run_token'],status=status,reason='synthetic detail'))
  if seal:submit(SealAgentWrites(**owner))


def promote(owner,**kwargs):
  return submit(PromotePending(chat_id=owner['chat_id'],run_token='provisional',
    ending_run_token=owner['run_token'],**kwargs))


@pytest.mark.parametrize('status',['failed','unknown'])
def test_only_failed_writes_earn_one_exact_same_root_recovery(chat,db,status):
  owner=attempt(chat);outcome(owner,status)
  result=promote(owner)
  source=result['promoted'];assert source['continuation_reason']=='quiet_write_failure'
  db.expire_all();run=db.get(models.ChatRun,source['_run_token'])
  assert run.root_run_id==owner['run_token'] and run.chat_id==chat.id
  assert run.continuation_json['supersedes_run_token']==owner['run_token']
  saved=db.get(models.Chat,chat.id)
  assert not saved.pending_messages
  assert not any(row.get('kind')=='continuation' for row in list(transcript_rows.history(saved)))
  recovery={'chat_id':chat.id,'run_token':run.id}
  submit(AdmitProviderExecution(**recovery));outcome(recovery)
  assert promote(recovery)['promoted'] is None  # No failure feedback loop.


def test_success_never_creates_provider_input_or_an_automatic_turn(chat,db):
  owner=attempt(chat);outcome(owner,'succeeded')
  assert promote(owner)['promoted'] is None
  assert pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next')==[]


@pytest.mark.parametrize('terminal',['stopped','failed','interrupted'])
def test_stop_and_provider_failure_keep_reports_without_authorizing_execution(chat,db,terminal):
  owner=attempt(chat);outcome(owner)
  assert promote(owner,ending_status=terminal)['promoted'] is None
  reports=pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next')
  assert len(reports)==1 and 'private contents' not in str(reports)


@pytest.mark.parametrize('state',['open','stopped','completed','cancelled','cannot_complete','dismissed','missing'])
def test_write_repair_keeps_goal_authority_instead_of_detaching_from_a_hold(chat,db,state):
  owner=attempt(chat);outcome(owner)
  db.expire_all()
  run=db.get(models.ChatRun,owner['run_token'])
  run.goal_id='write-goal'
  if state!='missing':
    db.add(models.ChatGoal(id='write-goal',chat_id=chat.id,objective='Synthetic work',
      status='open' if state=='dismissed' else state))
  if state=='dismissed':db.get(models.Chat,chat.id).dismissed_goal_id='write-goal'
  db.commit()
  result=promote(owner)
  db.expire_all()
  if state=='open':
    assert result['promoted']['continuation_reason']=='quiet_write_failure'
    assert db.get(models.ChatRun,result['promoted']['_run_token']).goal_id=='write-goal'
  else:
    assert result['promoted'] is None
    assert db.query(models.ChatRun).filter(models.ChatRun.id!=owner['run_token']).count()==0
    assert len(pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next'))==1


def test_recovery_cannot_overtake_pending_work_or_saved_owner_card(chat,db):
  owner=attempt(chat);outcome(owner)
  db.expire_all();row=db.get(models.Chat,chat.id)
  row.pending_question_id='exact-card';db.commit()
  assert promote(owner).reason=='question'
  row.pending_question_id=None
  row.pending_messages=[{'role':'user','content':'Changed request','cid':'new','ts':20}];db.commit()
  assert promote(owner)['promoted']['content']=='Changed request'


def test_pending_execution_prevents_premature_repair(chat):
  owner=attempt(chat);outcome(owner,seal=False)
  assert promote(owner)['promoted'] is None


def test_report_acknowledgment_is_exact_chat_owned_and_never_marks_before_use(chat,db):
  owner=attempt(chat);outcome(owner)
  submit(FinishRun(**owner,terminal_status='completed'))
  new=attempt(chat,'next')
  assert len(pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next'))==1
  report=pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next')[0]
  assert submit(AcknowledgeAgentWriteFailures(**new,reports=((owner['run_token'],report['fingerprint']),)))=={'status':'acknowledged'}
  db.expire_all()
  assert pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next')==[]
  assert db.get(models.AgentWriteStream,owner['run_token']).failure_delivered_by=='next'


def test_acknowledgment_cannot_consume_late_changes_or_hide_future_negative_information(chat,db):
  owner=attempt(chat);outcome(owner,'unknown')
  submit(FinishRun(**owner,terminal_status='completed'))
  new=attempt(chat,'consumer')
  selected=pending_failure_reports(db,chat_id=chat.id,exclude_run_id='consumer')[0]
  submit(RecordAgentWriteFailure(**owner,stage='late',reason='new detail'))
  submit(AcknowledgeAgentWriteFailures(**new,reports=((owner['run_token'],selected['fingerprint']),)))
  db.expire_all()
  current=pending_failure_reports(db,chat_id=chat.id,exclude_run_id='consumer')[0]
  assert current['fingerprint']!=selected['fingerprint']
  submit(AcknowledgeAgentWriteFailures(**new,reports=((owner['run_token'],current['fingerprint']),)))
  db.expire_all();assert pending_failure_reports(db,chat_id=chat.id,exclude_run_id='consumer')==[]
  submit(SettleAgentWrite(**owner,operation_id=owner['run_token'],status='failed',reason='observed late failure'))
  db.expire_all();assert pending_failure_reports(db,chat_id=chat.id,exclude_run_id='consumer')


def test_diagnostic_overflow_invalidates_both_stale_and_prior_acknowledgments(chat, db):
  from app.agent_write_journal import MAX_DIAGNOSTICS

  owner = attempt(chat)
  for index in range(MAX_DIAGNOSTICS):
    submit(RecordAgentWriteFailure(**owner, stage='protocol', reason=f'failure-{index}'))
  submit(FinishRun(**owner, terminal_status='completed'))
  consumer = attempt(chat, 'consumer')

  def report():
    db.expire_all()
    return pending_failure_reports(db, chat_id=chat.id, exclude_run_id='consumer')[0]

  def acknowledge(snapshot):
    submit(AcknowledgeAgentWriteFailures(
      **consumer, reports=((owner['run_token'], snapshot['fingerprint']),)))

  selected = report()
  submit(RecordAgentWriteFailure(**owner, stage='late', reason='first late detail'))
  acknowledge(selected)
  current = report()
  assert current['fingerprint'] != selected['fingerprint']
  assert len(current['diagnostics']) == MAX_DIAGNOSTICS
  assert current['diagnostics'][-1] == {
    **selected['diagnostics'][-1], 'additional_diagnostics_omitted': 1,
  }
  acknowledge(current)
  db.expire_all()
  assert pending_failure_reports(db, chat_id=chat.id, exclude_run_id='consumer') == []

  submit(RecordAgentWriteFailure(**owner, stage='late', reason='second late detail'))
  newest = report()
  assert newest['fingerprint'] != current['fingerprint']
  assert newest['diagnostics'][-1]['additional_diagnostics_omitted'] == 2
  assert len(newest['diagnostics']) == MAX_DIAGNOSTICS


def test_negative_replay_after_diagnostic_cap_still_changes_failure_report(chat, db):
  from app.agent_write_journal import MAX_DIAGNOSTICS, failure_report

  owner = attempt(chat)
  outcome(owner, seal=False)
  for index in range(MAX_DIAGNOSTICS):
    submit(RecordAgentWriteFailure(**owner, stage='protocol', reason=f'failure-{index}'))
  db.expire_all()
  run = db.get(models.ChatRun, owner['run_token'])
  before = failure_report(db, run)
  replay = submit(AdmitAgentWrites(**owner, item_id='replay', fingerprint='b' * 64,
    writes=(WriteIntent(owner['run_token'], 'checkpoint_chat', {'summary': 'private contents'}),)))
  assert replay['negative_replays'][0]['status'] == 'failed'
  db.expire_all()
  after = failure_report(db, run)
  assert after['fingerprint'] != before['fingerprint']
  assert after['diagnostics'][-1]['additional_diagnostics_omitted'] == 1
  assert submit(ClaimAgentWrite(**owner))['status'] == 'empty'


def test_repair_context_prioritizes_its_exact_cause_ahead_of_old_backlog(chat,db):
  from app import agent_write_context
  for index in range(10):
    owner=attempt(chat,f'old-{index}');outcome(owner)
    submit(FinishRun(**owner,terminal_status='completed'))
  owner=attempt(chat,'trigger');outcome(owner)
  source=promote(owner)['promoted']
  db.expire_all()
  context=agent_write_context.prepare_write_context(db,chat_id=chat.id,
    run_id=source['_run_token'],top_level=True,coordination_enabled=True)
  assert context.failure_receipts[0][0]=='trigger'
  assert len(context.failure_receipts)==8
  assert 'trigger' in context.prompt


@pytest.mark.parametrize('provider',['codex','claude'])
def test_helper_failure_repair_keeps_exact_task_policy_and_root(chat,db,provider):
  import hashlib
  from app.delegations import policy_for_chat
  parent=create_chat(id='parent',title='Parent',messages=[],provider=provider)
  db.add(parent)
  db.add(models.Delegation(id='helper',parent_chat_id=parent.id,
    parent_root_run_id='parent-root',task_key='bounded-task',child_chat_id=chat.id,
    provider=provider,model='synthetic-model',scope='write',cwd='/data/bounded',
    prompt_sha256=hashlib.sha256(b'Test').hexdigest()))
  chat.provider=provider;db.commit()
  owner=attempt(chat);outcome(owner)
  before=policy_for_chat(db,chat.id)
  source=promote(owner)['promoted']
  db.expire_all();run=db.get(models.ChatRun,source['_run_token'])
  assert policy_for_chat(db,chat.id)==before
  assert run.root_run_id==owner['run_token'] and run.provider==provider
  assert run.goal_id is None
  assert list(transcript_rows.history(db.get(models.Chat, chat.id)))[0]['content']=='Test'
  assert db.get(models.Delegation,'helper').delivered_run_id is None


def test_cancelled_helper_cannot_gain_a_failure_repair_turn(chat,db):
  from datetime import datetime,UTC
  db.add(create_chat(id='parent',title='Parent',messages=[]))
  db.add(models.Delegation(id='cancelled',parent_chat_id='parent',
    parent_root_run_id='parent-root',task_key='bounded-task',child_chat_id=chat.id,
    provider='codex',scope='write',cwd='/data/bounded',prompt_sha256='a'*64,
    cancelled_at=datetime.now(UTC)))
  db.commit()
  owner=attempt(chat);outcome(owner)
  assert promote(owner)['promoted'] is None
  assert pending_failure_reports(db,chat_id=chat.id,exclude_run_id='next')


@pytest.mark.parametrize('condition',['valid','deleted-app','wrong-attribution','wrong-child-owner'])
def test_app_owned_helper_repair_reuses_delegation_ownership_guard(chat,db,condition):
  from datetime import UTC,datetime
  app=models.App(name='Synthetic app',slug='quiet-fixture',source_dir='/tmp/quiet-fixture',jsx_source='')
  db.add(app);db.flush()
  db.add(create_chat(id='parent',title='Parent',messages=[]))
  db.add(models.Delegation(id='app-helper',app_id=app.id,parent_chat_id='parent',
    parent_root_run_id='parent-root',task_key='bounded-task',child_chat_id=chat.id,
    provider='codex',scope='write',cwd='/data/bounded',prompt_sha256='a'*64))
  chat.created_by_app_id=app.id;db.commit()
  owner=attempt(chat,app_id=None if condition=='wrong-attribution' else app.id);outcome(owner)
  db.expire_all()
  if condition=='deleted-app':db.get(models.App,app.id).deleted_at=datetime.now(UTC)
  if condition=='wrong-child-owner':db.get(models.Chat,chat.id).created_by_app_id=None
  db.commit()
  source=promote(owner)['promoted']
  assert bool(source)==(condition=='valid')
  if source:
    db.expire_all()
    assert db.get(models.ChatRun,source['_run_token']).initiated_by_app_id==app.id


@pytest.mark.parametrize('continuation',['quiet_write_failure','restart','manual','unrelated-root','different-chat','different-browser','different-browser-epoch','cycle'])
def test_helper_result_preserves_findings_only_for_exact_write_repair_lineage(chat,db,continuation):
  from datetime import UTC,datetime,timedelta
  from app.delegations import _result_with_write_repair
  now=datetime.now(UTC)
  original=models.ChatRun(id='findings',chat_id=chat.id,root_run_id='findings',
    provider='codex',status='completed',started_at=now)
  repair=models.ChatRun(id='repair',chat_id=chat.id,root_run_id='findings',
    provider='codex',status='completed',started_at=now+timedelta(seconds=1),
    continuation_json={'reason':'quiet_write_failure','supersedes_run_token':'findings',
                       'source_work_id':'findings'})
  db.add_all([original,repair]);db.flush()
  transcript_rows.replace_all(object_session(chat), chat, [{'id':'findings:assistant:1','role':'assistant','content':'Task findings: defect A and fix B.'},
    {'id':'repair','role':'assistant','content':'The checkpoint save is repaired.'}])
  target=repair
  if continuation=='restart':
    target=models.ChatRun(id='resumed-repair',chat_id=chat.id,root_run_id='findings',
      provider='codex',status='completed',started_at=now+timedelta(seconds=2),
      continuation_json={'reason':'restart','supersedes_run_token':'repair'})
    db.add(target)
  elif continuation=='manual':
    repair.continuation_json={**repair.continuation_json,'reason':'manual'}
  elif continuation=='unrelated-root':repair.root_run_id='unrelated'
  elif continuation=='different-chat':
    db.add(create_chat(id='foreign',title='Foreign',messages=[]));db.flush()
    original.chat_id='foreign'
  elif continuation=='different-browser':
    repair.browser_grant_id='another-browser';repair.browser_grant_epoch=0
  elif continuation=='different-browser-epoch':
    original.browser_grant_id=repair.browser_grant_id='same-browser'
    original.browser_grant_epoch=0;repair.browser_grant_epoch=1
  elif continuation=='cycle':
    repair.continuation_json={**repair.continuation_json,'source_work_id':'repair',
                             'supersedes_run_token':'repair'}
  db.commit()
  result=_result_with_write_repair(db,chat,target)
  assert ('Task findings: defect A and fix B.' in result)==(continuation in {'quiet_write_failure','restart'})
  assert 'The checkpoint save is repaired.' in result


def test_failed_write_details_are_retrievable_without_replaying_or_loading_all_arguments(client,chat,db,auth):
  owner=attempt(chat);outcome(owner,'unknown')
  submit(FinishRun(**owner,terminal_status='completed'))
  report=pending_failure_reports(db,chat_id=chat.id,exclude_run_id='future')[0]
  assert 'private contents' not in str(report)
  path=report['writes'][0]['details_url']
  assert client.get(path).status_code==401
  response=client.get(path,headers=auth)
  assert response.status_code==200,response.text
  assert response.json()['arguments']=={'summary':'private contents'}
  assert response.json()['status']=='unknown'
  assert client.get(path.replace('/write-run/','/different-run/'),headers=auth).status_code==404
  assert client.post(path,headers=auth).status_code in (404,405)
  db.expire_all()
  assert db.get(models.AgentWriteIntent,('write-run','write-run')).status=='unknown'
  from datetime import UTC,datetime
  db.get(models.Chat,chat.id).deleted_at=datetime.now(UTC);db.commit()
  assert client.get(path,headers=auth).status_code==404


def test_app_token_cannot_read_private_write_arguments(client,chat,db,auth):
  from app import auth as auth_mod
  owner=attempt(chat);outcome(owner)
  app=models.App(name='Synthetic',slug='no-write-read',source_dir='/tmp/no-write-read',jsx_source='',token_nonce='fixture')
  db.add(app);db.commit()
  token=auth_mod.create_app_token(app.id,'test',0,app_nonce='fixture')
  path=f'/api/chats/{chat.id}/write-outcomes/write-run/write-run'
  assert client.get(path,headers={'Authorization':f'Bearer {token}'}).status_code==403


def test_helper_can_read_failed_write_only_while_its_own_run_is_live(client,chat,db,auth):
  import hashlib
  from app.delegations import delegation_execution_token,policy_for_chat
  db.add(create_chat(id='read-parent',title='Parent',messages=[]))
  db.add(models.Delegation(id='read-helper',parent_chat_id='read-parent',
    parent_root_run_id='parent-root',task_key='read-write-detail',child_chat_id=chat.id,
    provider='codex',scope='write',cwd='/data',prompt_sha256=hashlib.sha256(b'Test').hexdigest()))
  db.commit()
  source=attempt(chat,'source');outcome(source,'unknown')
  submit(FinishRun(**source,terminal_status='completed'))
  reader=attempt(chat,'reader')
  db.expire_all()
  token=delegation_execution_token(db,policy_for_chat(db,chat.id),run_id='reader')
  headers={'Authorization':f'Bearer {token}'}
  path=f'/api/chats/{chat.id}/write-outcomes/source/source'
  response=client.get(path,headers=headers)
  assert response.status_code==200,response.text
  assert response.json()['arguments']=={'summary':'private contents'}
  submit(FinishRun(**reader,terminal_status='completed'))
  assert client.get(path,headers=headers).status_code==401


def test_clean_write_recovery_inherits_original_owner_input_time(chat, db):
  owner = {'chat_id': chat.id, 'run_token': 'owner-write-run'}
  submit(StartTurn(**owner, user_msg={'role': 'user', 'content': 'Test', 'ts': 10},
    owner_input=True, default_provider='claude'))
  submit(AdmitProviderExecution(**owner))
  outcome(owner)
  db.expire_all()
  admitted_at = db.get(models.ChatRun, owner['run_token']).owner_input_at
  assert admitted_at is not None
  result = promote(owner)
  db.expire_all()
  recovered = db.get(models.ChatRun, result['promoted']['_run_token'])
  assert recovered.continuation_json['reason'] == 'quiet_write_failure'
  assert recovered.owner_input_at == admitted_at
