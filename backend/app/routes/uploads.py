# backend/app/routes/uploads.py
"""Upload and serve per-chat user files."""

import os
import re
from datetime import UTC, datetime
import pathlib
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models
from app.auth import decode_access_token
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner_or_app

router = APIRouter(prefix="/api/chats", tags=["uploads"])

_MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024

# Images are served inline; everything else is forced to download so the
# browser never executes uploaded content (harmless for a single-owner app,
# but a sensible default regardless).
_INLINE_MIME_TYPES = {
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "image/avif",
}


def _safe_filename(filename: str) -> str:
  """Strips directory components and rejects dangerous filenames."""
  # Strip any path component — only the final name segment is kept.
  name = pathlib.Path(filename).name
  # Replace anything that isn't alphanumeric, dot, dash, or underscore.
  name = re.sub(r"[^\w.\-]", "_", name)
  # Reject empty names after sanitization.
  if not name or name.startswith("."):
    name = "upload"
  return name


def _resolve_upload_dir(data_dir: str, chat_id: str) -> Path:
  """Returns and creates the uploads directory for a chat."""
  p = pathlib.Path(data_dir) / "chats" / chat_id / "uploads"
  p.mkdir(parents=True, exist_ok=True)
  return p


def _unique_name(directory: Path, filename: str) -> str:
  """Returns a filename that does not collide with existing files."""
  dest = directory / filename
  if not dest.exists():
    return filename
  stem = pathlib.Path(filename).stem
  suffix = pathlib.Path(filename).suffix
  i = 1
  while (directory / f"{stem}_{i}{suffix}").exists():
    i += 1
  return f"{stem}_{i}{suffix}"


# The serve endpoint uses this instead of get_current_owner because
# <img> tags and iframes cannot set Authorization headers; ?token= is
# the only way to authenticate browser-initiated resource fetches.
def _auth_token(
  authorization: Optional[str] = Header(default=None),
  token: Optional[str] = Query(default=None),
) -> str:
  """Accepts a JWT from Authorization header or ?token= query param."""
  if authorization and authorization.startswith("Bearer "):
    return authorization[7:]
  if token:
    return token
  raise HTTPException(status_code=401, detail="Not authenticated.")


@router.post("/{chat_id}/uploads")
async def upload_files(
  chat_id: str,
  files: List[UploadFile],
  owner: models.Owner = Depends(get_current_owner_or_app),
  db: Session = Depends(get_db),
):
  """Saves uploaded files to /data/chats/{id}/uploads/ and records metadata."""
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found.")

  settings = get_settings()
  upload_dir = _resolve_upload_dir(settings.data_dir, chat_id)
  saved = []

  for file in files:
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
      raise HTTPException(
        status_code=413,
        detail=(
          f"{file.filename} exceeds the "
          f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
        ),
      )
    total = len(content)
    name = _unique_name(upload_dir, _safe_filename(file.filename or "upload"))
    (upload_dir / name).write_bytes(content)
    saved.append({
      "name": name,
      "path": str(upload_dir / name),
      "size": total,
      "mime_type": mime,
      "uploaded_at": datetime.now(UTC).isoformat(),
    })

  chat.uploads = list(chat.uploads or []) + saved
  db.commit()
  return saved


@router.get("/{chat_id}/uploads")
def list_uploads(
  chat_id: str,
  owner: models.Owner = Depends(get_current_owner_or_app),
  db: Session = Depends(get_db),
):
  """Returns the list of uploaded files for a chat."""
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found.")
  return chat.uploads or []


@router.get("/{chat_id}/uploads/{filename}")
def serve_upload(
  chat_id: str,
  filename: str = Path(...),
  raw_token: str = Depends(_auth_token),
  db: Session = Depends(get_db),
):
  """Serves an uploaded file. Accepts JWT from header or ?token= param."""
  payload = decode_access_token(raw_token)
  if not payload:
    raise HTTPException(status_code=401, detail="Invalid token.")
  owner = db.query(models.Owner).filter(
    models.Owner.username == payload.get("sub")
  ).first()
  if not owner:
    raise HTTPException(status_code=401, detail="Owner not found.")

  settings = get_settings()
  upload_dir = pathlib.Path(settings.data_dir) / "chats" / chat_id / "uploads"
  file_path = (upload_dir / filename).resolve()

  if not str(file_path).startswith(str(upload_dir.resolve()) + os.sep):
    raise HTTPException(status_code=400, detail="Invalid path.")
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")

  # Detect MIME from the stored metadata if available; fall back to
  # letting FileResponse infer it. Force attachment for non-image types
  # to prevent a stored-XSS vector if a malicious file slips through.
  stored_mime = None
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat:
    for entry in (chat.uploads or []):
      if entry.get("name") == filename:
        stored_mime = entry.get("mime_type")
        break

  headers = {}
  if stored_mime not in _INLINE_MIME_TYPES:
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'

  return FileResponse(str(file_path), headers=headers)
