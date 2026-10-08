"""Reviewed app operations with trusted forms and sealed, one-use execution.

Only non-secret context and prompt metadata live in the transient ticket registry.
A ticket is consumed before execution and never retried, even after restart.
Consumers share the existing bounded sealed runner; no output reaches the UI.
"""
import asyncio
import copy
import json
import secrets
import sys
import time
from types import SimpleNamespace
from fastapi import HTTPException
from app import app_services, models
from app.applied_app_runtime import hold_runtime
from app.config import get_settings
from app.database import SessionLocal
from app.saved_secure_inputs import _run_consumer, consumer_outcome, _consumer_env
from app.secure_input_spec import validate_submitted_values

_tickets = {}
_slots = asyncio.Semaphore(4)


def action_contract(app, action_id):
    service = app_services.service_contract(app, access='self')
    contract = app.capability_contract or {}
    if contract.get('runtime', {}).get('app.owner-action', {}).get('version') != 1:
        raise HTTPException(403, 'Trusted owner actions are not declared.')
    action = service.get('owner_actions', {}).get(action_id)
    if not isinstance(action, dict):raise HTTPException(404, 'Reviewed owner action not found.')
    return action


def prepare(app, owner, action_id, context):
    action = action_contract(app, action_id)
    if not isinstance(context, dict) or len(json.dumps(context)) > 4096:
        raise HTTPException(400, 'Action context must be a small JSON object.')
    now=time.monotonic()
    for key,row in list(_tickets.items()):
        if row['expires'] < now: _tickets.pop(key, None)
    if len(_tickets)>=32:raise HTTPException(429, 'Finish or cancel an open owner action first.')
    ticket=secrets.token_urlsafe(24)
    _tickets[ticket]={'app_id':app.id,'nonce':app.token_nonce,'revision':app.runtime_revision,
      'owner':owner.id,'epoch':owner.token_epoch,'action_id':action_id,'action':copy.deepcopy(action),
      'context':copy.deepcopy(context),'expires':now+900}
    return {'ticket':ticket,'app_name':app.name,'title':action['title'],'description':action['description'],
      'fields':action['fields'], 'context':context}


def take(app, owner, ticket):
    row=_tickets.get(ticket)
    if not row or row['app_id']!=app.id or row['owner']!=owner.id:
        raise HTTPException(404,'Owner action is no longer available. Check its status before trying again.')
    _tickets.pop(ticket)
    validate_ticket(app, owner, row)
    return row


def validate_ticket(app, owner, row):
    if app is None or owner is None or app.deleted_at is not None:
        raise HTTPException(409, 'This action changed or expired. Open it again.')
    if (row['expires']<time.monotonic() or row['nonce']!=app.token_nonce or
        row['revision']!=app.runtime_revision or row['epoch']!=owner.token_epoch or
        row['action']!=action_contract(app,row['action_id'])):
        raise HTTPException(409,'This action changed or expired. Open it again.')


async def execute(app, owner, ticket, fields):
    values={}
    try:
        row=take(app,owner,ticket)
        try:values=validate_submitted_values(SimpleNamespace(fields=row['action']['fields']),fields)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        finally:
            if isinstance(fields,dict):fields.clear()
        async with _slots:
            pin=hold_runtime(app.id)
            try:
                # Waiting for a runner must not preserve revoked authority or
                # execute a superseded accepted source revision.
                with SessionLocal() as db:
                    app = db.get(models.App, row['app_id'])
                    owner = db.get(models.Owner, row['owner'])
                    validate_ticket(app, owner, row)
                    db.expunge(app); db.expunge(owner)
                entry=app_services.service_entry(app,{'entry':row['action']['entry']})
                settings=get_settings()
                env=_consumer_env('');env.pop('CHAT_ID',None)
                env.update(APP_OWNER_ACTION='1',APP_ID=str(app.id),APP_STORAGE_DIR=str(settings.data_dir)+'/apps/'+str(app.id),
                  API_BASE_URL=settings.api_base_url,DATA_DIR=str(settings.data_dir))
                spec={'command':[sys.executable,str(entry)],'cwd':str(entry.parent)}
                try:
                    code=await _run_consumer(spec,{'action':row['action_id'],'context':row['context'],'fields':values},'',env=env)
                except Exception:
                    # Never reflect consumer diagnostics or secret-bearing errors.
                    code=1
                ok,_,message=consumer_outcome('run',code)
                return {'status':'completed' if ok else 'failed','message':message}
            finally:pin.close()
    finally:
        values.clear()
        if isinstance(fields,dict):fields.clear()
