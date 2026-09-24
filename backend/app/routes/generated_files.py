"""Serve immutable, chat-scoped agent deliverables by their recorded name."""

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import generated_files, models
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import resolve_media_or_header_owner
from app.path_utils import validate_chat_id, validate_path_within_base

router = APIRouter(prefix="/api/chats", tags=["generated-files"])

# Mirrors uploads.py's forced-download posture: never let a browser render an
# agent-authored file inline (an SVG/HTML deliverable could carry a stored-XSS
# payload). Every generated-file mime type is served as an attachment.


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

  data_dir = get_settings().data_dir
  file_path = validate_path_within_base(
    row.path,
    generated_files.stored_dir(data_dir, chat_id),
  )
  if not file_path.exists() or not file_path.is_file():
    raise HTTPException(status_code=404, detail="File not found.")

  return FileResponse(
    str(file_path),
    media_type=row.mime_type,
    filename=row.name,
  )
