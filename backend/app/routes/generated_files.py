"""Serve immutable, chat-scoped agent deliverables by their recorded name."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import generated_files, models
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import resolve_media_or_header_owner
from app.path_utils import validate_chat_id

router = APIRouter(prefix="/api/chats", tags=["generated-files"])

# Unknown and active-content formats always download. A small explicit set of
# browser-native document/media formats may opt into inline viewing; nosniff
# keeps an agent-authored payload from changing that reviewed type boundary.


class _AnchoredFileResponse(FileResponse):
  """Serve an already-open inode without letting ASGI reopen a mutable path."""

  def __init__(self, file_fd: int, **kwargs):
    self._file_fd = file_fd
    super().__init__(f"/proc/self/fd/{file_fd}", **kwargs)

  async def __call__(self, scope, receive, send):
    # A pathsend-capable server may resolve the path after this app coroutine
    # returns. Force FileResponse's ordinary range-aware read while our held
    # descriptor is still alive.
    extensions = scope.get("extensions") or {}
    if "http.response.pathsend" in extensions:
      scope = {**scope, "extensions": {
        key: value for key, value in extensions.items()
        if key != "http.response.pathsend"
      }}
    try:
      await super().__call__(scope, receive, send)
    finally:
      os.close(self._file_fd)


@router.get("/{chat_id}/generated-files/{name}")
def serve_generated_file(
  chat_id: str,
  name: str = PathParam(...),
  preview: bool = False,
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

  recorded_path = Path(row.path)
  if recorded_path.is_absolute() or ".." in recorded_path.parts:
    raise HTTPException(status_code=400, detail="Invalid path.")

  data_dir = get_settings().data_dir
  try:
    # Hold the verified inode for the whole response. FileResponse reopens
    # this descriptor through procfs, so a later path/symlink swap cannot
    # change which bytes are served while retaining range-request support.
    file_fd, file_stat = generated_files.open_stored_file(
      data_dir, chat_id, str(recorded_path),
    )
  except OSError:
    raise HTTPException(status_code=404, detail="File not found.")

  inline = preview and generated_files.previewable_mime_type(row.mime_type)
  try:
    return _AnchoredFileResponse(
      file_fd,
      media_type=row.mime_type,
      filename=row.name,
      content_disposition_type="inline" if inline else "attachment",
      headers={"X-Content-Type-Options": "nosniff"},
      stat_result=file_stat,
    )
  except Exception:
    os.close(file_fd)
    raise
