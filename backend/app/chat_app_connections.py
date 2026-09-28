"""Revocable, owner-approved conversation access for one app installation.

Not general resource access: callers must opt in at the conversation send and
stream boundaries. The ordinary app/chat/participant fences stay unchanged.
A connection pins the app nonce, owner epoch and an app-owned identity record
(e.g. a channel pairing). No owner credential is given to the app.
"""
from __future__ import annotations
from pathlib import Path

import hashlib
import json
import os
import re
import stat

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.resource_access import get_active_chat_for_principal, live_app


def binding_digest(app_id: int, filename: str) -> str | None:
  """Bounded flat JSON identity record; never follow a file/directory symlink."""
  if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.json", filename):
    return None
  root_fd = file_fd = None
  try:
    root_fd = os.open(
      Path(get_settings().data_dir) / 'apps' / str(app_id),
      os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
      return None
    raw = os.read(file_fd, 8193)
    if len(raw) > 8192:
      return None
    value = json.loads(raw)
    if not isinstance(value, dict) or not value:
      return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()
  except (OSError, ValueError, TypeError):
    return None
  finally:
    if file_fd is not None: os.close(file_fd)
    if root_fd is not None: os.close(root_fd)


def valid_connection(db: Session, connection_id: str, *, app_id: int,
                     chat_id: str | None = None, app_nonce: str | None = None):
  row = db.query(models.ChatAppConnection).filter_by(id=connection_id).first()
  if row is None or row.app_id != app_id or (chat_id is not None and row.chat_id != chat_id):
    return None
  app = live_app(db, app_id)
  owner = db.query(models.Owner).filter_by(id=row.owner_id).first()
  chat = db.query(models.Chat.id).filter(
    models.Chat.id == row.chat_id, models.Chat.deleted_at.is_(None),
    models.Chat.created_by_app_id.is_(None),
  ).first()
  if (app is None or owner is None or chat is None
      or app.token_nonce != row.app_nonce
      or (app_nonce is not None and app_nonce != row.app_nonce)
      or owner.token_epoch != row.owner_epoch
      or binding_digest(app_id, row.binding_file) != row.binding_digest):
    return None
  return row


def conversation_chat(db, chat_id, principal, connection_id=None, *, load_fields=None):
  """Same default fence, with an explicit exact-grant alternative for app sends/SSE."""
  if connection_id:
    if (principal.scope != 'app' or principal.app_id is None
        or not principal.app_instance_id
        or not valid_connection(db, connection_id, app_id=principal.app_id,
                                chat_id=chat_id, app_nonce=principal.app_instance_id)):
      raise HTTPException(403, 'This chat connection is no longer available.')
    # The validated connection targets an owner-created active chat only.
    from app.resource_access import get_active_chat_or_404
    return get_active_chat_or_404(db, chat_id, load_fields=load_fields)
  return get_active_chat_for_principal(db, chat_id, principal, load_fields=load_fields)


def connection_is_active(connection_id: str, app_id: int, chat_id: str, app_nonce: str) -> bool:
  # Never retain a database transaction while waiting for an SSE event.
  with SessionLocal() as db:
    return valid_connection(db, connection_id, app_id=app_id,
                            chat_id=chat_id, app_nonce=app_nonce) is not None


def require_conversation_input(chat, body, connection_id):
  if connection_id and (chat.pending_question_id or body.answers or body.question_id
                        or body.selected_options):
    raise HTTPException(409, 'Open this conversation in Möbius to answer its owner-input card.')

  if connection_id and body.model_fields_set - {'content', 'cid', 'timezone', 'viewport'}:
    raise HTTPException(403, 'Chat connections accept ordinary text messages only.')
