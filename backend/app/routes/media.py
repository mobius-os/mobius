"""Authenticated chat-media serving routes."""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import resolve_media_or_header_owner
from app.image_previews import display_image_preview
from app.path_utils import validate_chat_id, validate_path_within_base

router = APIRouter(prefix="/api/chats", tags=["media"])

_RASTER_MEDIA_TYPES = {
  "image/avif",
  "image/bmp",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
}

_AGENT_TMP_ROOT = Path("/tmp")


def _authorize_chat_media(chat_id, token_src, db, *, allow_app_output=False):
  """Validate the chat id and the owner-scoped media credential once."""
  validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
    allow_app_output=allow_app_output,
  )


def _raster_media_type(file_path: Path) -> str | None:
  guessed_type = mimetypes.guess_type(file_path.name)[0]
  return guessed_type if guessed_type in _RASTER_MEDIA_TYPES else None


def _serve_chat_image(
  chat_id, filename, token_src, db, *, preview=False, allow_app_output=False,
):
  """Common auth + path-validation + FileResponse for a chat media file.

  The token can come from two sources:
  - Authorization header: any valid owner JWT (full-session auth).
  - ?token= query param: ONLY a short-lived media-scoped token minted by
    POST /api/chats/{id}/media-token. Owner JWTs are explicitly rejected on
    this path to prevent the 30-day token from leaking into logs/history.

  App tokens are rejected on both paths.
  """
  _authorize_chat_media(
    chat_id, token_src, db, allow_app_output=allow_app_output,
  )

  settings = get_settings()
  base = Path(settings.data_dir) / "chats" / chat_id / "media"
  file_path = validate_path_within_base(filename, base)

  if not file_path.is_file():
    raise HTTPException(status_code=404, detail="Image not found.")

  media_type = _raster_media_type(file_path) or "application/octet-stream"
  if preview and media_type in _RASTER_MEDIA_TYPES:
    preview_path = display_image_preview(file_path, base)
    if preview_path is not None:
      return FileResponse(
        str(preview_path),
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
      )
  return FileResponse(str(file_path), media_type=media_type)


@router.get("/{chat_id}/media/{filename:path}")
def serve_chat_media(
  chat_id: str,
  filename: str,
  preview: bool = False,
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an agent-attached image or its bounded transcript preview."""
  return _serve_chat_image(
    chat_id, filename, token_src, db, preview=preview, allow_app_output=True,
  )


@router.get("/{chat_id}/tmp-images/{filename:path}")
def serve_agent_tmp_image(
  chat_id: str,
  filename: str,
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serve a raster image viewed by the agent from inside ``/tmp``.

  Codex's native image-view event records only the file path, not a duplicate
  base64 result. This narrow route lets the owner's chat render that exact
  temporary image while keeping every non-image file and every path outside
  ``/tmp`` inaccessible. The ordinary short-lived, chat-scoped media token
  protects browser image requests just like durable chat media.
  """
  _authorize_chat_media(chat_id, token_src, db)
  file_path = validate_path_within_base(filename, _AGENT_TMP_ROOT)
  if not file_path.is_file():
    raise HTTPException(status_code=404, detail="Image not found.")

  media_type = _raster_media_type(file_path)
  if media_type is None:
    raise HTTPException(status_code=415, detail="File is not a supported image.")

  return FileResponse(
    str(file_path),
    media_type=media_type,
    headers={"Cache-Control": "private, no-store"},
  )
