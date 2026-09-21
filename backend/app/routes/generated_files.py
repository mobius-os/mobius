"""Serve files an agent turn wrote to its own cwd (generated_files.py's
allowlisted directory diff), most commonly a PDF a Bash-run script produced.

Security note — read this before touching the lookup below: a non-delegated
chat's cwd is `settings.data_dir` itself (`/data`), a root SHARED by every
other chat's `uploads/` tree and by credential paths (`cli-auth/`,
`.secret-key`). Resolving a client-supplied relative path against that shared
cwd — even with `validate_path_within_base`'s symlink/`..` confinement — would
let one chat's media token read another chat's files, because that helper
only prevents escaping OUTSIDE a root; it does nothing to stop legitimate-
looking traversal INSIDE a root that isn't actually private to this chat.

So `name` is never resolved against cwd directly. It is looked up in THIS
chat's own `generated_files` table rows first (populated only by the
allowlisted diff — see generated_files.py and chat_writer.RecordGeneratedFile)
and only the path recorded there is ever touched. `validate_path_within_base`
is still applied to that looked-up path as defense-in-depth, but the actual
authorization boundary is the per-chat row lookup, not the filesystem check.
"""

import pathlib

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.delegations import policy_for_chat
from app.deps import resolve_media_or_header_owner
from app.path_utils import validate_chat_id, validate_path_within_base

router = APIRouter(prefix="/api/chats", tags=["generated-files"])

# Mirrors uploads.py's forced-download posture: never let a browser render an
# agent-authored file inline (an SVG/HTML deliverable could carry a stored-XSS
# payload). Every generated-file mime type is served as an attachment.


def _chat_cwd(db: Session, chat_id: str) -> str:
  """Resolves the same cwd chat.py used to run this chat's turns."""
  policy = policy_for_chat(db, chat_id)
  if policy is not None and policy.cwd:
    return policy.cwd
  return get_settings().data_dir


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

  cwd = _chat_cwd(db, chat_id)
  file_path = validate_path_within_base(row.path, pathlib.Path(cwd))
  if not file_path.exists() or not file_path.is_file():
    raise HTTPException(status_code=404, detail="File not found.")

  return FileResponse(
    str(file_path),
    media_type=row.mime_type,
    headers={"Content-Disposition": f'attachment; filename="{row.name}"'},
  )
