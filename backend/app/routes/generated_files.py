"""Serve files an agent wrote to its chat-private generated directory, most
commonly a PDF produced by a shell-run script.

Security note — read this before touching the lookup below: new rows point
inside `data_dir/chats/<chat>/generated`, while historical rows may still point
inside the chat's old cwd. A client-supplied path is never resolved directly.

So `name` is never resolved against cwd directly. It is looked up in THIS
chat's own `generated_files` table rows first (populated only by the
allowlisted diff — see generated_files.py and chat_writer.RecordGeneratedFile)
and only the path recorded there is ever touched. `validate_path_within_base`
is still applied to that looked-up path as defense-in-depth, but the actual
authorization boundary is the per-chat row lookup, not the filesystem check.
"""

import pathlib

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import generated_files, models
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.delegations import policy_for_chat
from app.deps import resolve_media_or_header_owner
from app.path_utils import (
  attachment_disposition, validate_chat_id, validate_path_within_base,
)

router = APIRouter(prefix="/api/chats", tags=["generated-files"])

# Mirrors uploads.py's forced-download posture: never let a browser render an
# agent-authored file inline (an SVG/HTML deliverable could carry a stored-XSS
# payload). Every generated-file mime type is served as an attachment.


def _chat_cwd(db: Session, chat_id: str) -> str:
  """Resolves the same cwd chat.py used to run this chat's turns."""
  policy = policy_for_chat(db, chat_id)
  if policy is not None and policy.cwd:
    return policy.cwd
  return get_settings().data_dir


@router.get("/{chat_id}/generated-files/{name}")
def serve_generated_file(
  chat_id: str,
  name: str = PathParam(...),
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an agent-generated file. Auth mirrors uploads.py's serve_upload:
  JWT from header or a short-lived media token on ?token= (never an
  unscoped owner JWT in a query string — those leak into logs/history/
  Referer)."""
  validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
  )

  row = db.query(models.GeneratedFile).filter(
    models.GeneratedFile.chat_id == chat_id,
    models.GeneratedFile.name == name,
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="File not found.")

  # New generated files live in a chat-private namespace under data_dir. Keep
  # resolving historical rows against the original chat cwd so existing
  # download chips remain valid after the provenance-safe migration.
  data_dir = get_settings().data_dir
  generated_prefix = (
    generated_files.output_dir(data_dir, chat_id)
    .relative_to(pathlib.Path(data_dir))
    .as_posix() + "/"
  )
  base = (
    data_dir
    if row.path.startswith(generated_prefix)
    else _chat_cwd(db, chat_id)
  )
  file_path = validate_path_within_base(row.path, pathlib.Path(base))
  if not file_path.exists() or not file_path.is_file():
    raise HTTPException(status_code=404, detail="File not found.")

  return FileResponse(
    str(file_path),
    media_type=row.mime_type,
    headers={"Content-Disposition": attachment_disposition(row.name)},
  )
