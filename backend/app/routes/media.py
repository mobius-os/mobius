"""Authenticated chat-media serving routes."""

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as FastPath
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import resolve_media_or_header_owner
from app.path_utils import validate_path_within_base

# Chat IDs are dashed UUID4 strings produced by str(uuid.uuid4()).
# Rejecting early prevents using a crafted chat_id as a filesystem path
# component to escape the chats/ subtree.
_CHAT_ID_RE = re.compile(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
  re.IGNORECASE,
)


def _validate_chat_id(chat_id: str) -> None:
  """Raises 400 if chat_id doesn't look like a UUID4."""
  if not _CHAT_ID_RE.match(chat_id):
    raise HTTPException(status_code=400, detail="Invalid chat id.")


router = APIRouter(prefix="/api/chats", tags=["media"])

_RASTER_MEDIA_TYPES = {
  "image/avif",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
}


def _serve_chat_image(chat_id, filename, token_src, db):
  """Common auth + path-validation + FileResponse for a chat media file.

  The token can come from two sources:
  - Authorization header: any valid owner JWT (full-session auth).
  - ?token= query param: ONLY a short-lived media-scoped token minted by
    POST /api/chats/{id}/media-token. Owner JWTs are explicitly rejected on
    this path to prevent the 30-day token from leaking into logs/history.

  App tokens are rejected on both paths.
  """
  _validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
  )

  settings = get_settings()
  base = Path(settings.data_dir) / "chats" / chat_id / "media"
  file_path = validate_path_within_base(filename, base)

  if not file_path.is_file():
    raise HTTPException(status_code=404, detail="Image not found.")

  guessed_type = mimetypes.guess_type(file_path.name)[0]
  media_type = (
    guessed_type if guessed_type in _RASTER_MEDIA_TYPES
    else "application/octet-stream"
  )
  return FileResponse(str(file_path), media_type=media_type)


@router.get("/{chat_id}/media/{filename}")
def serve_chat_media(
  chat_id: str,
  filename: str = FastPath(...),
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an agent-attached chat image, such as a screenshot."""
  return _serve_chat_image(chat_id, filename, token_src, db)
