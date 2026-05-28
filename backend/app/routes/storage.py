"""Routes for per-app and shared file storage.

PUT body shapes:

  1. JSON inner object for `.json` files (`Content-Type:
     application/json`). Body IS the data:
       PUT /api/storage/apps/7/notes.json
       {"title": "hi", "items": [1, 2, 3]}
     Server stringifies and writes `{"title":"hi","items":[1,2,3]}`.

  2. JSON envelope for non-JSON files (`Content-Type:
     application/json`):
       PUT /api/storage/apps/7/notes.txt
       {"content": "plain text body"}
     Server writes the inner string as-is.

  3. Raw text for non-JSON files (`Content-Type: text/*`):
       PUT /api/storage/apps/7/notes.txt
       plain text body
     Server decodes the request body as UTF-8 and writes it directly.

  4. Raw bytes for any path (`Content-Type: application/octet-stream`,
     or any other non-JSON, non-text MIME type):
       PUT /api/storage/apps/7/blob.bin
       <bytes>
     Server writes the bytes directly.

For JSON requests, a body that is exactly `{"content": "<str>"}` is
treated as the envelope; anything else is the inner object and is only
accepted for `.json` paths.
"""

import json
import logging
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal,
  get_current_owner,
  get_current_owner_or_app,
  get_principal,
)
from app.path_utils import validate_path_within_base

router = APIRouter(prefix="/api/storage", tags=["storage"])

_log = logging.getLogger(__name__)
_SAFE_RE = re.compile(r"^[\w.\-\/]+$")


_LEVELS = {"none": 0, "read": 1, "write": 2}


def _check_cross_app(
  db: Session, principal: Principal, target_app_id: int, mode: str
) -> None:
  """Enforces declared cross-app access on /api/storage/apps/{id}/...

  Owner tokens always pass. App tokens accessing their OWN app always
  pass. App tokens accessing a DIFFERENT app pass only when BOTH:
    - the caller's `cross_app_access` permits the mode, AND
    - the target's `share_with_apps` permits the mode.

  Subject-side is the primary check (threat model: "one app is
  compromised; what stops it from ransacking the others"); object-
  side is defense-in-depth.

  mode: 'read' or 'write'.
  """
  if principal.app_id is None:
    return  # owner token
  if principal.app_id == target_app_id:
    return  # app accessing its own data
  need = _LEVELS[mode]
  caller = (
    db.query(models.App).filter(models.App.id == principal.app_id).first()
  )
  caller_level = _LEVELS.get(
    (caller.cross_app_access or "none").lower() if caller else "none", 0
  )
  if caller_level < need:
    raise HTTPException(
      status_code=403,
      detail=(
        f"This app's cross_app_access is "
        f"'{(caller.cross_app_access if caller else 'none')}' — "
        f"insufficient for {mode}."
      ),
    )
  target = (
    db.query(models.App).filter(models.App.id == target_app_id).first()
  )
  if not target:
    raise HTTPException(status_code=404, detail="App not found.")
  target_level = _LEVELS.get(
    (target.share_with_apps or "none").lower(), 0
  )
  if target_level < need:
    raise HTTPException(
      status_code=403,
      detail=(
        f"App {target_app_id} share_with_apps is "
        f"'{target.share_with_apps}' — insufficient for {mode}."
      ),
    )


def _resolve(base: Path, rel: str) -> Path:
  """Returns a path within base, raising 400 on traversal attempts.

  Layers a strict character-set whitelist over the shared
  validate_path_within_base check. The whitelist rejects spaces,
  quotes, control bytes, and other shell-metacharacters before the
  resolution step ever sees them, which keeps mini-app storage paths
  to the same shape (`[\\w.\\-/]+`) the file watcher and slug logic
  elsewhere already assume.
  """
  if not _SAFE_RE.match(rel):
    raise HTTPException(status_code=400, detail="Invalid path.")
  if ".." in Path(rel).parts:
    raise HTTPException(status_code=400, detail="Path traversal not allowed.")
  return validate_path_within_base(rel, base)


# Text MIME types that are safe to read as UTF-8.
_TEXT_PREFIXES = ("text/", "application/json", "application/xml")


def _serve_file(file_path: Path):
  """Serve a file with the right content type — binary or text."""
  mime, _ = mimetypes.guess_type(file_path.name)
  if mime and not mime.startswith(tuple(_TEXT_PREFIXES)):
    return FileResponse(file_path, media_type=mime)
  if mime == "application/json":
    try:
      return PlainTextResponse(
        file_path.read_text(encoding="utf-8"),
        media_type="application/json",
      )
    except UnicodeDecodeError:
      return FileResponse(file_path)
  # Fall back to text for unknown or text types.
  try:
    return PlainTextResponse(file_path.read_text(encoding="utf-8"))
  except UnicodeDecodeError:
    return FileResponse(file_path)


def _is_envelope(body) -> bool:
  """True iff body is the legacy `{"content": "<string>"}` shape."""
  return (
    isinstance(body, dict)
    and set(body.keys()) == {"content"}
    and isinstance(body["content"], str)
  )


async def _decode_write_body(request: Request, file_path: Path) -> str | bytes:
  """Decodes a PUT body into text or bytes to write to disk.

  Accepts JSON inner objects for `.json` files, the legacy JSON
  envelope for text files, raw text for non-JSON files, and raw bytes
  for any path.
  """
  raw = await request.body()
  content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
  is_json_path = file_path.suffix.lower() == ".json"

  if content_type != "application/json":
    if content_type.startswith("text/"):
      if is_json_path:
        raise HTTPException(
          status_code=415,
          detail="Raw text writes are only accepted for non-JSON paths.",
        )
      try:
        return raw.decode("utf-8")
      except UnicodeDecodeError:
        raise HTTPException(
          status_code=400,
          detail="Text storage writes must be valid UTF-8.",
        )
    return raw

  try:
    body = json.loads(raw)
  except json.JSONDecodeError:
    raise HTTPException(status_code=400, detail="Invalid JSON body.")

  # For .json paths the body IS the document — never sniff for the
  # legacy envelope. Otherwise a mini-app that legitimately stores
  # `{"content": "..."}` (single-field forms, markdown notes, etc.)
  # gets silently unwrapped: the file ends up containing the raw
  # string instead of the JSON object, and the next read returns a
  # string where the app expected a dict. The envelope shape is only
  # meaningful when the path is non-JSON (the envelope was added so
  # text files could be PUT via a JSON body, not the other way).
  if is_json_path:
    return json.dumps(body, ensure_ascii=False)

  if _is_envelope(body):
    return body["content"]

  raise HTTPException(
    status_code=400,
    detail=(
      "Non-JSON paths require an envelope body: "
      "{\"content\": \"<text>\"}."
    ),
  )


@router.get("/apps/{app_id}/{path:path}")
def read_app_file(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Returns a file from an app's data directory."""
  _check_cross_app(db, principal, app_id, mode="read")
  base = Path(get_settings().data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  return _serve_file(file_path)


@router.put("/apps/{app_id}/{path:path}", status_code=204)
async def write_app_file(
  app_id: int,
  path: str,
  request: Request,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Writes content to a file in an app's data directory."""
  _check_cross_app(db, principal, app_id, mode="write")
  base = Path(get_settings().data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  content = await _decode_write_body(request, file_path)
  file_path.parent.mkdir(parents=True, exist_ok=True)
  if isinstance(content, bytes):
    file_path.write_bytes(content)
  else:
    file_path.write_text(content, encoding="utf-8")
  return Response(status_code=204)


@router.delete("/apps/{app_id}/{path:path}", status_code=204)
def delete_app_file(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Deletes a file from an app's data directory. 404 if missing."""
  _check_cross_app(db, principal, app_id, mode="write")
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
async def write_shared_file(
  path: str,
  request: Request,
  _: models.Owner = Depends(get_current_owner),
):
  """Writes content to a file in the shared data directory.

  Auto-snapshots the prior `theme.css` to `theme.css.bak-<unix-ts>`
  on every overwrite, so a theme that breaks the UI can always be
  rolled back from the recovery page or via `?reset-theme=1`. The
  snapshot is BEST-EFFORT — a snapshot failure must not block the
  agent's write, since the write itself is the recovery target.
  """
  settings = get_settings()
  base = Path(settings.data_dir) / "shared"
  file_path = _resolve(base, path)
  content = await _decode_write_body(request, file_path)
  file_path.parent.mkdir(parents=True, exist_ok=True)
  # Snapshot the prior theme.css before any overwrite. The agent
  # already does this informally; making it automatic means no
  # accidental clobber when the agent forgets.
  if file_path.name == "theme.css" and file_path.parent == base:
    try:
      from app.theme import snapshot_theme_if_present
      snapshot_theme_if_present(settings.data_dir)
    except Exception as exc:
      _log.warning("theme.css snapshot failed: %s", exc)
  if isinstance(content, bytes):
    file_path.write_bytes(content)
  else:
    file_path.write_text(content, encoding="utf-8")
  return Response(status_code=204)


@router.delete("/shared/{path:path}", status_code=204)
def delete_shared_file(
  path: str,
  _: models.Owner = Depends(get_current_owner),
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
