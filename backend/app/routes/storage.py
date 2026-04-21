"""Routes for per-app and shared file storage."""

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner_or_app

router = APIRouter(prefix="/api/storage", tags=["storage"])

_SAFE_RE = re.compile(r"^[\w.\-\/]+$")


def _resolve(base: Path, rel: str) -> Path:
  """Returns a path within base, raising 400 on traversal attempts."""
  if not _SAFE_RE.match(rel):
    raise HTTPException(status_code=400, detail="Invalid path.")
  if ".." in Path(rel).parts:
    raise HTTPException(status_code=400, detail="Path traversal not allowed.")
  resolved = (base / rel).resolve()
  if not str(resolved).startswith(str(base.resolve()) + "/"):
    raise HTTPException(
      status_code=400, detail="Path traversal denied."
    )
  return resolved


# Text MIME types that are safe to read as UTF-8.
_TEXT_PREFIXES = ("text/", "application/json", "application/xml")


def _serve_file(file_path: Path):
  """Serve a file with the right content type — binary or text."""
  mime, _ = mimetypes.guess_type(file_path.name)
  if mime and not mime.startswith(tuple(_TEXT_PREFIXES)):
    return FileResponse(file_path, media_type=mime)
  # Fall back to text for unknown or text types.
  try:
    return PlainTextResponse(file_path.read_text(encoding="utf-8"))
  except UnicodeDecodeError:
    return FileResponse(file_path)


class WriteBody(BaseModel):
  content: str


@router.get("/apps/{app_id}/{path:path}")
def read_app_file(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns a file from an app's data directory."""
  base = Path(get_settings().data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  return _serve_file(file_path)


@router.put("/apps/{app_id}/{path:path}", status_code=204)
def write_app_file(
  app_id: int,
  path: str,
  body: WriteBody,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Writes text content to a file in an app's data directory."""
  base = Path(get_settings().data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  file_path.parent.mkdir(parents=True, exist_ok=True)
  file_path.write_text(body.content, encoding="utf-8")
  return Response(status_code=204)


@router.delete("/apps/{app_id}/{path:path}", status_code=204)
def delete_app_file(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Deletes a file from an app's data directory. 404 if missing."""
  base = Path(get_settings().data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  if not file_path.is_file():
    raise HTTPException(status_code=400, detail="Path is not a file.")
  file_path.unlink()
  return Response(status_code=204)


@router.get("/shared/{path:path}")
def read_shared_file(
  path: str,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns a file from the shared data directory."""
  base = Path(get_settings().data_dir) / "shared"
  file_path = _resolve(base, path)
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  return _serve_file(file_path)


@router.put("/shared/{path:path}", status_code=204)
def write_shared_file(
  path: str,
  body: WriteBody,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Writes text content to a file in the shared data directory."""
  base = Path(get_settings().data_dir) / "shared"
  file_path = _resolve(base, path)
  file_path.parent.mkdir(parents=True, exist_ok=True)
  file_path.write_text(body.content, encoding="utf-8")
  return Response(status_code=204)


@router.delete("/shared/{path:path}", status_code=204)
def delete_shared_file(
  path: str,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Deletes a file from the shared data directory. 404 if missing."""
  base = Path(get_settings().data_dir) / "shared"
  file_path = _resolve(base, path)
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  if not file_path.is_file():
    raise HTTPException(status_code=400, detail="Path is not a file.")
  file_path.unlink()
  return Response(status_code=204)


@router.get("/shared-list/{path:path}")
def list_shared_dir(
  path: str,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Lists files in a shared subdirectory. Returns name, size, mime."""
  base = Path(get_settings().data_dir) / "shared"
  dir_path = _resolve(base, path)
  if not dir_path.is_dir():
    raise HTTPException(status_code=404, detail="Directory not found.")
  entries = []
  for f in sorted(dir_path.iterdir()):
    if f.is_file():
      mime, _ = mimetypes.guess_type(f.name)
      entries.append({
        "name": f.name,
        "size": f.stat().st_size,
        "mime_type": mime,
      })
  return entries
