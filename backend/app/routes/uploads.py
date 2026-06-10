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
  get_current_owner, reject_cross_site, resolve_media_or_header_owner,
)
from app.path_utils import validate_path_within_base
from app.resource_access import get_active_chat_or_404
from app.storage_io import atomic_write, app_dir_usage

# Chat IDs are UUID4 strings (str(uuid.uuid4()), 36 chars with dashes).
# Validating the format before constructing any filesystem path prevents
# a crafted chat_id from escaping the /data/chats/ subtree.
_CHAT_ID_RE = re.compile(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
  re.IGNORECASE,
)


def _validate_chat_id(chat_id: str) -> None:
  """Raises 400 if chat_id doesn't look like a UUID4."""
  if not _CHAT_ID_RE.match(chat_id):
    raise HTTPException(status_code=400, detail="Invalid chat id.")

router = APIRouter(prefix="/api/chats", tags=["uploads"])

_MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024

# Per-chat directory total caps. Enforced before each write so a chat
# that accumulates many uploads can't fill /data and OOM the host.
# 200 MB is generous for attachment use (10 × 20 MB max files) while
# still bounding blast radius. Mirror the storage_io.py approach:
# measure once per request, then gate the write.
_MAX_CHAT_UPLOADS_BYTES = 200 * 1024 * 1024  # 200 MB per chat uploads dir

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


# The serve endpoint uses get_auth_token from app.auth_helpers because
# <img> tags and iframes cannot set Authorization headers; ?token= is
# the only way to authenticate browser-initiated resource fetches.


@router.post("/{chat_id}/uploads", dependencies=[Depends(reject_cross_site)])
async def upload_files(
  chat_id: str,
  files: List[UploadFile],
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Saves uploaded files to /data/chats/{id}/uploads/ and records metadata."""
  _validate_chat_id(chat_id)
  chat = get_active_chat_or_404(db, chat_id)

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
      # Enforce the per-chat directory total before writing, so a chat
      # can't accumulate unbounded uploads and fill the host disk.
      dir_used = app_dir_usage(upload_dir)
      if dir_used + len(content) > _MAX_CHAT_UPLOADS_BYTES:
        raise HTTPException(
          status_code=413,
          detail=(
            f"This chat's uploads directory is full "
            f"({_MAX_CHAT_UPLOADS_BYTES // (1024 * 1024)} MB limit per chat). "
            f"Delete some existing uploads to free space."
          ),
        )
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
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns the list of uploaded files for a chat.

  Owner-only. A chat's uploads are partner attachments outside any
  per-app policy, so an app-scoped token must not list them — the
  gated chat-log capability that would grant scoped app access here
  is not built yet.
  """
  _validate_chat_id(chat_id)
  chat = get_active_chat_or_404(db, chat_id)
  return chat.uploads or []


@router.delete(
  "/{chat_id}/uploads/{filename}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def delete_upload(
  chat_id: str,
  filename: str = Path(...),
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Removes an uploaded file from disk and from the chat's upload list."""
  _validate_chat_id(chat_id)
  chat = get_active_chat_or_404(db, chat_id)

  settings = get_settings()
  upload_dir = pathlib.Path(settings.data_dir) / "chats" / chat_id / "uploads"
  file_path = validate_path_within_base(filename, upload_dir)

  if file_path.exists() and file_path.is_file():
    file_path.unlink()

  if chat.uploads:
    chat.uploads = [u for u in chat.uploads if u.get("name") != filename]
    db.commit()

  return Response(status_code=204)


@router.get("/{chat_id}/uploads/{filename}")
def serve_upload(
  chat_id: str,
  filename: str = Path(...),
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an uploaded file. Accepts JWT from header or media token on ?token=.

  Owner-only. The token can come from two sources:
  - Authorization header: any valid owner JWT (full-session auth, the normal
    path for programmatic fetches).
  - ?token= query param: ONLY a short-lived media-scoped token minted by
    POST /api/chats/{id}/media-token. Owner JWTs are explicitly rejected on
    this path — they would leak into access logs, browser history, and
    Referer headers. <img> tags use this path because they can't set headers.

  App tokens are rejected on both paths.
  """
  _validate_chat_id(chat_id)
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
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'

  return FileResponse(str(file_path), headers=headers)
