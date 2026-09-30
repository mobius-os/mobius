"""Owner forms never expose values, relax app authority, or repeat execution."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import app_owner_actions as actions, auth as tokens, models
from app.applied_app_runtime import runtime_parent
from tests.test_app_services import _service_app


def configured(db):
    app=_service_app(db)
    contract=copy.deepcopy(app.capability_contract)
    contract['runtime']={'app.owner-action':{'version':1}}
    contract['service']['owner_actions']={'connect':{'entry':'sealed.py','title':'Connect fixture',
      'description':'Synthetic test only.','fields':[{'name':'key','label':'Key','type':'password'}]}}
    app.capability_contract=contract
    (runtime_parent(app.id)/app.runtime_revision/'sealed.py').write_text('import json,sys\nv=json.load(sys.stdin)\nprint(v)\nprint(v,file=sys.stderr)\n')
    db.commit();return app


def test_owner_submit_output_discarded_one_use(client,auth,db,caplog):
    app=configured(db);url=f'/api/apps/{app.id}/owner-actions'
    prepared=client.post(url+'/connect/prepare',headers=auth,json={'item':'fixture'})
    assert prepared.status_code==200
    prompt=prepared.json();assert prompt['title']=='Connect fixture'
    payload={'key':'synthetic-secret-output-must-be-discarded'}
    submitted=client.post(url+'/'+prompt['ticket']+'/submit',headers=auth,json=payload)
    assert submitted.status_code==200 and submitted.json()['status']=='completed'
    assert payload['key'] not in submitted.text+caplog.text
    again=client.post(url+'/'+prompt['ticket']+'/submit',headers=auth,json=payload)
    assert again.status_code==404


def test_revision_changed_rejects_and_consumes_ticket(client,auth,db):
    app=configured(db);url=f'/api/apps/{app.id}/owner-actions'
    ticket=client.post(url+'/connect/prepare',headers=auth,json={}).json()['ticket']
    app.runtime_revision='b'*64;db.commit()
    assert client.post(url+'/'+ticket+'/submit',headers=auth,json={'key':'synthetic'}).status_code==409
    assert ticket not in actions._tickets


def test_cancel_and_unknown_action(client,auth,db):
    app=configured(db);url=f'/api/apps/{app.id}/owner-actions'
    assert client.post(url+'/unknown/prepare',headers=auth,json={}).status_code==404
    ticket=client.post(url+'/connect/prepare',headers=auth,json={}).json()['ticket']
    assert client.post(url+'/'+ticket+'/cancel',headers=auth).status_code==200
    assert client.post(url+'/'+ticket+'/submit',headers=auth,json={'key':'synthetic'}).status_code==404


def test_app_cannot_supply_owner_input(client,auth,db):
    app=configured(db);owner=db.query(models.Owner).first()
    bearer=tokens.create_app_token(app.id,owner.username,owner.token_epoch,app_nonce=app.token_nonce)
    response=client.post(f'/api/apps/{app.id}/owner-actions/connect/prepare',headers={'Authorization':'Bearer '+bearer},json={})
    assert response.status_code==403


def test_validation_never_echoes_secret(client,auth,db):
    app=configured(db);url=f'/api/apps/{app.id}/owner-actions'
    ticket=client.post(url+'/connect/prepare',headers=auth,json={}).json()['ticket']
    value='synthetic-must-not-reflect'
    response=client.post(url+'/'+ticket+'/submit',headers=auth,json={'unexpected':value})
    assert response.status_code==400 and value not in response.text


def test_owner_action_contract_changes_digest():
    from app.app_capabilities import contract_and_digest
    from app.manifest_contract import validate_manifest_contract
    manifest={'id':'fixture','name':'Fixture','version':'1','description':'fixture','entry':'index.jsx',
      'source_files':['service.py','sealed.py'],'capabilities':{'app.owner-action':{'version':1}},
      'service':{'entry':'service.py','owner_actions':{'connect':{'entry':'sealed.py','title':'Connect',
      'description':'Testing.','fields':[{'name':'key','label':'Key','type':'password'}]}}}}
    validate_manifest_contract(manifest)
    contract,digest=contract_and_digest(manifest)
    assert contract['service']['owner_actions']['connect']['entry']=='sealed.py'
    manifest['service']['owner_actions']['connect']['description']='Different operation.'
    assert contract_and_digest(manifest)[1]!=digest
    manifest['service']['owner_actions']['connect']['entry']='../unsafe.py'
    with pytest.raises(ValueError):validate_manifest_contract(manifest)

@pytest.mark.parametrize('scope',['agent','chat_embed','app'])
def test_nondirect_owner_principals_rejected(client,auth,db,scope):
    from app.deps import get_principal
    app=configured(db);owner=db.query(models.Owner).first()
    principal=SimpleNamespace(scope=scope,owner=owner,app_id=None,chat_id=None,run_id=None,delegation_id=None)
    client.app.dependency_overrides[get_principal]=lambda:principal
    try:
        response=client.post(f'/api/apps/{app.id}/owner-actions/connect/prepare',headers=auth,json={})
        assert response.status_code==403
    finally:client.app.dependency_overrides.pop(get_principal,None)

@pytest.mark.parametrize('mutation', ['revision', 'owner_epoch', 'deleted'])
def test_waiting_action_rechecks_live_authority(client, auth, db, monkeypatch, mutation):
    app = configured(db)
    url = f'/api/apps/{app.id}/owner-actions'
    ticket = client.post(url+'/connect/prepare', headers=auth, json={}).json()['ticket']
    class ChangedWhileWaiting:
        async def __aenter__(self):
            if mutation == 'revision': app.runtime_revision = 'c'*64
            elif mutation == 'owner_epoch': db.query(models.Owner).first().token_epoch += 1
            else:
                from datetime import datetime, UTC
                app.deleted_at = datetime.now(UTC)
            db.commit()
        async def __aexit__(self, *args): pass
    async def never_run(*args, **kwargs):
        pytest.fail('A revoked or superseded action must not execute')
    monkeypatch.setattr(actions, '_slots', ChangedWhileWaiting())
    monkeypatch.setattr(actions, '_run_consumer', never_run)
    response = client.post(url+'/'+ticket+'/submit', headers=auth, json={'key':'fixture'})
    assert response.status_code == 409
    assert ticket not in actions._tickets
