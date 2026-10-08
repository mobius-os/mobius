"""Narrow owner-granted app conversation access; no general chat discovery."""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import models
from app.chat_app_connections import binding_digest, valid_connection
from app.chat_log_redaction import _assistant_text
from app.database import get_db
from app.deps import (Principal, get_principal, get_current_owner_for_owner_input,
                      reject_cross_site)
from app.resource_access import get_active_chat_or_404, live_app_or_404

router = APIRouter(prefix='/api/apps/{app_id}/chat-connections', tags=['chat-connections'])


class Connect(BaseModel):
  model_config = ConfigDict(extra='forbid')
  chat_id: str = Field(min_length=1, max_length=64)
  binding_file: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.json$')
  binding_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


def reader(principal, app_id):
  if principal.scope == 'chat_embed' or (principal.app_id is not None and principal.app_id != app_id):
    raise HTTPException(403, 'This connection belongs to another app.')


def metadata(db, row):
  chat = get_active_chat_or_404(db, row.chat_id, load_fields=(
    models.Chat.id, models.Chat.title, models.Chat.pending_question_id,
  ))
  return {'id': row.id, 'chat_id': row.chat_id, 'title': chat.title,
          'needs_owner_input': bool(chat.pending_question_id),
          'created_at': row.created_at.isoformat()}


@router.get('')
def list_connections(app_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
  reader(principal, app_id)
  live_app_or_404(db, app_id)
  rows = db.query(models.ChatAppConnection).filter_by(app_id=app_id).all()
  return {'connections': [metadata(db, row) for row in rows if valid_connection(
    db, row.id, app_id=app_id, app_nonce=principal.app_instance_id)]}


@router.post('', status_code=201, dependencies=[Depends(reject_cross_site)])
def connect(app_id: int, body: Connect, owner=Depends(get_current_owner_for_owner_input),
            db: Session = Depends(get_db)):
  app = live_app_or_404(db, app_id)
  chat = get_active_chat_or_404(db, body.chat_id, load_fields=(models.Chat.id, models.Chat.created_by_app_id))
  if chat.created_by_app_id is not None:
    raise HTTPException(403, 'Only owner-created conversations can be connected.')
  if binding_digest(app_id, body.binding_file) != body.binding_digest:
    raise HTTPException(409, 'The app connection identity changed. Review it again.')
  # One row per pair. Re-grant deliberately rotates identity even if unchanged.
  db.query(models.ChatAppConnection).filter_by(app_id=app_id, chat_id=body.chat_id).delete()
  row = models.ChatAppConnection(id=secrets.token_urlsafe(24), app_id=app_id,
    chat_id=body.chat_id, owner_id=owner.id, owner_epoch=owner.token_epoch,
    app_nonce=app.token_nonce, binding_file=body.binding_file, binding_digest=body.binding_digest)
  db.add(row)
  db.commit()
  return metadata(db, row)


@router.delete('/{connection_id}', status_code=204, dependencies=[Depends(reject_cross_site)])
def disconnect(app_id: int, connection_id: str, owner=Depends(get_current_owner_for_owner_input),
               db: Session = Depends(get_db)):
  # Inactive/expired grants must remain revocable. No valid_connection check.
  db.query(models.ChatAppConnection).filter_by(id=connection_id, app_id=app_id,
                                              owner_id=owner.id).delete()
  db.commit()


def recovery_message(msg):
  """One visible conversation turn, or None for internals and tool-only turns.

  Hidden deliveries and ``kind`` rows (compaction summaries, continuations)
  are not conversation. Finished assistant turns keep their text in blocks,
  so only their text blocks are returned.
  """
  if not isinstance(msg, dict) or msg.get('hidden') or msg.get('kind'):
    return None
  role = msg.get('role')
  if role == 'assistant':
    content = _assistant_text(msg.get('blocks') or [], msg.get('content') or '')
    if not content:
      return None
  elif role == 'user':
    content = msg.get('content') or ''
  else:
    return None
  out = {'role': role, 'content': content}
  if 'cid' in msg:
    out['cid'] = msg['cid']
  return out


@router.get('/{connection_id}/messages')
def messages(app_id: int, connection_id: str, limit: int = Query(100, ge=1, le=100),
             principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
  reader(principal, app_id)
  row = valid_connection(db, connection_id, app_id=app_id, app_nonce=principal.app_instance_id)
  if row is None:
    raise HTTPException(403, 'This chat connection is no longer available.')
  chat = get_active_chat_or_404(db, row.chat_id)
  # Recovery only needs visible conversation text + send identity, never tool
  # payloads, provider sessions, credentials, settings, attachments or sidecars.
  visible = [m for m in map(recovery_message, chat.messages or []) if m is not None]
  return {**metadata(db, row), 'messages': visible[-limit:]}
