# backend/app/routes/uploads.py
"""Upload and serve per-chat user files."""

import os
import re
from datetime import UTC, datetime
import pathlib
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app import models
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_owner_or_chat_embed_principal, reject_cross_site,
  require_chat_embed_operation, resolve_media_or_header_owner,
)
from app.image_previews import discard_image_preview, display_image_preview
from app.path_utils import (
  attachment_disposition, safe_filename, validate_chat_id,
  validate_path_within_base,
)
from app.resource_access import get_active_chat_for_principal
from app.storage_io import atomic_write

router = APIRouter(prefix="/api/chats", tags=["uploads"])

_MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024

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
  """Strips directory components and rejects dangerous filenames.

  The rule itself now lives in path_utils so the generated-file download
  route sanitizes identically; this keeps the upload-specific fallback name.
  """
  return safe_filename(filename, fallback="upload")


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


# The serve endpoint uses get_auth_token from app.auth_helpers because
# <img> tags and iframes cannot set Authorization headers; ?token= is
# the only way to authenticate browser-initiated resource fetches.


@router.post("/{chat_id}/uploads", dependencies=[Depends(reject_cross_site)])
async def upload_files(
  chat_id: str,
  files: List[UploadFile],
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Saves uploaded files to /data/chats/{id}/uploads/ and records metadata."""
  validate_chat_id(chat_id)
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:uploads")
  chat = get_active_chat_for_principal(db, chat_id, principal)

  settings = get_settings()
  upload_dir = _resolve_upload_dir(settings.data_dir, chat_id)
  saved = []
  written: list[pathlib.Path] = []

  try:
    for file in files:
      mime = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
      # Stream-read in chunks with the per-file cap, aborting the instant it's
      # exceeded, rather than buffering the whole upload before the size check —
      # so a giant file can't balloon memory on the tight host before being
      # rejected.
      chunks: list[bytes] = []
      total = 0
      while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
          break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
          raise HTTPException(
            status_code=413,
            detail=(
              f"{file.filename} exceeds the "
              f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            ),
          )
        chunks.append(chunk)
      content = b"".join(chunks)
      name = _unique_name(upload_dir, _safe_filename(file.filename or "upload"))
      dest = upload_dir / name
      atomic_write(dest, content)
      written.append(dest)
      saved.append({
        "name": name,
        "path": str(dest),
        "size": total,
        "mime_type": mime,
        "uploaded_at": datetime.now(UTC).isoformat(),
      })

    chat.uploads = list(chat.uploads or []) + saved
    db.commit()
  except BaseException:
    # A later file over the cap, or a commit failure, must not leave the files
    # already written this request orphaned on disk with no metadata row. Unlink
    # them; the metadata change rolls back when the request's session closes.
    for p in written:
      try:
        p.unlink()
      except OSError:
        pass
    raise
  return saved


@router.get("/{chat_id}/uploads")
def list_uploads(
  chat_id: str,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Returns uploads for an owner or the exact authorized chat embed."""
  validate_chat_id(chat_id)
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:uploads")
  chat = get_active_chat_for_principal(db, chat_id, principal)
  return chat.uploads or []


@router.delete(
  "/{chat_id}/uploads/{filename}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def delete_upload(
  chat_id: str,
  filename: str = Path(...),
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Removes an uploaded file from disk and from the chat's upload list."""
  validate_chat_id(chat_id)
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:uploads")
  chat = get_active_chat_for_principal(db, chat_id, principal)

  settings = get_settings()
  upload_dir = pathlib.Path(settings.data_dir) / "chats" / chat_id / "uploads"
  file_path = validate_path_within_base(filename, upload_dir)

  if file_path.exists() and file_path.is_file():
    file_path.unlink()
    discard_image_preview(file_path, upload_dir)

  if chat.uploads:
    chat.uploads = [u for u in chat.uploads if u.get("name") != filename]
    db.commit()

  return Response(status_code=204)


@router.get("/{chat_id}/uploads/{filename}")
def serve_upload(
  chat_id: str,
  filename: str = Path(...),
  preview: bool = False,
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an uploaded file. Accepts JWT from header or media token on ?token=.

  An unscoped owner JWT is accepted only in the Authorization header. A
  short-lived `media` or live-session-chained `chat_embed_media` token is
  accepted from either header or ?token=, always for its exact media_chat.
  Owner JWTs are rejected in query strings because they would leak into access
  logs, history and Referer headers. App/chat_embed tokens are rejected here.
  """
  validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
  )

  settings = get_settings()
  upload_dir = pathlib.Path(settings.data_dir) / "chats" / chat_id / "uploads"
  file_path = validate_path_within_base(filename, upload_dir)

  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")

  # Detect MIME from the stored metadata if available; fall back to
  # letting FileResponse infer it. Force attachment for non-image types
  # to prevent a stored-XSS vector if a malicious file slips through.
  #
  # Lookup intentionally bypasses get_active_chat_or_404 — a missing
  # or soft-deleted chat here degrades to "no stored MIME" instead of
  # 404'ing a file the filesystem still has. The serve endpoint's
  # 404 belongs to the file existence check above.
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
    headers["Content-Disposition"] = attachment_disposition(filename)

  if preview and stored_mime in _INLINE_MIME_TYPES:
    preview_path = display_image_preview(file_path, upload_dir)
    if preview_path is not None:
      return FileResponse(
        str(preview_path),
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
      )

  return FileResponse(str(file_path), headers=headers)
