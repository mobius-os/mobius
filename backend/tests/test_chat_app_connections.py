"""Exact-grant tests; no providers, messages or external channels are run."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import models, schemas
from app.chat_app_connections import binding_digest, conversation_chat, valid_connection
from app.config import get_settings
from app.deps import Principal
from app.routes import chats_stream
from test_app_fixtures import create_local_app


def _make_app(client, owner_token, name):
  headers={'Authorization': f'Bearer {owner_token}'}
  app_id=create_local_app(client, headers, name=name)['id']
  token=client.post('/api/auth/app-token',json={'app_id':app_id},headers=headers).json()['token']
  return app_id,token


@pytest.fixture
def setup(client, owner_token, db, monkeypatch):
  app_id, token = _make_app(client, owner_token, 'channel')
  owner_auth = {'Authorization': f'Bearer {owner_token}'}
  auth = {'Authorization': f'Bearer {token}'}
  chat_id = client.post('/api/chats', json={'title': 'Customization'}, headers=owner_auth).json()['id']
  root = Path(get_settings().data_dir) / 'apps' / str(app_id)
  root.mkdir(parents=True, exist_ok=True)
  pair = root / 'pairing.json'
  pair.write_text(json.dumps({'userId': 42, 'chatId': 42, 'epoch': 'a'}))
  base = f'/api/apps/{app_id}/chat-connections'
  body = {'chat_id': chat_id, 'binding_file': 'pairing.json', 'binding_digest': binding_digest(app_id, 'pairing.json')}
  async def accept(*args): return JSONResponse({'status': 'started'}, status_code=202)
  monkeypatch.setattr(chats_stream, '_send_message_locked', accept)
  return dict(app_id=app_id, auth=auth, owner_auth=owner_auth, chat_id=chat_id, pair=pair, base=base, body=body)


def grant(client, s):
  r = client.post(s['base'], json=s['body'], headers=s['owner_auth'])
  assert r.status_code == 201, r.text
  return r.json()['id']


def send(client, s, connection=None, **body):
  auth = dict(s['auth'])
  if connection: auth['X-Mobius-Chat-Connection'] = connection
  return client.post(f"/api/chats/{s['chat_id']}/messages", json={'content': 'hello', **body}, headers=auth)


def test_explicit_grant_only_and_same_chat(client, setup, db):
  s = setup
  assert send(client, s).status_code == 403
  assert client.post(s['base'], json=s['body'], headers=s['auth']).status_code == 403
  connection = grant(client, s)
  assert send(client, s, connection).status_code == 202
  assert send(client, s).status_code == 403
  assert db.get(models.Chat, s['chat_id']).created_by_app_id is None
  assert client.get(s['base'], headers=s['auth']).json()['connections'][0]['id'] == connection
  assert client.get(f"{s['base']}/{connection}/messages", headers=s['auth']).status_code == 200
  other = client.post('/api/chats', json={'title': 'Unrelated'}, headers=s['owner_auth']).json()['id']
  assert send(client, {**s, 'chat_id': other}, connection).status_code == 403
  assert client.get('/api/app-chats', headers=s['auth']).json() == []


@pytest.mark.parametrize('kind', ['revoke', 'regrant', 'pair', 'unpair', 'nonce', 'epoch', 'chat-delete', 'app-delete'])
def test_invalidation(client, setup, db, kind):
  s = setup; cid = grant(client, s)
  if kind == 'revoke':
    assert client.delete(f"{s['base']}/{cid}", headers=s['owner_auth']).status_code == 204
  elif kind == 'regrant': assert grant(client, s) != cid
  elif kind == 'pair': s['pair'].write_text('{"userId":99}')
  elif kind == 'unpair': s['pair'].unlink()
  elif kind == 'nonce': db.get(models.App, s['app_id']).token_nonce = 'changed'; db.commit()
  elif kind == 'epoch': db.query(models.Owner).first().token_epoch += 1; db.commit()
  else:
    from datetime import datetime
    row = db.get(models.Chat, s['chat_id']) if kind == 'chat-delete' else db.get(models.App, s['app_id'])
    row.deleted_at = datetime.now(); db.commit()
  assert send(client, s, cid).status_code in (401, 403, 404)
  assert valid_connection(db, cid, app_id=s['app_id']) is None


def test_no_participant_or_foreign_app_grants(client, setup, owner_token):
  s = setup
  participant = client.post('/api/app-chats', json={'title': 'Participant'}, headers=s['auth']).json()['id']
  assert client.post(s['base'], json={**s['body'], 'chat_id': participant}, headers=s['owner_auth']).status_code == 403
  cid = grant(client, s)
  _, token = _make_app(client, owner_token, 'other')
  foreign = {**s, 'auth': {'Authorization': f'Bearer {token}'}}
  assert send(client, foreign, cid).status_code == 403
  assert client.get(s['base'], headers=foreign['auth']).status_code == 403
  assert client.get(f"{s['base']}/{cid}/messages", headers=foreign['auth']).status_code == 403


def test_connected_app_keeps_other_guards(client, setup):
  s = setup; cid = grant(client, s)
  auth = {**s['auth'], 'X-Mobius-Chat-Connection': cid}
  for path in (f"/api/chats/{s['chat_id']}", f"/api/chats/{s['chat_id']}/runtime"):
    assert client.get(path, headers=auth).status_code == 403
  assert client.patch(f"/api/app-chats/{s['chat_id']}", json={'title': 'forged'}, headers=auth).status_code == 403
  assert client.post(f"/api/app-chats/{s['chat_id']}/output-media-token", headers=auth).status_code == 403
  assert client.delete(f"{s['base']}/{cid}", headers=auth).status_code == 403
  assert send(client, s, cid, answers={'q':'yes'}).status_code == 409
  for extra in ({'hidden':True}, {'attachments':[]}, {'force_steer':True}, {'selected_options':{'q':['yes']}}):
    assert send(client, s, cid, **extra).status_code in (403, 409)


def test_pending_owner_card_requires_mobius(client, setup, db):
  s=setup; cid=grant(client,s)
  db.get(models.Chat,s['chat_id']).pending_question_id='owner-only-card'; db.commit()
  assert send(client,s,cid).status_code == 409
  assert client.get(s['base'],headers=s['auth']).json()['connections'][0]['needs_owner_input'] is True


@pytest.mark.parametrize('kind', ['path', 'symlink', 'large', 'nonobject', 'missing', 'changed'])
def test_binding_fails_closed(client, setup, kind):
  s=setup
  if kind == 'path': s['body']['binding_file']='../pairing.json'
  elif kind == 'symlink':
    target=s['pair'].with_name('other.json'); target.write_text('{}'); s['pair'].unlink(); s['pair'].symlink_to(target)
  elif kind == 'large': s['pair'].write_text(json.dumps({'large': 'a'*9000}))
  elif kind == 'nonobject': s['pair'].write_text('[]')
  elif kind == 'missing': s['pair'].unlink()
  else: s['pair'].write_text('{"epoch":"b"}')
  assert client.post(s['base'],json=s['body'],headers=s['owner_auth']).status_code in (409,422)


def test_owner_agent_cannot_grant(client, setup, db):
  from app.deps import get_current_owner_for_owner_input, require_owner_input_principal
  owner=db.query(models.Owner).first()
  with pytest.raises(HTTPException):
    require_owner_input_principal(Principal(owner=owner,app_id=None,scope='owner',chat_id='agent',run_id='run'))


def test_stream_revocation_checks_after_wait(client, setup, db):
  from app.broadcast import create_broadcast
  s=setup; cid=grant(client,s)
  app=db.get(models.App,s['app_id']); owner=db.query(models.Owner).first()
  principal=Principal(owner=owner,app_id=app.id,app_instance_id=app.token_nonce,scope='app')
  bc=create_broadcast(s['chat_id'])
  request=Request({'type':'http'})
  async def disconnected(): return False
  request.is_disconnected=disconnected
  async def check():
    response=await chats_stream.stream_chat(request,s['chat_id'],snapshot=False,principal=principal,db=db,connection_id=cid)
    iterator=response.body_iterator
    assert 'catch_up_done' in await anext(iterator)
    pending=asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    # Revoke while the stream is awaiting a new event.
    from app.database import SessionLocal
    with SessionLocal() as other:
      other.query(models.ChatAppConnection).filter_by(id=cid).delete(); other.commit()
    bc.publish({'type':'text','text':'must not escape'})
    with pytest.raises(StopAsyncIteration): await pending
    assert not bc.subscribers
  asyncio.run(check())


def test_new_table_upgrade_preserves_existing_rows(tmp_path):
  from sqlalchemy import create_engine, text, inspect
  from app.database import Base
  engine=create_engine(f'sqlite:///{tmp_path}/old.db')
  tables=[t for t in Base.metadata.sorted_tables if t.name != 'chat_app_connections']
  Base.metadata.create_all(engine,tables=tables)
  from sqlalchemy.orm import Session
  with Session(engine) as session:
    session.add(models.Chat(id="kept", title="Existing")); session.commit()
  Base.metadata.create_all(engine)
  assert 'chat_app_connections' in inspect(engine).get_table_names()
  with engine.connect() as conn:
    assert conn.execute(text("SELECT title FROM chats WHERE id='kept'")).scalar() == 'Existing'
  engine.dispose()


def test_send_rechecks_grant_inside_transition_lock(client, setup, monkeypatch):
  s=setup; cid=grant(client,s)
  original=chats_stream.conversation_chat
  calls=[]
  def checked(*args,**kwargs):
    calls.append(True)
    if len(calls)==2:
      from app.database import SessionLocal
      with SessionLocal() as db:
        db.query(models.ChatAppConnection).filter_by(id=cid).delete();db.commit()
    return original(*args,**kwargs)
  monkeypatch.setattr(chats_stream,'conversation_chat',checked)
  assert send(client,s,cid).status_code == 403
  assert len(calls)==2
