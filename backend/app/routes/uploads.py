# backend/app/routes/uploads.py
"""Upload and serve per-chat user files."""

import os
import re
import tempfile
from datetime import UTC, datetime
import pathlib
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app import models, chat_queue
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_owner_or_chat_embed_principal, reject_cross_site,
  require_chat_embed_operation, resolve_media_or_header_owner,
)
from app.image_previews import display_image_preview
from app.path_utils import validate_chat_id, validate_path_within_base
from app.resource_access import get_active_chat_for_principal
from app.upload_lifecycle import is_draft, remove_upload_files, take_expired_drafts

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


_UPLOAD_NAME_MAX_BYTES = 200


def _safe_filename(filename: str) -> str:
  """Strips directory components and rejects dangerous filenames."""
  # Strip any path component — only the final name segment is kept.
  name = pathlib.Path(filename).name
  # Replace anything that isn't alphanumeric, dot, dash, or underscore.
  name = re.sub(r"[^\w.\-]", "_", name)
  # Reject empty names after sanitization.
  if not name or name.startswith("."):
    name = "upload"
  # A phone or browser can hand over a name longer than the filesystem allows
  # (255 bytes, easily reached with non-Latin text). Shorten the stem and keep
  # the extension, leaving room for `_unique_name`'s collision suffix.
  if len(name.encode("utf-8")) > _UPLOAD_NAME_MAX_BYTES:
    suffix = pathlib.Path(name).suffix
    if len(suffix.encode("utf-8")) > 32:
      suffix = ""
    budget = _UPLOAD_NAME_MAX_BYTES - len(suffix.encode("utf-8"))
    stem = name[:len(name) - len(suffix)] if suffix else name
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore") or "upload"
    name = stem + suffix
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
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Saves uploaded files to /data/chats/{id}/uploads/ and records metadata."""
  validate_chat_id(chat_id)
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:uploads")
  chat = get_active_chat_for_principal(db, chat_id, principal)

  upload_dir = _resolve_upload_dir(get_settings().data_dir, chat_id)
  # Files are read and written outside the chat lock so answers and Stop never
  # wait behind a large upload; exclusive creation keeps concurrent uploads of
  # one name apart. Only the metadata update below is serialized with
  # admission and discard.
  saved: list[dict] = []
  try:
    for file in files:
      mime = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
      content = await _read_capped(file)
      dest = await run_in_threadpool(
        _create_upload_file, upload_dir, _safe_filename(file.filename or "upload"), content,
      )
      saved.append({
        "name": dest.name,
        "path": str(dest),
        "size": len(content),
        "mime_type": mime,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "claimed": False,
      })
  except BaseException:
    # A later file over the cap must not leave this request's files on disk
    # with no metadata row.
    await run_in_threadpool(remove_upload_files, upload_dir, [pathlib.Path(e["path"]) for e in saved])
    raise
  async with chat_queue.get_lock(chat_id):
    try:
      expired = await run_in_threadpool(_record_uploads, db, chat, saved)
    except Exception:
      # The commit failed and rolled back. A cancelled request is left alone:
      # its commit may already have landed, and its files must then stay.
      await run_in_threadpool(remove_upload_files, upload_dir, [pathlib.Path(e["path"]) for e in saved])
      raise
  await run_in_threadpool(remove_upload_files, upload_dir, expired)
  return saved


async def _read_capped(file: UploadFile) -> bytes:
  # Stream-read in chunks with the per-file cap, aborting the instant it's
  # exceeded, rather than buffering the whole upload before the size check —
  # so a giant file can't balloon memory on the tight host before being
  # rejected.
  chunks: list[bytes] = []
  total = 0
  while chunk := await file.read(1024 * 1024):
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
  return b"".join(chunks)


def _create_upload_file(upload_dir: pathlib.Path, filename: str, content: bytes) -> pathlib.Path:
  """Write content to a temp file, then link it under a free name.

  The link fails if the name is taken, so concurrent uploads of one name stay
  apart without the chat lock, and a crash never leaves a partial file under
  a real name.
  """
  fd, tmp = tempfile.mkstemp(dir=upload_dir, prefix=".upload-", suffix=".tmp")
  try:
    with os.fdopen(fd, "wb") as f:
      f.write(content)
      f.flush()
      os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    while True:
      dest = upload_dir / _unique_name(upload_dir, filename)
      try:
        os.link(tmp, dest)
      except FileExistsError:
        continue
      return dest
  finally:
    os.unlink(tmp)


def _record_uploads(db: Session, chat: models.Chat, saved: list[dict]) -> list[pathlib.Path]:
  """Append new drafts and drop expired ones; the caller holds the chat lock."""
  db.refresh(chat)
  expired = take_expired_drafts(chat)
  chat.uploads = list(chat.uploads or []) + saved
  db.commit()
  return expired


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
async def delete_upload(
  chat_id: str,
  filename: str = Path(...),
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Discards a draft upload: its file and its entry in the chat's upload list.

  Only a draft (see `app.upload_lifecycle`) can be deleted, so a stale tab or a
  late cleanup can never remove a file a sent message or answer uses. Anything
  else is a silent no-op.
  """
  validate_chat_id(chat_id)
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:uploads")
  chat = get_active_chat_for_principal(db, chat_id, principal)
  upload_dir = pathlib.Path(get_settings().data_dir) / "chats" / chat_id / "uploads"
  file_path = validate_path_within_base(filename, upload_dir)

  # Serialize with send/answer admission (which holds this lock while the
  # writer claims): whichever wins, the other sees its committed result.
  async with chat_queue.get_lock(chat_id):
    removed = await run_in_threadpool(_forget_draft, db, chat, filename)
  if removed:
    await run_in_threadpool(remove_upload_files, upload_dir, [file_path])
  return Response(status_code=204)


def _forget_draft(db: Session, chat: models.Chat, filename: str) -> bool:
  db.refresh(chat)
  uploads = list(chat.uploads or [])
  if not any(u.get("name") == filename and is_draft(u) for u in uploads):
    return False
  chat.uploads = [u for u in uploads if u.get("name") != filename]
  db.commit()
  return True


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

  # Serve exactly the recorded upload type: an allowlisted image inline,
  # anything else as an octet-stream attachment. Never let FileResponse infer
  # a type from the filename — `page.html` uploaded as `image/png` would
  # otherwise render as a document on the shell origin.
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

  inline = stored_mime in _INLINE_MIME_TYPES
  headers = {}
  if not inline:
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'

  if preview and inline:
    preview_path = display_image_preview(file_path, upload_dir)
    if preview_path is not None:
      return FileResponse(
        str(preview_path),
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
      )

  return FileResponse(
    str(file_path),
    media_type=stored_mime if inline else "application/octet-stream",
    headers=headers,
  )
